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

import contextlib
import http.cookiejar
import io
import json
import os
import queue
import re
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
    tomllib = tomli          # bind the name too, not just the module entry

import api                                                           # noqa: E402
import mailer as mailer_mod                                          # noqa: E402
import auth as authmod                                               # noqa: E402
import pool                                                          # noqa: E402
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

    handler = api.make_handler(cfg, inventory, store, portal, engine, auth, s["web_dir"])
    # The real server, so every assertion below also exercises the worker pool.
    srv = pool.Pool(("127.0.0.1", port), handler, workers=s["http_workers"],
                    backlog=s["http_backlog"], idle=s["http_idle"])
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
    code, body, hdr = c.get("/assets/manifest.json")
    check("the web manifest serves", code == 200 and b'"Elastic Ops"' in body, code)
    check("it is served as json",
          "json" in hdr.get("Content-Type", ""), hdr.get("Content-Type"))
    code, body, hdr = c.get("/assets/icon.svg")
    check("the app icon serves as svg",
          code == 200 and "svg" in hdr.get("Content-Type", ""), hdr.get("Content-Type"))
    code, body, _ = c.get("/")
    check("the page carries an icon and a theme colour",
          b"apple-touch-icon" in body and b"theme-color" in body)

    print("\ndisk and array integrity")
    sys.path.insert(0, os.path.join(ROOT, "collector"))
    import post_estate as coll

    # A fixture /sys tree: one clean raid0 and one degraded raid5, which is
    # exactly the pair this estate actually has.
    sysroot = os.path.join(tmp, "sysblock")
    for dev, vals, members in (
        ("md0", {"level": "raid0", "array_state": "clean", "degraded": "0",
                 "raid_disks": "2", "sync_action": "idle", "sync_completed": "none"},
         {"sda": "in_sync", "sdb": "in_sync"}),
        ("md1", {"level": "raid5", "array_state": "clean", "degraded": "1",
                 "raid_disks": "3", "sync_action": "idle", "sync_completed": "none"},
         {"sdc": "in_sync", "sdd": "faulty"}),
    ):
        md = os.path.join(sysroot, dev, "md")
        os.makedirs(md, exist_ok=True)
        for k, v in vals.items():
            with open(os.path.join(md, k), "w", encoding="utf-8") as fh:
                fh.write(v + "\n")
        for disk, st in members.items():
            os.makedirs(os.path.join(md, "dev-" + disk), exist_ok=True)
            with open(os.path.join(md, "dev-" + disk, "state"), "w",
                      encoding="utf-8") as fh:
                fh.write(st + "\n")
    os.makedirs(os.path.join(sysroot, "sda"), exist_ok=True)   # not an array

    arrs = {a["name"]: a for a in coll.md_arrays(sysroot)}
    check("mdraid parser finds both arrays and skips plain disks",
          sorted(arrs) == ["md0", "md1"], sorted(arrs))
    check("raid0 is reported as having no redundancy",
          arrs["md0"]["redundant"] is False and arrs["md0"]["level"] == "raid0",
          arrs.get("md0"))
    check("raid5 is reported as redundant", arrs["md1"]["redundant"] is True)
    check("a missing disk is counted", arrs["md1"]["degraded"] == 1
          and arrs["md1"]["disks"] == 3, arrs.get("md1"))
    check("a faulty member is carried through",
          any(m["state"] == "faulty" for m in arrs["md1"]["members"]),
          arrs["md1"]["members"])

    # zpool and pvesh are shelled out to, so stub the one helper they share.
    real_sh = coll.sh
    zpool_status = (
        "  pool: rpool\n state: ONLINE\n  config:\n\n"
        "\tNAME        STATE     READ WRITE CKSUM\n"
        "\trpool       ONLINE       0     0     0\n"
        "\t  mirror-0  ONLINE       0     0     0\n"
        "\t    sde     ONLINE       0     0     0\n"
        "\t    sdf     ONLINE       0     0     0\n")

    def fake_sh(*args):
        if args[:2] == ("zpool", "list"):
            return "rpool\n"
        if args[:2] == ("zpool", "status"):
            return zpool_status
        if args[0] == "pvesh":
            return json.dumps([
                {"devpath": "/dev/sda", "model": "ST4000", "serial": "ZA1",
                 "size": 4000787030016, "health": "PASSED", "wearout": "N/A",
                 "type": "hdd", "used": "LVM"},
                {"devpath": "/dev/sdb", "model": "ST4000", "serial": "ZA2",
                 "size": 4000787030016, "health": "FAILED!", "wearout": "N/A",
                 "type": "hdd", "used": "LVM"},
                {"devpath": "/dev/vda", "model": "QEMU", "serial": "",
                 "size": 107374182400, "health": "UNKNOWN", "wearout": "N/A",
                 "type": "hdd", "used": ""}])
        return ""

    coll.sh = fake_sh
    try:
        z = coll.zfs_arrays()
        dks = coll.disks("proxmox")
    finally:
        coll.sh = real_sh
    check("zpool is parsed with its health", len(z) == 1
          and z[0]["state"] == "ONLINE", z)
    check("a mirror vdev is recognised as redundant",
          z and z[0]["redundant"] is True, z)
    check("mirror members are collected",
          z and sorted(m["dev"] for m in z[0]["members"]) == ["sde", "sdf"],
          z[0]["members"] if z else None)
    check("pvesh disks are parsed with SMART health", len(dks) == 3
          and dks[1]["health"] == "FAILED!", dks)

    # Verbatim output from proxmox's P408i-a, including the unclosed bracket
    # after "Array A (SAS," and the dead disk in bay 2I:3:8. A hand-tidied
    # fixture would not have caught either.
    SSACLI = (
        "\nHPE Smart Array P408i-a SR Gen10 in Slot 0 (Embedded)  (sn: PEYHC0DRHBD7TR)\n"
        "\n\n   Internal Drive Cage at Port 1I, Box 3, OK\n"
        "\n   Port Name: 1I (Mixed)\n"
        "\n   Array A (SAS, \n"
        "      logicaldrive 1 (1.36 TB, RAID 5, Interim Recovery Mode)\n"
        "\n      physicaldrive 1I:3:3 (port 1I:box 3:bay 3, SAS HDD, 300 GB, OK)\n"
        "      physicaldrive 1I:3:4 (port 1I:box 3:bay 4, SAS HDD, 300 GB, OK)\n"
        "      physicaldrive 2I:3:5 (port 2I:box 3:bay 5, SAS HDD, 300 GB, OK)\n"
        "      physicaldrive 2I:3:6 (port 2I:box 3:bay 6, SAS HDD, 300 GB, OK)\n"
        "      physicaldrive 2I:3:7 (port 2I:box 3:bay 7, SAS HDD, 300 GB, OK)\n"
        "      physicaldrive 2I:3:8 (port 2I:box 3:bay 8, SAS HDD, 0 GB, Failed)\n"
        "\n\n   Array B (Solid State SATA, Unused Space: 0  MB)\n"
        "\n      logicaldrive 2 (1.82 TB, RAID 0, OK)\n"
        "\n      physicaldrive 1I:3:1 (port 1I:box 3:bay 1, SATA SSD, 1 TB, OK)\n"
        "      physicaldrive 1I:3:2 (port 1I:box 3:bay 2, SATA SSD, 1 TB, OK)\n"
        "\n   SEP (Vendor ID HPE, Model Smart Adapter) 379  (WWID: 51402EC011947930)\n")

    coll.sh = lambda *a: SSACLI if a[:2] == ("ssacli", "ctrl") else ""
    try:
        sa, sd = coll.ssacli_arrays()
    finally:
        coll.sh = real_sh
    byname = {a["name"]: a for a in sa}
    check("ssacli finds both arrays despite the unclosed bracket",
          sorted(byname) == ["slot0 Array A", "slot0 Array B"], sorted(byname))
    check("the raid5 level and state are read",
          byname["slot0 Array A"]["level"] == "raid5"
          and byname["slot0 Array A"]["state"] == "Interim Recovery Mode",
          byname.get("slot0 Array A"))
    check("Interim Recovery Mode counts as degraded",
          byname["slot0 Array A"]["degraded"] == 1,
          byname["slot0 Array A"]["degraded"])
    check("all six raid5 members are collected",
          byname["slot0 Array A"]["disks"] == 6,
          byname["slot0 Array A"]["disks"])
    check("the failed bay is identified by name",
          any(m["dev"] == "2I:3:8" and m["state"] == "Failed"
              for m in byname["slot0 Array A"]["members"]),
          byname["slot0 Array A"]["members"])
    check("the raid0 is clean but flagged as having no redundancy",
          byname["slot0 Array B"]["state"] == "OK"
          and byname["slot0 Array B"]["redundant"] is False
          and byname["slot0 Array B"]["degraded"] == 0,
          byname.get("slot0 Array B"))
    check("the raid5 is marked redundant by level",
          byname["slot0 Array A"]["redundant"] is True)
    check("physical drives come back as disks with their bay as the id",
          len(sd) == 8 and sd[-1]["dev"] == "1I:3:2", [len(sd), sd[-1]["dev"]])
    check("drive sizes are parsed to bytes",
          sd[0]["size_bytes"] == 300 * 10 ** 9, sd[0]["size_bytes"])
    check("ssd and hdd are told apart",
          sd[0]["type"] == "hdd" and sd[-1]["type"] == "ssd",
          [sd[0]["type"], sd[-1]["type"]])
    check("a missing ssacli is not an error, just no arrays",
          coll.ssacli_arrays() == ([], []))

    # The dead bay has to become an alert, with the bay named in the detail.
    store.put_arrays("proxmox-ssa", sa)
    store.put_disks("proxmox-ssa", sd)
    sent2, keep_send = [], engine.note.send
    engine.note.send = lambda *a, **k: sent2.append(a)
    try:
        engine.sweep_storage()
    finally:
        engine.note.send = keep_send
    sst = {r["name"]: r for r in store.all_state() if r["kind"] == "storage"}
    a5 = sst.get("array proxmox-ssa/slot0 Array A", {})
    check("the degraded raid5 array alerts", a5.get("status") == "down", a5)
    check("the alert names the dead bay",
          "2I:3:8" in (a5.get("detail") or ""), a5.get("detail"))
    check("the alert says the redundancy is spent",
          "Interim Recovery Mode" in (a5.get("detail") or ""), a5.get("detail"))
    check("the failed physical drive is its own check, by bay",
          sst.get("disk proxmox-ssa 2I:3:8", {}).get("status") == "down",
          sorted(k for k in sst if "proxmox-ssa" in k))
    check("healthy bays are up, not noise",
          sst.get("disk proxmox-ssa 1I:3:3", {}).get("status") == "up",
          sst.get("disk proxmox-ssa 1I:3:3"))
    check("the production raid0 does not alert for lacking redundancy",
          sst.get("array proxmox-ssa/slot0 Array B", {}).get("status") == "up",
          sst.get("array proxmox-ssa/slot0 Array B"))
    store.put_arrays("proxmox-ssa", [])
    store.put_disks("proxmox-ssa", [])

    # Round-trip through the store, including the prune.
    store.put_arrays("proxmox", coll.md_arrays(sysroot) + z)
    store.put_disks("proxmox", dks)
    rows = {a["name"]: a for a in store.arrays()}
    check("arrays store and read back", sorted(rows) == ["md0", "md1", "rpool"],
          sorted(rows))
    check("redundancy survives the round trip",
          rows["md0"]["redundant"] == 0 and rows["md1"]["redundant"] == 1,
          [rows["md0"]["redundant"], rows["md1"]["redundant"]])
    check("disks store and read back", len(store.disks()) == 3,
          len(store.disks()))
    store.put_arrays("proxmox", [a for a in coll.md_arrays(sysroot)
                                  if a["name"] == "md0"])
    check("an array that stops being reported is pruned",
          [a["name"] for a in store.arrays()] == ["md0"],
          [a["name"] for a in store.arrays()])

    # The engine turns that into alertable state. This is the whole point:
    # pools were only ever displayed, never alerted on.
    store.put_arrays("proxmox", coll.md_arrays(sysroot) + z)
    sent = []
    real_send = engine.note.send
    engine.note.send = lambda *a, **k: sent.append(a)
    try:
        engine.sweep_storage()
    finally:
        engine.note.send = real_send
    st = {r["name"]: r for r in store.all_state() if r["kind"] == "storage"}
    check("the degraded raid5 is down",
          st.get("array proxmox/md1", {}).get("status") == "down",
          st.get("array proxmox/md1"))
    check("the clean raid0 is up, not red for lacking redundancy",
          st.get("array proxmox/md0", {}).get("status") == "up",
          st.get("array proxmox/md0"))
    check("the raid0 still says so in its detail",
          "no redundancy" in (st.get("array proxmox/md0", {}).get("detail") or ""),
          st.get("array proxmox/md0", {}).get("detail"))
    check("the failed disk is down",
          st.get("disk proxmox /dev/sdb", {}).get("status") == "down",
          st.get("disk proxmox /dev/sdb"))
    check("a virtual disk with UNKNOWN SMART is not a check at all",
          "disk proxmox /dev/vda" not in st, sorted(st))
    check("the degraded array actually reached the notifier",
          any("array proxmox/md1" in a[0] for a in sent), sent)
    check("storage severity defaults to page",
          s["storage_severity"] == "page", s.get("storage_severity"))

    # Storage checks are discovered at runtime, so the startup sweep that
    # drops deleted config checks must leave them alone.
    store.forget_missing(["Zabbix"])
    still = [r["name"] for r in store.all_state() if r["kind"] == "storage"]
    check("forget_missing does not wipe discovered storage checks",
          "array proxmox/md1" in still, still)

    # Round-trip over the real ingest endpoint, which is how this arrives in
    # production — and the omit rule, which is the contract most likely to be
    # broken by a future edit.
    itok = {"X-Ingest-Token": "test-ingest-token"}
    code, ibody, _ = c.post_json("/api/ingest/host", {
        "host": "yam",
        "arrays": [{"name": "md9", "kind": "mdraid", "level": "raid1",
                    "state": "clean", "degraded": 0, "disks": 2,
                    "redundant": True, "sync_action": "idle",
                    "sync_completed": "none",
                    "members": [{"dev": "sdx", "state": "in_sync"}]}],
        "disks": [{"dev": "/dev/sdx", "model": "WD", "serial": "W1",
                   "size_bytes": 1000204886016, "health": "PASSED",
                   "wearout": 97, "type": "hdd", "used": "ZFS"}],
    }, itok)
    ij = json.loads(ibody) if code == 200 else {}
    check("arrays and disks ingest over the api",
          code == 200 and ij.get("arrays") == 1 and ij.get("disks") == 1,
          [code, ij])
    check("the ingested array is stored against its host",
          any(a["host"] == "yam" and a["name"] == "md9"
              for a in store.arrays()), [a["name"] for a in store.arrays()])
    # A metrics post from an older collector omits the keys entirely. That must
    # not read as "this host has no disks" and wipe them.
    c.post_json("/api/ingest/host", {"host": "yam", "mem_total": 1,
                                     "mem_used": 1}, itok)
    check("omitting arrays does not wipe the host's arrays",
          any(a["host"] == "yam" for a in store.arrays()),
          [a["host"] for a in store.arrays()])
    check("omitting disks does not wipe the host's disks",
          any(d["host"] == "yam" for d in store.disks()),
          [d["host"] for d in store.disks()])
    # An explicit empty list is a host saying it really has none.
    c.post_json("/api/ingest/host", {"host": "yam", "arrays": []}, itok)
    check("an explicit empty list does clear them",
          not any(a["host"] == "yam" for a in store.arrays()),
          [a["host"] for a in store.arrays()])

    code, body, _ = c.get("/api/storage")
    sj = json.loads(body) if code == 200 else {}
    check("the storage API serves arrays and disks", code == 200
          and sorted(a["name"] for a in sj.get("arrays", [])) == ["md0", "md1", "rpool"]
          and "/dev/sdb" in [d["dev"] for d in sj.get("disks", [])],
          [code, sorted(a["name"] for a in sj.get("arrays", []))])
    check("members come back as json, not a string",
          isinstance(sj["arrays"][0]["members"], list), sj["arrays"][0]["members"])

    # The page's loader unpacks Promise.all by index, so adding an endpoint
    # without updating the index silently shifts every later field.
    code, page, _ = c.get("/")
    page = page.decode("utf-8", "replace")
    check("the page has a disks and arrays section",
          'id="v-storage"' in page and "function renderStorage(" in page)
    check("the page fetches the storage endpoint",
          '"/api/storage"' in page)
    check("the loader index for storage matches its position",
          "S.storage=r[14]" in page
          and page.count("api(\"/api/") == 15, page.count("api(\"/api/"))
    check("storage is in the nav", '"storage","Disks' in page)

    print("\nestate launcher")
    code, page, _ = c.get("/")
    page = page.decode("utf-8", "replace")
    check("the launcher section exists", 'id="v-estate"' in page)
    check("the launcher is rendered on load", "renderEstate()" in page)
    check("the launcher is the landing view", 'current="estate"' in page)
    check("the launcher is in the nav", '"estate","Estate"' in page)
    check("overview is still reachable", '"overview","Overview"' in page)
    check("the LED board is present",
          'id="led-msg"' in page and "ALL SYSTEMS NOMINAL" in page)

    apps = inventory.get("apps", [])
    check("devices and consoles were added to the launcher",
          len([a for a in apps if a.get("group") == "Devices"]) == 6,
          len([a for a in apps if a.get("group") == "Devices"]))
    check("every app has a name and a group",
          all(a.get("name") and a.get("group") for a in apps))

    # A tile whose icon does not exist silently falls back to a generic box,
    # and a tile pointing at a check that does not exist shows "unknown"
    # forever. Both are invisible in the browser and obvious from here.
    icons = set(re.findall(r"^\s*([a-z]+):", re.search(
        r"var ICON=\{(.*?)\n\};", page, re.S).group(1), re.M))
    bad_icon = [a["name"] for a in apps if a.get("icon") and a["icon"] not in icons]
    check("every tile icon exists in the icon set", not bad_icon, bad_icon)

    # Against the SHIPPED checks.toml, not the cut-down one this test runs on
    # — the pairing that matters is the one that goes to pepper.
    with open(os.path.join(ROOT, "checker", "checks.toml"), "rb") as fh:
        shipped = tomllib.load(fh)
    shipped_names = {x["name"] for x in shipped.get("checks", [])}
    shipped_names |= {x["name"] for x in shipped.get("jobs", [])}
    bad_check = [f"{a['name']} -> {a['check']}" for a in apps
                 if a.get("check") and a["check"] not in shipped_names]
    check("every tile's check refers to a real check", not bad_check, bad_check)

    linked = [a for a in apps if a.get("check")]
    check("most tiles carry a live status", len(linked) >= 12, len(linked))
    check("device tiles are honest about not being monitored",
          all(not a.get("check") for a in apps if a.get("group") == "Devices"))

    print("\nediting and deleting what you wrote")
    # Create-and-close was the whole surface: a typo in a title was permanent,
    # and an incident raised by mistake could only be "resolved", which leaves
    # a lie in the record rather than removing it.
    code, body, _ = c.post_json("/api/incidents", {
        "title": "Typpo in the titel", "severity": "minor",
        "system": "testbox", "detail": "first draft"})
    inc = json.loads(body)
    iid = inc["id"]
    check("an incident can be opened", code == 201 and inc["ref"], code)

    code, body, _ = c.post_json(f"/api/incidents/{iid}/edit",
                                {"title": "Typo in the title fixed",
                                 "detail": "corrected", "severity": "major"})
    fixed = json.loads(body) if code == 200 else {}
    check("an incident can be edited", code == 200
          and fixed.get("title") == "Typo in the title fixed"
          and fixed.get("severity") == "major", [code, fixed.get("title")])
    check("editing leaves the ref alone", fixed.get("ref") == inc["ref"],
          fixed.get("ref"))
    check("fields left out of an edit are untouched",
          fixed.get("system") == "testbox", fixed.get("system"))

    code, _, _ = c.post_json(f"/api/incidents/{iid}/edit", {"title": "   "})
    check("an edit cannot blank the title", code == 400, code)

    # A follow-up hangs off the incident; deleting the incident must take it.
    code, body, _ = c.post_json("/api/followups",
                                {"incident_id": iid, "action": "chekc the thing",
                                 "owner": "tester", "due": "2027-01-01"})
    fid = json.loads(body)["id"]
    code, body, _ = c.post_json(f"/api/followups/{fid}/edit",
                                {"action": "check the thing", "owner": "someone"})
    fu = json.loads(body) if code == 200 else {}
    check("a follow-up can be edited", code == 200
          and fu.get("action") == "check the thing"
          and fu.get("owner") == "someone", [code, fu.get("action")])
    check("an unmentioned follow-up field survives the edit",
          fu.get("due") == "2027-01-01", fu.get("due"))

    code, _, _ = c.post_json(f"/api/incidents/{iid}/delete", {})
    check("an incident can be deleted", code == 200, code)
    check("deleting an incident takes its follow-ups with it",
          not any(f["id"] == fid for f in store.q("SELECT id FROM followups")),
          [f["id"] for f in store.q("SELECT id FROM followups")])
    code, _, _ = c.post_json(f"/api/incidents/{iid}/delete", {})
    check("deleting a missing incident is a 404, not a crash", code == 404, code)

    # Follow-ups delete on their own too.
    code, body, _ = c.post_json("/api/followups", {"action": "standalone"})
    sfid = json.loads(body)["id"]
    code, _, _ = c.post_json(f"/api/followups/{sfid}/delete", {})
    check("a follow-up can be deleted on its own", code == 200, code)
    check("/done still works after the new routes were added in front of it",
          c.post_json("/api/followups/" + str(
              json.loads(c.post_json("/api/followups",
                                     {"action": "closeme"})[1])["id"]) + "/done",
              {})[0] == 200)

    code, body, _ = c.post_json("/api/changes",
                                {"title": "wrogn", "system": "x", "detail": "y"})
    cid = json.loads(body)["id"]
    code, body, _ = c.post_json(f"/api/changes/{cid}/edit", {"title": "right"})
    ch = json.loads(body) if code == 200 else {}
    check("a change can be edited", code == 200 and ch.get("title") == "right",
          [code, ch.get("title")])
    check("an unmentioned change field survives", ch.get("system") == "x",
          ch.get("system"))
    code, _, _ = c.post_json(f"/api/changes/{cid}/delete", {})
    check("a change can be deleted", code == 200, code)
    check("the deleted change is gone",
          not any(x["id"] == cid for x in store.q("SELECT id FROM changes")))

    # The edit path builds SQL from an allowlist, never from caller keys.
    before = store.one("SELECT COUNT(1) AS n FROM incidents")["n"]
    code, body, _ = c.post_json("/api/incidents", {"title": "allowlist probe"})
    pid = json.loads(body)["id"]
    c.post_json(f"/api/incidents/{pid}/edit",
                {"status": "resolved", "id": 9999, "opened_ts": 0})
    row = store.one("SELECT * FROM incidents WHERE id=?", (pid,))
    check("an edit cannot set columns outside the allowlist",
          row and row["status"] == "open" and row["id"] == pid, row)
    c.post_json(f"/api/incidents/{pid}/delete", {})
    check("the probe cleaned up",
          store.one("SELECT COUNT(1) AS n FROM incidents")["n"] == before)

    print("\nproper dialogs")
    code, page, _ = c.get("/")
    page = page.decode("utf-8", "replace")
    # The browser's prompt() is another application's UI wearing your data,
    # and it cannot hold two fields, a hint, or any validation.
    body = page.split("<script>")[-1]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)       # drop block comments
    body = re.sub(r"(?m)^\s*//.*$", "", body)               # and line comments
    leftover = [ln.strip()[:70] for ln in body.splitlines()
                if ("prompt(" in ln or re.search(r"[^.\w]confirm\(", ln))
                and "placeholder" not in ln]
    check("no prompt() or confirm() left in the app", not leftover, leftover)
    check("there is a real modal", "function modal(o){" in page)
    check("and a confirmation dialog built on it", "function askConfirm(o){" in page)
    check("dialogs validate required fields",
          "is required." in page and "dlgerr" in page)
    check("Escape closes them", "function dlgKey(e)" in page)
    check("the resolve dialog asks in operational terms",
          "Actions taken to restore service" in page
          and "Preventive action" in page, True)
    check("it asks for specifics, not 'fixed it'",
          "Name hosts, addresses and commands" in page)
    check("silences offer a bounded set of durations, not free text",
          '"168","1 week' in page and 'Every silence expires' in page)
    check("deletes explain what goes with them",
          "and its follow-ups will be removed" in page)

    print("\nresolution is recorded, not just the lesson")
    # Closing used to capture only "what would have caught this sooner", so
    # the log kept the lesson and threw away the fix.
    code, body, _ = c.post_json("/api/incidents", {
        "title": "Printers unreachable", "severity": "major",
        "system": "cfp-machine-1", "detail": "queues point at dead addresses"})
    rid = json.loads(body)["id"]
    code, body, _ = c.post_json(f"/api/incidents/{rid}/resolve", {
        "resolution": "Repointed the queue to 192.168.42.177:9100",
        "prevention": "Reserve the printer addresses on the UDM"})
    done = json.loads(body) if code == 200 else {}
    check("resolving records what was done", code == 200
          and "192.168.42.177" in (done.get("resolution") or ""),
          done.get("resolution"))
    check("resolving still records the prevention",
          "UDM" in (done.get("prevention") or ""), done.get("prevention"))
    check("the incident is closed", done.get("status") == "resolved",
          done.get("status"))
    check("the prevention still became a follow-up",
          any("UDM" in (f["action"] or "")
              for f in store.q("SELECT action FROM followups")), True)
    check("the resolution did NOT become a follow-up",
          not any("Repointed" in (f["action"] or "")
                  for f in store.q("SELECT action FROM followups")), True)

    # Both are editable afterwards — the fix is usually written up in a hurry.
    code, body, _ = c.post_json(f"/api/incidents/{rid}/edit",
                                {"resolution": "Repointed on 201 and 202"})
    amended = json.loads(body) if code == 200 else {}
    check("the resolution can be corrected later",
          amended.get("resolution") == "Repointed on 201 and 202",
          amended.get("resolution"))
    check("correcting the resolution leaves the prevention alone",
          "UDM" in (amended.get("prevention") or ""), amended.get("prevention"))

    # Databases already in the wild need the column adding, or every insert
    # throws while the page quietly serves yesterday's rows.
    from store import MIGRATIONS
    check("the new column is in the migration list",
          ("incidents", "resolution", "TEXT") in MIGRATIONS, MIGRATIONS)
    cols = {r["name"] for r in store.q("PRAGMA table_info(incidents)")}
    check("the incidents table has the resolution column",
          "resolution" in cols, sorted(cols))
    c.post_json(f"/api/incidents/{rid}/delete", {})

    print("\naccepted risks")
    # A standing, written decision to live with something broken. Not a
    # silence — silences expire by design, so expressing a permanent decision
    # as one means it quietly comes back.
    from engine import Engine as EngineCls
    acc_inv = {"accepted": [
        {"check": "array test/Array Z",
         "reason": "holds nothing that matters",
         "decided": "2026-10-22", "review": "2027-01-31"},
        {"check": "array test/Array Overdue", "reason": "forgotten",
         "decided": "2026-01-01", "review": "2026-01-02"},
    ]}
    eng2 = EngineCls(cfg, store, Notifier(cfg), acc_inv)
    check("accepted risks are loaded from inventory",
          "array test/Array Z" in eng2.accepted, sorted(eng2.accepted))
    check("an entry with no check name is ignored",
          len(EngineCls(cfg, store, Notifier(cfg),
                        {"accepted": [{"reason": "no name"}]}).accepted) == 0)
    check("no inventory at all is not an error",
          EngineCls(cfg, store, Notifier(cfg)).accepted == {})

    sentA, keepA = [], eng2.note.send
    eng2.note.send = lambda *a, **k: sentA.append(a)
    try:
        store.register("array test/Array Z", "storage", "Disks & arrays",
                       "test", "page")
        eng2.transition("array test/Array Z", "down", "bay 9 failed", "page")
        # Suppresses the message, never the measurement.
        rowz = store.get("array test/Array Z") or {}
        check("an accepted risk still records the transition",
              rowz.get("status") == "down", rowz.get("status"))
        check("an accepted risk still writes an event",
              any(e["name"] == "array test/Array Z"
                  for e in store.q("SELECT name FROM events")), True)
        check("an accepted risk is downgraded to quiet",
              sentA and sentA[0][2] == "quiet", sentA)
        check("the reason travels with it",
              sentA and "accepted risk" in (sentA[0][4] or "")
              and "nothing that matters" in (sentA[0][4] or ""), sentA)
        check("the review date is named in the hold",
              sentA and "2027-01-31" in (sentA[0][4] or ""), sentA)

        # Not accepted -> untouched, still pages.
        store.register("array test/Array Real", "storage", "Disks & arrays",
                       "test", "page")
        eng2.transition("array test/Array Real", "down", "bay 1 failed", "page")
        check("an unaccepted check still pages",
              any(a[0] == "array test/Array Real" and a[2] == "page"
                  for a in sentA), sentA)
    finally:
        eng2.note.send = keepA

    # An acceptance nobody revisits is a forgotten problem, so a past review
    # date has to say so out loud rather than keep quietly suppressing.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        EngineCls(cfg, store, Notifier(cfg), acc_inv)
    warned = buf.getvalue()
    check("an overdue review warns at startup",
          "due for review" in warned and "Array Overdue" in warned,
          warned[:200])
    check("an in-date acceptance does not warn",
          "Array Z" not in warned, warned[:200])

    # The real entry ships in inventory.toml so the ssacli deploy does not
    # page forever about a drive nobody intends to replace.
    real = {a["check"]: a for a in inventory.get("accepted", [])}
    check("proxmox Array A is accepted in the shipped inventory",
          "array proxmox/slot0 Array A" in real, sorted(real))
    check("the shipped acceptance records why and when to revisit",
          real.get("array proxmox/slot0 Array A", {}).get("review")
          and "2I:3:8" in real["array proxmox/slot0 Array A"]["reason"],
          real.get("array proxmox/slot0 Array A"))
    # An array and its member drives are separate checks, so accepting the
    # array alone still paged for the dead bay. Both have to be named.
    check("the failed bay is accepted in its own right",
          "disk proxmox 2I:3:8" in real, sorted(real))
    check("accepting one bay does not accept the others",
          not any(k.startswith("disk proxmox 2I:3:") and k != "disk proxmox 2I:3:8"
                  for k in real), sorted(real))

    print("\ndead-man switch")
    # The only alert path that cannot report its own failure: when it is
    # misconfigured the symptom is silence, which is also what working looks
    # like. So it is worth asserting rather than assuming.
    keep_dm = s["deadman_url"]
    s["deadman_url"] = ""
    engine.deadman()                        # must be a no-op, not a crash
    check("an unset deadman url is a no-op", True)

    s["deadman_url"] = base + "/health"
    engine.deadman()
    check("a working deadman url is pinged without raising", True)

    # If the URL is wrong, the ping must fail quietly. A dead-man switch that
    # takes the check loop down with it is worse than not having one.
    dead_port = free_port()
    s["deadman_url"] = f"http://127.0.0.1:{dead_port}/nothing-here"
    try:
        engine.deadman()
        survived = True
    except Exception:                                            # noqa: BLE001
        survived = False
    check("an unreachable deadman url does not take the loop down", survived)

    s["deadman_url"] = base + "/api/status"     # 401 without a session
    try:
        engine.deadman()
        survived = True
    except Exception:                                            # noqa: BLE001
        survived = False
    check("a rejecting deadman url does not take the loop down", survived)
    s["deadman_url"] = keep_dm

    # And the CLI path, since a switch nobody can test is how this came to be
    # unconfigured for weeks in the first place.
    import app as appmod
    s["deadman_url"] = base + "/health"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        handled = appmod.matrix_cli(cfg, ["--deadman-test"])
    out = buf.getvalue()
    check("--deadman-test is handled as a CLI command", handled is True)
    check("--deadman-test reports the HTTP status it got",
          "HTTP 200" in out, out[:160])

    s["deadman_url"] = ""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        appmod.matrix_cli(cfg, ["--deadman-test"])
    out = buf.getvalue()
    check("--deadman-test explains the setup when nothing is configured",
          "healthchecks.io" in out and "OPS_DEADMAN_URL" in out, out[:160])
    check("--deadman-test warns the watchdog must be off-estate",
          "OUTSIDE this estate" in out, out[:160])
    s["deadman_url"] = keep_dm

    # A typo used to fall through to starting a second portal against the same
    # database. It was only ever caught because the port was already bound.
    old_argv = sys.argv
    sys.argv = ["app.py", cfg_path, "--deadmantest"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = appmod.main()
    finally:
        sys.argv = old_argv
    out = buf.getvalue()
    check("an unknown option is refused, not run as a server", rc == 2, rc)
    check("the refusal names the offending option",
          "--deadmantest" in out, out[:120])
    check("the refusal prints the usage", "--deadman-test" in out, out[:200])
    check("every documented flag is in the allow-list",
          {"--adduser", "--users", "--counters", "--deadman-test",
           "--mail-test"} <= appmod.FLAGS, sorted(appmod.FLAGS))

    # A placeholder is the dangerous state: non-empty, so the startup warning
    # never fires, and the ping fails silently every 60s. Pepper sat like this.
    from engine import placeholder as is_ph
    check("an example value is recognised as a placeholder",
          is_ph("https://hc-ping.com/<your-uuid>") is True)
    check("a real url is not a placeholder",
          is_ph("https://hc-ping.com/1b2c3d4e-5678-90ab-cdef-1234567890ab")
          is False)
    check("empty is not a placeholder", is_ph("") is False and is_ph(None) is False)

    s["deadman_url"] = "https://hc-ping.com/<your-uuid>"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        appmod.matrix_cli(cfg, ["--deadman-test"])
    out = buf.getvalue()
    check("--deadman-test refuses to ping a placeholder",
          "still a placeholder" in out and "HTTP" not in out, out[:160])
    check("--deadman-test explains that .env does not overwrite",
          "FIRST occurrence wins" in out, out[:200])

    # The watchdog gets watched. A placeholder can be any string, so guessing
    # whether the URL looks right does not work — but noticing that the
    # heartbeat is never accepted does. /api/status 401s instantly, which is a
    # failure as far as the ping is concerned and keeps the test quick.
    from engine import DEADMAN
    s["deadman_url"] = base + "/api/status"
    store.register(DEADMAN, "internal", "Alerting", "external heartbeat", "page")
    store.update(DEADMAN, status="up", since=now_ts())
    engine.deadman_fails = 0
    sent3, keep3 = [], engine.note.send
    engine.note.send = lambda *a, **k: sent3.append(a)
    try:
        for _ in range(4):
            engine.deadman()
        check("four failed heartbeats is a blip, not an alert",
              (store.get(DEADMAN) or {}).get("status") == "up",
              (store.get(DEADMAN) or {}).get("status"))
        engine.deadman()
        down = store.get(DEADMAN) or {}
        check("five consecutive failed heartbeats goes down",
              down.get("status") == "down", down.get("status"))
        check("the detail counts the failures",
              "5 consecutive" in (down.get("detail") or ""), down.get("detail"))
        check("a broken heartbeat reaches the notifier",
              any(DEADMAN in a[0] for a in sent3), sent3)

        s["deadman_url"] = base + "/health"
        engine.deadman()
        check("a working heartbeat clears it",
              (store.get(DEADMAN) or {}).get("status") == "up",
              (store.get(DEADMAN) or {}).get("status"))
        check("the failure counter resets on success", engine.deadman_fails == 0)
    finally:
        engine.note.send = keep3

    check("deadman severity defaults to page",
          s["deadman_severity"] == "page", s.get("deadman_severity"))
    store.forget(DEADMAN)
    check("an unconfigured heartbeat leaves no phantom check",
          store.get(DEADMAN) is None)
    s["deadman_url"] = keep_dm

    print("\nconcurrency limits")
    # The busy reply is assembled from its own body, so the two can never drift.
    clen = int(pool.BUSY.split(b"Content-Length: ")[1].split(b"\r\n")[0])
    check("the busy reply declares its real length",
          clen == len(pool.BUSY_BODY) and pool.BUSY.endswith(pool.BUSY_BODY), clen)

    g = pool.Gate(limit=1, wait=0.05)
    check("the gate admits up to its limit", g.acquire() is True)
    check("the gate sheds past its limit", g.acquire() is False)
    check("the gate counts who was turned away", g.turned_away == 1, g.turned_away)
    g.release()
    check("the gate admits again once a slot frees", g.acquire() is True)
    g.release()
    gs = g.snapshot()
    check("the gate reports limit, peak and totals",
          gs["limit"] == 1 and gs["peak_held"] == 1 and gs["admitted"] == 2
          and gs["held"] == 0, gs)

    # Pressure is pure arithmetic, so test it without racing a live pool.
    class _Fake:
        pass

    f = _Fake()
    f._q, f.workers = queue.Queue(), 16
    f.active = 11
    check("no pressure below three quarters of the workers",
          pool.Pool.under_pressure(f) is False, f.active)
    f.active = 12
    check("pressure at three quarters of the workers",
          pool.Pool.under_pressure(f) is True, f.active)
    f.active = 0
    f._q.put_nowait(("waiting",))
    check("anything waiting in the queue is pressure",
          pool.Pool.under_pressure(f) is True)

    # A full queue must answer on the accepting thread and hang up, never
    # silently drop the connection — "busy" has to be distinguishable from
    # "down" by whoever is looking.
    shed = _Fake()
    shed._q = queue.Queue(maxsize=1)
    shed._q.put_nowait(("already", "full", 0.0))
    shed._lock = threading.Lock()
    shed._stop = threading.Event()
    shed.shed_after = 0.0       # no patience, so the test does not wait around
    shed.rejected = shed.peak_queued = 0
    closed = []
    shed.shutdown_request = closed.append
    near, far = socket.socketpair()
    try:
        pool.Pool.process_request(shed, near, ("127.0.0.1", 9))
        far.settimeout(2)
        got = far.recv(4096)
    finally:
        near.close()
        far.close()
    check("a full queue answers 503 with Retry-After",
          got.startswith(b"HTTP/1.1 503") and b"Retry-After: 2" in got, got[:48])
    check("a refused connection is counted", shed.rejected == 1, shed.rejected)
    check("a refused connection is closed, not leaked", closed == [near], closed)

    time.sleep(0.1)
    check("an idle pool reports no pressure", not srv.under_pressure())
    code, body, _ = c.get("/health")
    h = json.loads(body)
    check("health reports the pool without a session",
          isinstance(h.get("http"), dict)
          and h["http"]["workers"] == s["http_workers"], h.get("http"))
    check("health shows traffic served and nothing refused",
          h["http"]["served"] > 0 and h["http"]["rejected"] == 0, h.get("http"))
    check("health shows the queue drained", h["http"]["queued"] == 0, h.get("http"))

    code, body, _ = c.get("/api/runtime")
    r = json.loads(body) if code == 200 else {}
    check("runtime is served to a session", code == 200, code)
    check("runtime reports the login gate",
          r.get("login", {}).get("limit") == s["login_concurrency"], r.get("login"))
    # Proves the gate is really in the login path, not just constructed.
    check("the login gate admitted the sign-in above",
          r.get("login", {}).get("admitted", 0) >= 1, r.get("login"))
    code, _, _ = Client(base).get("/api/runtime")
    check("runtime needs a session", code == 401, code)

    print("\nsigning out")
    code, _, hdr = c.post_form("/logout", {})
    check("logout redirects and clears the cookie",
          code == 303 and "Max-Age=0" in hdr.get("Set-Cookie", ""), code)
    c2 = Client(base)
    code, _, _ = c2.get("/api/status")
    check("a fresh client has no access", code == 401, code)

    srv.shutdown()
    srv.server_close()          # also stops the worker threads
    store.db.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
