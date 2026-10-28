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
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime as _dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

try:
    import tomllib                                                   # noqa: F401
except ModuleNotFoundError:                                          # Python 3.10
    import tomli
    sys.modules["tomllib"] = tomli

import api                                                           # noqa: E402
import mailer as mailer_mod                                          # noqa: E402
import auth as authmod                                               # noqa: E402
from engine import Engine, Notifier, load_config, load_inventory     # noqa: E402
from store import Portal, Store                                      # noqa: E402
from store import now as now_ts                                      # noqa: E402

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

    print("\nnamed-instance SQL probe")
    import probes
    ok, _, detail = probes.run({"type": "mssql", "target": f"127.0.0.1:{port}"}, 2)
    check("no instance given falls back to a plain tcp connect", ok, detail)
    ok, _, detail = probes.run({"type": "mssql", "target": "127.0.0.1\\NOSUCH"}, 1)
    check("a host with no SQL Browser fails cleanly, not with an exception",
          ok is False and "Browser" in detail, detail)
    ok, _, detail = probes.run({"type": "mssql", "target": "203.0.113.9\\X"}, 1)
    check("an unreachable host says so rather than hanging",
          ok is False and detail, detail)

    print("\nprinters — SNMP codec and the Printer MIB")
    import printers as prt
    import snmp as snmp_mod

    # Known vector: 1.3.6.1.2.1.1.1.0 encodes to 2b 06 01 02 01 01 01 00,
    # the first two arcs collapsing to 1*40+3 = 0x2b.
    enc = snmp_mod.encode_oid("1.3.6.1.2.1.1.1.0")
    check("OID encodes to the documented bytes",
          enc[2:].hex() == "2b06010201010100", enc.hex())
    check("OID round trips", snmp_mod.decode_oid(enc[2:]) == "1.3.6.1.2.1.1.1.0")
    big = "1.3.6.1.4.1.2435.2.3.9.1"          # Brother's enterprise arc
    check("multi-byte arcs survive the trip",
          snmp_mod.decode_oid(snmp_mod.encode_oid(big)[2:]) == big)
    check("long lengths encode in the long form",
          snmp_mod._len(300) == bytes([0x82, 0x01, 0x2C]), snmp_mod._len(300).hex())
    check("an unreachable agent returns None rather than raising",
          snmp_mod.alive("203.0.113.9", timeout=0.4) is None)

    # -3 means "enough, but I will not count it". Showing 0% would be a lie.
    check("an unmeasured supply has no percentage",
          prt._pct(-3, -2, "toner") is None)
    check("a normal supply is a percentage", prt._pct(48, 100, "toner") == 48.0)
    check("a waste container inverts — 90% full reads as 10% left",
          prt._pct(90, 100, "waste toner") == 10.0)
    check("a zero maximum yields no percentage", prt._pct(5, 0, "toner") is None)

    bits = prt.decode_errors(b"\x24\x00")      # bits 2 and 5
    check("error bitfield decodes to names",
          "low toner" in bits and "jammed" in bits, bits)
    check("an empty error field means no errors", prt.decode_errors("") == [])

    low = {"supplies": [{"name": "Toner (Cyan)", "percent": 8.0},
                        {"name": "Toner (Black)", "percent": 90.0}], "errors": []}
    check("the most depleted supply decides the state",
          prt.worst(low)[0] == "bad", prt.worst(low))
    unmeasured = {"supplies": [{"name": "Black Toner", "percent": None}], "errors": []}
    check("a printer that only reports ok/low is not permanently amber",
          prt.worst(unmeasured)[0] == "ok", prt.worst(unmeasured))
    check("a device error outranks supply levels",
          prt.worst({"supplies": [], "errors": ["jammed"]})[0] == "bad")

    # Paper sizes come as two numbers and no name, so "420 x 297" has to
    # become "A3" before anyone can act on it.
    check("A4 decodes from micrometres", prt.paper_name(210000, 297000, 4) == "A4")
    check("A3 decodes from micrometres", prt.paper_name(420000, 297000, 4) == "A3")
    check("dimensions in either order give the same size",
          prt.paper_name(297000, 210000, 4) == "A4")
    check("a few mm of slack still matches",
          prt.paper_name(296926, 209973, 4) == "A4")
    check("ten-thousandths of an inch also work",
          prt.paper_name(85000, 110000, 3) == "Letter",
          prt.paper_name(85000, 110000, 3))
    check("an unknown size falls back to millimetres",
          prt.paper_name(100000, 500000, 4) == "100x500mm",
          prt.paper_name(100000, 500000, 4))
    check("an absent dimension yields no size", prt.paper_name(-2, -2, 4) is None)

    check("an open cover outranks healthy supplies",
          prt.worst({"supplies": [{"name": "T", "percent": 90}], "errors": [],
                     "covers": [{"name": "Front Door", "open": True}]})[0] == "bad")
    check("closed covers are not a finding",
          prt.worst({"supplies": [{"name": "T", "percent": 90}], "errors": [],
                     "covers": [{"name": "Front Door", "open": False}]})[0] == "ok")

    # A new column on a table that already exists in the wild: CREATE TABLE
    # IF NOT EXISTS does nothing, so every insert throws and the page quietly
    # serves yesterday's rows. This is what that bug looked like.
    old_db = os.path.join(tmp, "old-schema.db")
    con = sqlite3.connect(old_db)
    con.execute("CREATE TABLE printers (host TEXT PRIMARY KEY, name TEXT, "
                "model TEXT, location TEXT, status TEXT, state TEXT, "
                "detail TEXT, pages INTEGER, payload TEXT, error TEXT, "
                "ts INTEGER NOT NULL)")
    con.execute("INSERT INTO printers VALUES('1.2.3.4','old','m','','idle',"
                "'ok','',1,'{}',NULL,0)")
    con.commit()
    con.close()
    migrated = Store(old_db)
    cols = {r["name"] for r in migrated.q("PRAGMA table_info(printers)")}
    check("a missing column is added to an existing table", "serial" in cols, cols)
    migrated.put_printer("5.6.7.8", "n", "m", "l", "idle", "ok", "d", 7,
                         {"trays": []}, None, "SERIAL123")
    check("inserts work after the migration",
          migrated.one("SELECT serial FROM printers WHERE host='5.6.7.8'"
                       )["serial"] == "SERIAL123")
    check("existing rows survive it",
          migrated.one("SELECT name FROM printers WHERE host='1.2.3.4'")["name"] == "old")
    migrated.db.close()

    print("\n  toner runway — a percentage is not actionable, a date is")
    now_s = now_ts()

    def hist(days_ago, pages, cyan):
        store.x("INSERT INTO printer_history(host,ts,pages,supplies) VALUES(?,?,?,?)",
                ("pr", now_s - days_ago * 86400, pages,
                 json.dumps({"Toner (Cyan)": cyan})))

    for i, ago in enumerate(range(20, -1, -1)):      # 90 -> 50 at 2%/day
        hist(ago, 1000 + i * 40, 90 - i * 2)
    tr = store.printer_trend("pr")
    cy = tr["supplies"]["Toner (Cyan)"]
    check("depletion rate is measured", cy["per_day"] == 2.0, cy)
    check("days remaining follow from it", cy["days_left"] == 25, cy)
    check("pages per day is measured", tr["pages_per_day"] == 40.0, tr["pages_per_day"])

    # A cartridge change makes the level jump up. Averaging across it would
    # report a supply refilling itself.
    store.x("DELETE FROM printer_history")
    for i, ago in enumerate(range(40, 20, -1)):
        hist(ago, 1000 + i * 40, 90 - i * 2)
    for i, ago in enumerate(range(20, -1, -1)):
        hist(ago, 1800 + i * 55, 100 - i * 3)
    cy2 = store.printer_trend("pr")["supplies"]["Toner (Cyan)"]
    check("a cartridge change restarts the measurement",
          cy2["per_day"] == 3.0 and 12 <= cy2["days_left"] <= 14, cy2)

    store.x("DELETE FROM printer_history")
    for ago in (10, 5, 0):
        hist(ago, 500, 60.0)
    check("a flat series forecasts nothing rather than something wrong",
          store.printer_trend("pr")["supplies"]["Toner (Cyan)"]["days_left"] is None)
    store.x("DELETE FROM printer_history")
    hist(0, 1, 50.0)
    check("one sample is not a trend",
          store.printer_trend("pr")["supplies"] == {})
    check("history only appends when something moved",
          store.put_printer_history("pr2", 10, {"T": 50.0}) is True
          and store.put_printer_history("pr2", 10, {"T": 50.0}) is False
          and store.put_printer_history("pr2", 11, {"T": 50.0}) is True)
    store.x("DELETE FROM printer_history")

    check("the printer poll interval is configurable, defaulting to 5 minutes",
          load_config(cfg_path)["settings"]["printer_interval"] == 300,
          load_config(cfg_path)["settings"].get("printer_interval"))

    code, body, _ = c.get("/api/printers")
    check("/api/printers answers", code == 200 and "printers" in json.loads(body), code)

    print("\n.env is read by the CLI too, not only by systemd")
    envdir = os.path.join(tmp, "envtest")
    os.makedirs(os.path.join(envdir, "checker"), exist_ok=True)
    with open(os.path.join(envdir, ".env"), "w", encoding="utf-8") as fh:
        fh.write('# a comment\n\nOPS_GMAIL_CLIENT_SECRET=from-file\n'
                 'OPS_GMAIL_REFRESH_TOKEN="quoted-value"\n')
    inner = os.path.join(envdir, "checker", "checks.toml")
    with open(inner, "w", encoding="utf-8") as fh:
        db = os.path.join(envdir, "x.db").replace("\\", "/")
        fh.write(f'[settings]\ndb = "{db}"\ngmail_client_id = "cid"\n'
                 'mail_from = "a@b"\nmail_to = "c@d"\n')
    for k in ("OPS_GMAIL_CLIENT_SECRET", "OPS_GMAIL_REFRESH_TOKEN"):
        os.environ.pop(k, None)
    s2 = load_config(inner)["settings"]
    check("a .env one directory up is picked up",
          s2["gmail_client_secret"] == "from-file", s2["gmail_client_secret"])
    check("quotes are stripped from values",
          s2["gmail_refresh_token"] == "quoted-value", s2["gmail_refresh_token"])
    os.environ["OPS_GMAIL_CLIENT_SECRET"] = "from-real-env"
    s3 = load_config(inner)["settings"]
    check("a real environment variable beats the file",
          s3["gmail_client_secret"] == "from-real-env", s3["gmail_client_secret"])
    os.environ.pop("OPS_GMAIL_CLIENT_SECRET", None)
    check("missing() names the fields rather than shrugging",
          "mail_to" in mailer_mod.Gmail({}).missing(), mailer_mod.Gmail({}).missing())

    print("\nemail alerting")
    mailer = mailer_mod
    check("unconfigured gmail reports itself as such", not mailer.Gmail({}).configured)
    ok, why = mailer.Gmail({}).send("x", "down", "page", "y")
    check("sending while unconfigured is a no-op, not a crash",
          ok is False and "not configured" in why, why)
    check("gmail needs every one of its five values",
          not mailer.Gmail({"gmail_client_id": "a", "gmail_client_secret": "b",
                            "gmail_refresh_token": "c", "mail_from": "d"}).configured)
    g = mailer.Gmail({"gmail_client_id": "a", "gmail_client_secret": "b",
                      "gmail_refresh_token": "c", "mail_from": "ops@x",
                      "mail_to": "one@x, two@x"})
    check("gmail is configured once all five are present", g.configured)
    check("recipients are split and trimmed", g.to == ["one@x", "two@x"], g.to)

    subject, body = mailer.compose("CFP / WinMan SQL", "down", "page",
                                   "connection refused", "http://10.10.1.70:8080")
    check("the subject carries the whole story",
          subject == "[DOWN] CFP / WinMan SQL", subject)
    check("the body carries detail, severity and the portal link",
          "connection refused" in body and "page" in body and "8080" in body)

    msg = mailer.build("ops@x", ["a@x", "b@x"], subject, body)
    check("the message is a well-formed email",
          msg["Subject"] == subject and msg["To"] == "a@x, b@x")
    import base64 as _b64
    raw = _b64.urlsafe_b64encode(msg.as_bytes()).decode()
    check("it base64url-encodes for the gmail api, with no padding issues",
          _b64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).startswith(b"From:"))

    check("smtp is unconfigured without a password",
          not mailer.Smtp({"smtp_host": "smtp.gmail.com", "smtp_user": "u",
                           "mail_to": "a@x"}).configured)
    check("smtp is configured with one",
          mailer.Smtp({"smtp_host": "smtp.gmail.com", "smtp_user": "u",
                       "smtp_password": "p", "mail_to": "a@x"}).configured)

    url = mailer.consent_url("cid", "http://127.0.0.1:1234")
    check("consent asks for offline access and forces the prompt",
          "access_type=offline" in url and "prompt=consent" in url, url)
    check("consent requests only gmail.send, not full mailbox access",
          "gmail.send" in url and "mail.google.com" not in url)

    print("\nwhatsapp alerting")
    import whatsapp as wamod
    check("unconfigured whatsapp reports itself as such",
          not wamod.WhatsApp({}).configured)
    ok, why = wamod.WhatsApp({}).send("x", "down", "page", "y")
    check("sending while unconfigured is a no-op, not a crash",
          ok is False and "not configured" in why, why)

    shapes = {
        "waha": ({"whatsapp_provider": "waha", "whatsapp_url": "http://h:3000",
                  "whatsapp_to": "44@c.us"}, "http://h:3000/api/sendText"),
        "wppconnect": ({"whatsapp_provider": "wppconnect", "whatsapp_url": "http://h:21465",
                        "whatsapp_to": "44", "whatsapp_session": "s"},
                       "http://h:21465/api/s/send-message"),
        "evolution": ({"whatsapp_provider": "evolution", "whatsapp_url": "http://h:8080",
                       "whatsapp_to": "44", "whatsapp_session": "s"},
                      "http://h:8080/message/sendText/s"),
        "callmebot": ({"whatsapp_provider": "callmebot", "whatsapp_to": "+44",
                       "whatsapp_token": "k"}, "api.callmebot.com"),
    }
    for prov, (conf, expect) in shapes.items():
        w = wamod.WhatsApp(conf)
        check(f"{prov} is configured", w.configured, conf)
        req, _ = w._request("hello")
        check(f"{prov} builds the right url", expect in req.full_url, req.full_url)

    tw = wamod.WhatsApp({"whatsapp_provider": "twilio", "whatsapp_url": "https://api/x",
                         "whatsapp_token": "SID:AUTH", "whatsapp_to": "+44",
                         "whatsapp_from": "+1"})
    check("twilio is configured", tw.configured)
    req, _ = tw._request("hello")
    check("twilio uses basic auth and form encoding",
          req.get_header("Authorization", "").startswith("Basic ")
          and "form-urlencoded" in req.get_header("Content-type", ""),
          req.get_header("Content-type"))
    check("twilio without a from number is not configured",
          not wamod.WhatsApp({"whatsapp_provider": "twilio", "whatsapp_url": "u",
                              "whatsapp_token": "t", "whatsapp_to": "+44"}).configured)

    msg = wamod.compose("CFP / WinMan SQL", "down", "page", "connection refused")
    check("the message leads with the state, for a lock screen",
          msg.splitlines()[0].endswith("DOWN: CFP / WinMan SQL"), msg.splitlines()[0])

    print("\nmatrix alerting")
    import matrix as matrixmod
    m = matrixmod.Matrix("", "", "")
    check("unconfigured matrix reports itself as such", not m.configured)
    ok, why = m.send("x", "down", "page", "y")
    check("sending while unconfigured is a no-op, not a crash",
          ok is False and "not configured" in why, why)

    m2 = matrixmod.Matrix("http://127.0.0.1:1", "!r:s", "tok", 1)
    check("a configured but unreachable homeserver is configured", m2.configured)
    ok, why = m2.send("CFP", "down", "page", "connection refused")
    check("an unreachable homeserver fails without raising", ok is False and why, why)
    check("transaction ids are unique per message", m2._txn() != m2._txn())

    # A notifier with matrix configured must still honour severity and the clock.
    n2 = Notifier({"settings": {"webhook_url": "", "webhook_header": "", "timeout": 1,
                                "matrix_homeserver": "http://127.0.0.1:1",
                                "matrix_room": "!r:s", "matrix_token": "tok",
                                "notify_hours": "08:00-18:00",
                                "notify_days": ["mon"]}})
    check("matrix does not bypass a quiet severity",
          n2.withhold("quiet", None, _dt(2026, 10, 26, 10, 0)) == "quiet severity")
    check("matrix does not bypass a maintenance window",
          n2.withhold("page", "backup window", _dt(2026, 10, 26, 10, 0)) is not None)

    print("\nsilences — they stop the message, never the measurement")
    code, body, _ = c.post_json("/api/silences", {
        "target": "loopback", "hours": 2, "reason": "testing"})
    check("a silence can be created", code == 201, body[:120])
    sid = json.loads(body)["id"]
    check("the engine sees the check as silenced", store.silenced("loopback") is not None)
    check("an unrelated check is not silenced", store.silenced("something else") is None)

    code, _, _ = c.post_json("/api/silences", {"target": "x", "hours": 0})
    check("a zero-hour silence is rejected", code == 400, code)
    code, _, _ = c.post_json("/api/silences", {"target": "x", "hours": 1000})
    check("a silence longer than a week is rejected — every silence expires",
          code == 400, code)
    code, _, _ = c.post_json("/api/silences", {"target": "", "hours": 2})
    check("a silence with no target is rejected", code == 400, code)

    # Silencing must not stop the check running or the transition being recorded.
    before = len(store.recent_events(500))
    store.update("loopback", status="up", fails=0)
    engine.transition("loopback", "down", "pretend failure", "page")
    check("a silenced check still records its state change",
          len(store.recent_events(500)) > before)
    check("and the state really changed", store.get("loopback")["status"] == "down")

    code, _, _ = c.post_json(f"/api/silences/{sid}/end", {})
    check("a silence can be lifted early", code == 200, code)
    check("lifting it takes effect at once", store.silenced("loopback") is None)

    past = store.add_silence("loopback", now_ts() - 60, "already over", "test")
    check("an expired silence does not mute anything",
          store.silenced("loopback") is None, past)
    check("a wildcard silence covers everything",
          (store.add_silence("*", now_ts() + 600, "all", "test")
           and store.silenced("anything at all") is not None))
    store.x("DELETE FROM silences")

    print("\ncertificate expiry, judged by each cert's own lifetime")
    # A fixed threshold would alert twice a day about a 12h auto-renewing cert
    # and stay silent about a 90-day one with a week left. Both must be right.
    hour, day = 3600.0, 86400.0
    check("a 12h cert warns only in its last ~2h",
          abs(probes.renewal_threshold(12 * hour) - 1.8 * hour) < 60,
          probes.renewal_threshold(12 * hour) / hour)
    check("a 90-day cert warns at ~13.5 days",
          abs(probes.renewal_threshold(90 * day) - 13.5 * day) < day,
          probes.renewal_threshold(90 * day) / day)
    check("a 1000-year cert is capped at 21 days, not 150 years",
          probes.renewal_threshold(365000 * day) == 21 * day)
    check("a 1-minute cert still gets an hour's floor",
          probes.renewal_threshold(60) == hour)
    check("url parsing defaults the port by scheme",
          probes.hostport_of("https://10.10.1.90/") == ("10.10.1.90", 443)
          and probes.hostport_of("http://10.10.1.32/zabbix/") == ("10.10.1.32", 80)
          and probes.hostport_of("https://10.10.10.22:8007/") == ("10.10.10.22", 8007))
    info, why = probes.cert_info("127.0.0.1", port, 2)
    check("a plain-http port yields no certificate, without raising",
          info is None and why, why)

    code, body, _ = c.get("/api/certs")
    check("/api/certs answers", code == 200 and "certs" in json.loads(body), code)

    print("\nalert routing — severity and the clock")
    n = Notifier({"settings": {"webhook_url": "", "webhook_header": "", "timeout": 1,
                               "notify_hours": "08:00-18:00",
                               "notify_days": ["mon", "tue", "wed", "thu", "fri"]}})
    wed_10 = _dt(2026, 10, 21, 10, 0)     # Wednesday, working hours
    wed_03 = _dt(2026, 10, 21, 3, 0)      # Wednesday, the middle of the night
    sun_10 = _dt(2026, 10, 25, 10, 0)     # Sunday

    check("page goes out at 3am", n.withhold("page", None, wed_03) is None)
    check("page goes out on a Sunday", n.withhold("page", None, sun_10) is None)
    check("notify goes out in working hours", n.withhold("notify", None, wed_10) is None)
    check("notify is held at 3am", n.withhold("notify", None, wed_03) is not None,
          n.withhold("notify", None, wed_03))
    check("notify is held at the weekend", n.withhold("notify", None, sun_10) is not None)
    check("quiet is never sent, even at noon on a Wednesday",
          n.withhold("quiet", None, wed_10) == "quiet severity")
    check("maintenance holds even a page",
          n.withhold("page", "Nightly backup window", wed_10) is not None)

    always = Notifier({"settings": {"webhook_url": "", "webhook_header": "", "timeout": 1}})
    check("no notify_hours configured means around the clock",
          always.withhold("notify", None, wed_03) is None)

    code, body, _ = c.get("/api/status")
    st2 = json.loads(body)
    check("the portal reports the rule it is actually using",
          "notify_hours" in st2 and "off_hours_now" in st2, list(st2)[:6])

    print("\ncoverage — what exists, minus what is checked")
    code, _, _ = c.post_json("/api/ingest/host", {
        "host": "cinnamon", "mem_total": 1, "mem_used": 1, "pools": [],
        "guests": [
            # Checked by address: the loopback check points at this port.
            {"vmid": "1100", "name": "cfp", "kind": "qemu", "status": "running",
             "addresses": ["127.0.0.1"]},
            # Running, no check anywhere — the whole point of the feature.
            {"vmid": "100", "name": "AD-1", "kind": "qemu", "status": "running",
             "addresses": ["10.10.1.21"]},
            # Stopped, so not a gap even though nothing checks it.
            {"vmid": "999", "name": "old-backupserver", "kind": "qemu",
             "status": "stopped", "addresses": []},
        ]}, tok)
    check("guests accepted", code == 200, code)

    code, body, _ = c.get("/api/coverage")
    cov = json.loads(body)
    gaps = {g["name"] for g in cov["gaps"]}
    check("guest matched to a check by address is not a gap", "cfp" not in gaps, gaps)
    check("running guest with no check is a gap", "AD-1" in gaps, gaps)
    check("stopped guest is not reported as a gap", "old-backupserver" not in gaps, gaps)
    check("counts add up", cov["total"] == 3 and cov["running"] == 2,
          (cov["total"], cov["running"]))

    # A metrics-only post omits 'guests' and must not wipe the list.
    c.post_json("/api/ingest/host", {"host": "cinnamon", "mem_total": 2,
                                     "mem_used": 1, "pools": []}, tok)
    code, body, _ = c.get("/api/coverage")
    check("a metrics-only post does not wipe the guest list",
          json.loads(body)["total"] == 3, json.loads(body)["total"])

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
