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

Python 3.11+, standard library only. Nothing to pip install, which is the point:
the thing that watches the estate should not itself be a dependency problem.
"""

import os
import sys
import time
import urllib.parse
import threading
import http.server

import api
import auth as authmod
from engine import Engine, Notifier, load_config, load_inventory, log
from store import Portal, Store


def gmail_auth(cfg):
    """One-time OAuth consent, catching the redirect on loopback.

    Google disabled the out-of-band "copy this code" flow in 2022, so the
    only route for an installed app is a redirect to localhost. That means
    running this where a browser is — your laptop, not pepper — and then
    pasting the refresh token into pepper's .env.
    """
    import http.server
    import threading
    import webbrowser

    import mailer

    s = cfg["settings"]
    cid = s.get("gmail_client_id") or input("client id: ").strip()
    secret = s.get("gmail_client_secret") or input("client secret: ").strip()
    if not (cid and secret):
        print("a client id and secret are required — make an OAuth client of "
              "type 'Desktop app' in the Google Cloud console")
        return

    got = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Done. You can close this tab and return "
                             b"to the terminal.</h2>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.handle_request, daemon=True).start()

    url = mailer.consent_url(cid, redirect)
    print(f"\nAdd this EXACT redirect URI to the OAuth client first:\n  {redirect}\n")
    print(f"Then open:\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        # Headless machines have no browser; the URL is printed above, which
        # is the only part that matters.
        print("(could not open a browser here — use the link above)")
    print("waiting for the redirect...")

    for _ in range(300):
        if got:
            break
        time.sleep(1)
    srv.server_close()

    if "error" in got:
        print(f"refused: {got['error']}")
        return
    if "code" not in got:
        print("timed out waiting for consent")
        return
    try:
        r = mailer.exchange_code(cid, secret, got["code"], redirect)
    except Exception as e:
        print(f"could not exchange the code: {e}")
        return
    rt = r.get("refresh_token")
    if not rt:
        print("Google returned no refresh token. That happens when the app was "
              "already authorised; revoke it at myaccount.google.com/permissions "
              "and run this again.")
        return
    print("\nPut these in .env on pepper:\n")
    print(f"  OPS_GMAIL_CLIENT_SECRET={secret}")
    print(f"  OPS_GMAIL_REFRESH_TOKEN={rt}\n")
    print("and set gmail_client_id, mail_from and mail_to in checks.toml")


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

    if "--gmail-auth" in args:
        gmail_auth(cfg)
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
        print("neither Gmail nor SMTP is configured — see checks.toml")
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
