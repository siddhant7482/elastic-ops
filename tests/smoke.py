#!/usr/bin/env python3
"""
Smoke test — starts the real server against a temporary database and walks
through everything a browser or a collector would do.

Run it from the repository root:

    python3 tests/smoke.py

On Python 3.10 it borrows `tomli`, which is byte-for-byte the parser that
became `tomllib` in 3.11, so the config path under test is the real one.
Exit code 0 means every assertion passed.
"""

import http.cookiejar
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

try:
    import tomllib                                                   # noqa: F401
except ModuleNotFoundError:                                          # Python 3.10
    import tomli
    sys.modules["tomllib"] = tomli

import api                                                           # noqa: E402
import auth as authmod                                               # noqa: E402
from engine import Engine, Notifier, load_config, load_inventory     # noqa: E402
from store import Portal, Store                                      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  — ' + str(extra)) if extra and not cond else ''}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CONFIG = """
[settings]
db = "{db}"
listen_port = {port}
default_interval = 999999
timeout = 1
web_dir = "{web}"
inventory = "{inv}"
ingest_token = "test-ingest-token"
session_hours = 1

[[maintenance]]
name = "Nightly backup window"
start = "02:00"
end = "04:15"

[[checks]]
name = "loopback"
group = "Test"
type = "tcp"
target = "127.0.0.1:{port}"

[[jobs]]
name = "Nightly machine backup"
token = "nightly-backup"
grace_hours = 26
"""


class Client:
    """Keeps cookies, like a browser."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect())

    def get(self, path, headers=None):
        return self._do(urllib.request.Request(self.base + path, headers=headers or {}))

    def post_json(self, path, body, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self._do(urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=h))

    def post_form(self, path, fields):
        return self._do(urllib.request.Request(
            self.base + path, data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}))

    def _do(self, req):
        # Close each connection rather than keeping it alive, so shutting the
        # server down at the end cannot race a half-open socket.
        req.add_header("Connection", "close")
        try:
            r = self.op.open(req, timeout=10)
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def main():
    tmp = tempfile.mkdtemp(prefix="ops-smoke-")
    port = free_port()
    cfg_path = os.path.join(tmp, "checks.toml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(CONFIG.format(
            db=os.path.join(tmp, "ops.db").replace("\\", "/"), port=port,
            web=os.path.join(ROOT, "checker", "web").replace("\\", "/"),
            inv=os.path.join(ROOT, "checker", "inventory.toml").replace("\\", "/")))

    cfg = load_config(cfg_path)
    s = cfg["settings"]
    inventory = load_inventory(s["inventory"])
    store = Store(s["db"])
    portal = Portal(store)
    auth = authmod.Auth(store, s["session_hours"], s["cookie_secure"])
    engine = Engine(cfg, store, Notifier(cfg))

    import http.server
    handler = api.make_handler(cfg, inventory, store, portal, engine, auth, s["web_dir"])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    base = f"http://127.0.0.1:{port}"
    c = Client(base)

    print("\nopen routes")
    code, body, _ = c.get("/health")
    check("health answers without a session", code == 200 and json.loads(body)["ok"], code)
    code, _, hdr = c.get("/")
    check("the portal redirects to login when signed out",
          code == 303 and hdr.get("Location") == "/login", code)
    code, _, _ = c.get("/api/status")
    check("the API refuses without a session", code == 401, code)
    code, body, _ = c.get("/login")
    check("login page renders", code == 200 and b"Sign in" in body, code)
    check("login page warns that no accounts exist", b"No accounts exist yet" in body)

    print("\naccounts and sessions")
    check("inventory loaded from toml", len(inventory.get("hosts", [])) == 5,
          len(inventory.get("hosts", [])))
    auth.add_user("tester", "correct-horse-battery", "Test User")
    check("user created", auth.any_users())
    check("wrong password is refused", auth.login("tester", "nope", "1.2.3.4") is None)
    check("right password is accepted",
          auth.login("tester", "correct-horse-battery", "1.2.3.4") is not None)
    check("unknown user is refused", auth.login("nobody", "whatever", "1.2.3.4") is None)

    for i in range(8):
        auth.login("tester", "wrong", "9.9.9.9")
    check("brute force is locked out", auth.locked("9.9.9.9") > 0)
    check("the right password still fails while locked out",
          auth.login("tester", "correct-horse-battery", "9.9.9.9") is None)

    code, body, hdr = c.post_form("/login", {"username": "tester", "password": "wrong"})
    check("bad login re-renders the form", code == 200 and b"Wrong username" in body, code)
    code, _, hdr = c.post_form("/login",
                               {"username": "tester", "password": "correct-horse-battery"})
    cookie = hdr.get("Set-Cookie", "")
    check("good login redirects and sets a cookie",
          code == 303 and "eo_session=" in cookie, code)
    check("session cookie is HttpOnly", "HttpOnly" in cookie)
    check("session cookie is SameSite=Lax", "SameSite=Lax" in cookie)

    print("\nreading the estate")
    code, body, _ = c.get("/")
    check("the portal serves once signed in",
          code == 200 and b"Elastic Ops" in body, code)
    code, body, _ = c.get("/api/me")
    check("api/me names the signed-in user",
          code == 200 and json.loads(body)["display"] == "Test User", body[:120])

    engine.run_check(cfg["checks"][0])
    code, body, _ = c.get("/api/status")
    st = json.loads(body)
    check("status lists the check", code == 200 and st["summary"]["checks"] == 1, st.get("summary"))
    check("the loopback check came back up", st["summary"]["up"] == 1, st["summary"])
    check("history is 45 buckets wide", len(st["items"][0]["history"]) == 45)
    check("the never-run job reads as late", st["summary"]["jobs"] == 1)
    check("maintenance windows are reported", "maintenance" in st)

    for path, key in [("/api/inventory", "hosts"), ("/api/incidents", "incidents"),
                      ("/api/followups", "followups"), ("/api/changes", "changes"),
                      ("/api/backups", "backups"), ("/api/capacity", "pools"),
                      ("/api/hosts", "hosts"), ("/api/events", "events")]:
        code, body, _ = c.get(path)
        check(f"{path} answers", code == 200 and key in json.loads(body), code)

    print("\nwriting records")
    code, body, _ = c.post_json("/api/incidents", {
        "title": "CCTV server unreachable", "severity": "critical",
        "system": "networkserver", "detail": "no answer on 8006"})
    inc = json.loads(body)
    check("incident created with a reference", code == 201 and inc["ref"].startswith("INC-"), body[:120])
    code, body, _ = c.post_json("/api/incidents", {"title": ""})
    check("an incident with no title is rejected", code == 400, code)

    code, body, _ = c.post_json(f"/api/incidents/{inc['id']}/resolve",
                                {"prevention": "Add networkserver to the backup job"})
    check("incident resolves", code == 200 and json.loads(body)["status"] == "resolved", code)
    code, body, _ = c.get("/api/followups")
    fo = json.loads(body)["followups"]
    check("resolving created a follow-up from the prevention answer",
          len(fo) == 1 and "backup job" in fo[0]["action"], fo)
    code, _, _ = c.post_json(f"/api/followups/{fo[0]['id']}/done", {})
    check("follow-up can be marked done", code == 200, code)
    check("follow-up is now done",
          json.loads(c.get("/api/followups")[1])["followups"][0]["status"] == "done")

    code, _, _ = c.post_json("/api/changes", {
        "title": "Capped ZFS cache", "system": "networkserver", "detail": "7.7 GB to 2 GB"})
    check("change recorded", code == 201, code)

    print("\ncsrf, content type and body limits")
    code, _, _ = c.post_form("/api/incidents", {"title": "via a cross-site form"})
    check("a form POST to the API is refused (CSRF)", code == 400, code)
    code, body, _ = c.post_json("/api/incidents", {"title": "x" * (300 * 1024)})
    check("an oversized body gets 413, not a dropped connection", code == 413, code)
    code, _, _ = c.get("/api/me")
    check("the server is still healthy after an oversized body", code == 200, code)

    print("\ningest")
    code, _, _ = c.post_json("/api/ingest/host", {"host": "x"})
    check("ingest without a token is refused", code == 401, code)
    code, _, _ = c.post_json("/api/ingest/host", {"host": "x"},
                             {"X-Ingest-Token": "wrong"})
    check("ingest with a wrong token is refused", code == 401, code)

    tok = {"X-Ingest-Token": "test-ingest-token"}
    code, _, _ = c.post_json("/api/ingest/host", {
        "host": "cinnamon", "mem_total": 134217728000, "mem_used": 111669149696,
        "pools": [{"name": "local-lvm", "used_bytes": 1682029056000,
                   "total_bytes": 1968526671872}]}, tok)
    check("host metrics accepted", code == 200, code)

    code, _, _ = c.post_json("/api/ingest/backups", {"backups": [
        {"vmid": "cinnamon/1100", "name": "1100 cfp", "host": "cinnamon",
         "last_ts": int(time.time()) - 3600, "size_bytes": 1649267441664, "state": "ok"},
        {"vmid": "cinnamon/100", "name": "100 AD-1", "host": "cinnamon",
         "last_ts": None, "size_bytes": None, "state": "bad",
         "detail": "no backup has ever been taken"}]}, tok)
    check("backup rows accepted", code == 200, code)

    code, body, _ = c.get("/api/backups")
    bks = json.loads(body)["backups"]
    check("backups read back", len(bks) == 2, len(bks))
    check("a machine with no backup survives the round trip",
          any(b["last_ts"] is None for b in bks))
    code, body, _ = c.get("/api/capacity")
    pools = json.loads(body)["pools"]
    check("pool usage computed", pools and abs(pools[0]["pct"] - 85.4) < 0.5,
          pools[0] if pools else None)
    code, body, _ = c.get("/api/hosts")
    check("host metrics come back with a series",
          json.loads(body)["hosts"][0]["host"] == "cinnamon", body[:160])

    print("\njob check-in")
    before = store.get("Nightly machine backup")
    code, body, _ = c.get("/checkin/nightly-backup")
    check("check-in with a known token is accepted", code == 200, code)
    after = store.get("Nightly machine backup")
    check("check-in recorded the time", after["last_ok"] and not before["last_ok"])
    check("the job is now up", after["status"] == "up", after["status"])
    code, _, _ = c.get("/checkin/not-a-real-token")
    check("an unknown check-in token is refused", code == 404, code)

    print("\nstatic files")
    code, _, _ = c.get("/assets/../../checker/checks.toml")
    check("path traversal is blocked", code in (403, 404), code)
    code, body, _ = c.get("/assets/nope.css")
    check("a missing asset is a 404", code == 404, code)

    print("\nsigning out")
    code, _, hdr = c.post_form("/logout", {})
    check("logout redirects and clears the cookie",
          code == 303 and "Max-Age=0" in hdr.get("Set-Cookie", ""), code)
    c2 = Client(base)
    code, _, _ = c2.get("/api/status")
    check("a fresh client has no access", code == 401, code)

    srv.shutdown()
    store.db.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
