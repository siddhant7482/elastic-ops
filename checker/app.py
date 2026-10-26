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

Python 3.11+, standard library only. Nothing to pip install, which is the point:
the thing that watches the estate should not itself be a dependency problem.
"""

import os
import sys
import threading
import http.server

import api
import auth as authmod
from engine import Engine, Notifier, load_config, load_inventory, log
from store import Portal, Store


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
    cfg_path = next((a for a in args if not a.startswith("--")), "checks.toml")
    here = os.path.dirname(os.path.abspath(cfg_path)) or "."

    cfg = load_config(cfg_path)
    s = cfg["settings"]
    inventory = load_inventory(os.path.join(here, s.get("inventory", "inventory.toml")))

    store = Store(s["db"])
    portal = Portal(store)
    auth = authmod.Auth(store, s["session_hours"], s["cookie_secure"])

    if authmod.cli(auth, args):
        return
    if matrix_cli(cfg, args):
        return

    engine = Engine(cfg, store, Notifier(cfg))

    web_dir = s["web_dir"]
    if not os.path.isabs(web_dir):
        web_dir = os.path.join(here, web_dir)

    handler = api.make_handler(cfg, inventory, store, portal, engine, auth, web_dir)
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", s["listen_port"]), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    log(f"portal up on :{s['listen_port']} — {len(cfg.get('checks', []))} checks, "
        f"{len(cfg.get('jobs', []))} jobs, {len(inventory.get('hosts', []))} hosts")
    if not auth.any_users():
        log("WARNING: no accounts exist. Create one: "
            f"python3 app.py {cfg_path} --adduser <name>")
    if not s["deadman_url"]:
        log("WARNING: no deadman_url set. Nothing will tell you if this dies.")
    if not s["ingest_token"]:
        log("WARNING: no ingest_token set. Backup and host metrics cannot be posted.")

    try:
        engine.loop()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
