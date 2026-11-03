#!/usr/bin/env python3
"""
Elastic Ops — entry point.

    python3 app.py checks.toml                      run it
    python3 app.py checks.toml --adduser siddhant   create an account
    python3 app.py checks.toml --passwd siddhant    change a password
    python3 app.py checks.toml --deluser esti       remove an account
    python3 app.py checks.toml --users              list accounts

    python3 app.py checks.toml --matrix-login      get an alerting access token
    python3 app.py checks.toml --matrix-rooms      list rooms and their IDs
    python3 app.py checks.toml --matrix-test       send one test message
    python3 app.py checks.toml --whatsapp-test     send one test WhatsApp

    python3 app.py checks.toml --gmail-auth        get a Gmail refresh token
    python3 app.py checks.toml --mail-test         send one test email
    python3 app.py checks.toml --deadman-test      ping the dead-man URL once
    python3 app.py checks.toml --counters <ip>     dump a printer's raw counters

Python 3.11+, standard library only. Nothing to pip install, which is the point:
the thing that watches the estate should not itself be a dependency problem.
"""

import os
import sys
import threading

import api
import auth as authmod
import pool
from engine import (Engine, Notifier, load_config, load_inventory, log,
                    placeholder)
from store import Portal, Store


# Every flag the CLI understands, here and in auth.cli(). An unknown one used
# to fall straight through to starting the server: on a host where the service
# happened to be stopped, a typo like `--deadmantest` would quietly bring up a
# second portal against the same database. The only reason it was ever noticed
# is that port 8080 was already taken.
FLAGS = frozenset({
    "--adduser", "--deluser", "--display", "--passwd", "--users",
    "--gmail-auth", "--mail-test", "--deadman-test", "--whatsapp-test",
    "--matrix-login", "--matrix-rooms", "--matrix-test", "--counters",
})


def gmail_auth(cfg):
    import mailer
    s = cfg["settings"]
    mailer.interactive_auth(s.get("gmail_client_id"), s.get("gmail_client_secret"))


def cfg_path_of(args):
    return next((a for a in args if not a.startswith("--")), "checks.toml")


def matrix_cli(cfg, args):
    """Token and room-id plumbing, so nobody has to hand-roll curl against the
    Matrix client API. Returns True if the process should stop afterwards."""
    import getpass

    import matrix as matrixmod
    s = cfg["settings"]
    hs = s.get("matrix_homeserver")

    if "--matrix-login" in args:
        if not hs:
            print("set matrix_homeserver in checks.toml first")
            return True
        user = input("matrix username (without the @ or :server): ").strip()
        try:
            r = matrixmod.login(hs, user, getpass.getpass("password: "))
        except Exception as e:
            print(f"login failed: {e}")
            return True
        print("\nAccess token below. Put it in .env as OPS_MATRIX_TOKEN —")
        print("never in checks.toml, which is in version control.\n")
        print(f"  OPS_MATRIX_TOKEN={r.get('access_token')}\n")
        print(f"  user: {r.get('user_id')}   device: {r.get('device_id')}")
        return True

    if "--matrix-rooms" in args:
        if not (hs and s.get("matrix_token")):
            print("needs matrix_homeserver and OPS_MATRIX_TOKEN")
            return True
        try:
            rooms = matrixmod.joined_rooms(hs, s["matrix_token"])
        except Exception as e:
            print(f"could not list rooms: {e}")
            return True
        if not rooms:
            print("this account has joined no rooms — invite it to one first")
        for rid, name in rooms:
            print(f"  {rid:<48} {name}")
        print("\nPut the chosen id in checks.toml as matrix_room")
        return True

    if "--counters" in args:
        import snmp
        host = args[args.index("--counters") + 1]
        print(f"\nRaw counters from {host}. Compare these against the device's "
              "own\nmeter page (bizhub: Utility > Meter Count) and tell me which "
              "line\nmatches colour and which matches black.\n")
        total = snmp.get(host, ["1.3.6.1.2.1.43.10.2.1.4.1.1"]).get(
            "1.3.6.1.2.1.43.10.2.1.4.1.1")
        print(f"  standard lifetime total         {total}")
        print("\n  vendor scalars (Konica 18334.1.1.1.5.7.2.1.N.0):")
        for n in range(1, 12):
            oid = f"1.3.6.1.4.1.18334.1.1.1.5.7.2.1.{n}.0"
            v = snmp.get(host, [oid]).get(oid)
            if v not in (None, -1):
                print(f"    .{n:<3} {v}")
        print("\n  vendor table, value column (5.7.2.2.1.5.row.sub):")
        rows = snmp.walk(host, "1.3.6.1.4.1.18334.1.1.1.5.7.2.2.1.5", limit=40)
        kinds = dict(snmp.walk(host, "1.3.6.1.4.1.18334.1.1.1.5.7.2.2.1.3", limit=40))
        by_sub = {}
        for oid, val in rows:
            idx = oid.rsplit(".", 2)
            row, sub = idx[-2], idx[-1]
            if isinstance(val, int) and val >= 0:
                by_sub.setdefault(sub, 0)
                by_sub[sub] += val
                k = kinds.get("1.3.6.1.4.1.18334.1.1.1.5.7.2.2.1.3." + row + "." + sub)
                print(f"    row {row} sub {sub}  type={k}  {val}")
        for sub, tot in sorted(by_sub.items()):
            print(f"    -> sub {sub} totals {tot}")
        print("\n  If one of those sub-totals is the colour count on the panel,"
              "\n  that settles it and I will wire it in.\n")
        return True

    if "--gmail-auth" in args:
        gmail_auth(cfg)
        return True

    if "--deadman-test" in args:
        # The one alert path that cannot announce its own failure: if this URL
        # is wrong, the symptom is silence, which is also what success looks
        # like. So it gets a test, like every other channel.
        import urllib.error
        import urllib.request
        url = s["deadman_url"]
        if placeholder(url):
            print(f"deadman_url is still a placeholder: {url}\n")
            print("  This is the dangerous state: it is not empty, so the "
                  "startup warning does not fire, and the\n"
                  "  ping just fails every 60 seconds into the journal.\n")
            print("  Note .env does NOT overwrite a key that is already set — "
                  "the FIRST occurrence wins. Appending a\n"
                  "  second OPS_DEADMAN_URL line does nothing. Edit the "
                  "existing line in place.")
            return True
        if not url:
            print("No deadman_url set, so nothing would notice this dying.\n")
            print("  Make a check at healthchecks.io (or any heartbeat "
                  "service OUTSIDE this estate — a watchdog on pepper cannot\n"
                  "  report that pepper has stopped), then put its ping URL in "
                  ".env as:\n")
            print("    OPS_DEADMAN_URL=https://hc-ping.com/<uuid>\n")
            print("  Set the check's period to 5 minutes with a 10 minute "
                  "grace. The checker pings every 60s, so anything\n"
                  "  shorter than that is a false alarm waiting to happen.")
            return True
        shown = url if len(url) < 60 else url[:57] + "..."
        print(f"Pinging {shown}")
        try:
            with urllib.request.urlopen(url, timeout=s["timeout"]) as r:
                body = r.read(200).decode("utf-8", "replace").strip()
            print(f"  HTTP {r.status} {body!r}")
            print("  Sent. Confirm the check went green, then stop the "
                  "service and confirm it goes red —\n"
                  "  an untested dead-man switch is indistinguishable from "
                  "no dead-man switch.")
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} — the service answered but rejected it. "
                  "Wrong or deleted check UUID?")
        except Exception as e:
            print(f"  failed: {e}")
        return True

    if "--mail-test" in args:
        import mailer
        for cls in (mailer.Gmail, mailer.Smtp):
            m = cls(s)
            if m.configured:
                ok, why = m.send("Elastic Ops", "up", "notify",
                                 "test message - email alerting is wired up")
                print(f"{cls.__name__}: {'sent' if ok else why}")
                return True
        g, sm = mailer.Gmail(s), mailer.Smtp(s)
        print("Neither Gmail nor SMTP is configured.\n")
        print("  Gmail is missing: " + ", ".join(g.missing()))
        print("  SMTP  is missing: " + ", ".join(sm.missing()))
        print("\nValues in brackets come from .env; everything else from "
              "checks.toml.")
        dotenv = os.path.join(os.path.dirname(os.path.abspath(cfg_path_of(args))),
                              os.pardir, ".env")
        print(f"Looked for an env file at: {os.path.normpath(dotenv)}")
        return True

    if "--whatsapp-test" in args:
        import whatsapp as wamod
        w = wamod.WhatsApp(s)
        if not w.configured:
            print("set whatsapp_provider, whatsapp_to and the provider's url/token")
            print("in checks.toml, with the token in .env as OPS_WHATSAPP_TOKEN")
            return True
        ok, why = w.send("Elastic Ops", "up", "notify",
                         "test message - WhatsApp alerting is wired up")
        print("sent" if ok else why)
        return True

    if "--matrix-test" in args:
        m = matrixmod.Matrix(hs, s.get("matrix_room"), s.get("matrix_token"),
                             s.get("timeout", 10))
        if not m.configured:
            print("matrix_homeserver, matrix_room and OPS_MATRIX_TOKEN must all be set")
            return True
        ok, why = m.send("Elastic Ops", "up", "notify",
                         "test message — alerting is wired up")
        print("sent" if ok else why)
        return True

    return False


def main():
    args = sys.argv[1:]
    # Refuse before touching the database or binding a port: starting a second
    # portal is never what someone meant by a flag this does not recognise.
    unknown = [a for a in args if a.startswith("--") and a not in FLAGS]
    if unknown:
        print("unknown option: " + " ".join(unknown) + "\n")
        print((__doc__ or "").strip())
        return 2
    cfg_path = next((a for a in args if not a.startswith("--")), "checks.toml")
    here = os.path.dirname(os.path.abspath(cfg_path)) or "."

    cfg = load_config(cfg_path)
    s = cfg["settings"]
    inventory = load_inventory(os.path.join(here, s.get("inventory", "inventory.toml")))

    # Handled before the database is opened. These are the commands you run on
    # a live host to diagnose something, and none of them needs the store — so
    # none of them should have a second process touching the production file,
    # let alone running its schema and migrations.
    if matrix_cli(cfg, args):
        return

    store = Store(s["db"])
    portal = Portal(store)
    auth = authmod.Auth(store, s["session_hours"], s["cookie_secure"])

    if authmod.cli(auth, args):     # account commands genuinely need the store
        return

    engine = Engine(cfg, store, Notifier(cfg), inventory)

    web_dir = s["web_dir"]
    if not os.path.isabs(web_dir):
        web_dir = os.path.join(here, web_dir)

    handler = api.make_handler(cfg, inventory, store, portal, engine, auth, web_dir)
    srv = pool.Pool(("0.0.0.0", s["listen_port"]), handler,
                    workers=s["http_workers"], backlog=s["http_backlog"],
                    idle=s["http_idle"], shed_after=s["http_shed_after"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    log(f"portal up on :{s['listen_port']} — {len(cfg.get('checks', []))} checks, "
        f"{len(cfg.get('jobs', []))} jobs, {len(inventory.get('hosts', []))} hosts")
    log(f"http pool: {s['http_workers']} workers, queue {s['http_backlog']}, "
        f"{s['login_concurrency']} concurrent logins")
    if not auth.any_users():
        log("WARNING: no accounts exist. Create one: "
            f"python3 app.py {cfg_path} --adduser <name>")
    if not s["deadman_url"]:
        log("WARNING: no deadman_url set. Nothing will tell you if this dies.")
    elif placeholder(s["deadman_url"]):
        log(f"WARNING: deadman_url is still a placeholder ({s['deadman_url']}) "
            "— the ping fails every 60s and nothing will tell you if this dies.")
    if not s["ingest_token"]:
        log("WARNING: no ingest_token set. Backup and host metrics cannot be posted.")
    elif placeholder(s["ingest_token"]):
        log("WARNING: ingest_token is still a placeholder — every collector "
            "post will be rejected with 401.")

    try:
        engine.loop()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    # sys.exit so a refused option is a non-zero status a script can act on.
    # main() returns None on the normal paths, which exits 0.
    sys.exit(main())
