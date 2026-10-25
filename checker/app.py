#!/usr/bin/env python3
"""
Elastic Ops — entry point.

    python3 app.py checks.toml                      run it
    python3 app.py checks.toml --adduser siddhant   create an account
    python3 app.py checks.toml --passwd siddhant    change a password
    python3 app.py checks.toml --deluser esti       remove an account
    python3 app.py checks.toml --users              list accounts

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
