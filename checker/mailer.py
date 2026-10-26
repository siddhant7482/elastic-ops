"""
Email alerts, via the Gmail API.

Named mailer.py and not email.py on purpose: a module called email.py in this
directory would shadow the standard library's email package, and the failure
that causes is spectacularly confusing.

AUTHENTICATION, AND WHY IT IS THIS WAY

Google offers two server-side routes. A service account with domain-wide
delegation signs a JWT with RS256, and the standard library cannot sign RSA —
that would mean adding `cryptography` or `google-auth`, breaking the one rule
this project actually has. The OAuth refresh-token flow needs no signing at
all: POST the refresh token, get an access token back. So that is what this
uses, and it stays dependency-free.

Getting a refresh token is a browser consent flow, done once. Run it on a
machine with a browser (your laptop, not pepper):

    python3 app.py checks.toml --gmail-auth

It prints a URL, catches the redirect on localhost, and gives you the refresh
token to put in .env as OPS_GMAIL_REFRESH_TOKEN.

SMTP is also here, because a Workspace app password is three lines of config
against this one's OAuth dance, and some estates have app passwords disabled
while others have OAuth clients locked down. Whichever is less trouble.
"""

import base64
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

WORD = {"down": "DOWN", "up": "RECOVERED", "unknown": "UNKNOWN"}


def compose(name, status, severity, detail, portal_url=""):
    """Subject carries the whole story — most alert mail is read as a
    notification line and never opened."""
    subject = f"[{WORD.get(status, status.upper())}] {name}"
    lines = [f"{name} is {WORD.get(status, status.upper()).lower()}.", "",
             detail or "", "", f"severity: {severity}",
             f"at: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}"]
    if portal_url:
        lines += ["", portal_url]
    return subject, "\n".join(lines)


def build(sender, recipients, subject, body):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = ", ".join(recipients)
    m["Subject"] = subject
    m.set_content(body)
    return m


class Gmail:
    """Gmail API sender. Caches the access token until it is nearly expired."""

    def __init__(self, s):
        self.client_id = s.get("gmail_client_id") or ""
        self.client_secret = s.get("gmail_client_secret") or ""
        self.refresh_token = s.get("gmail_refresh_token") or ""
        self.sender = s.get("mail_from") or ""
        self.to = [a.strip() for a in (s.get("mail_to") or "").split(",") if a.strip()]
        self.portal_url = s.get("portal_url") or ""
        self.timeout = s.get("timeout", 10)
        self._token = ""
        self._expires = 0

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret and self.refresh_token
                    and self.sender and self.to)

    def access_token(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read())
        self._token = payload["access_token"]
        self._expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def send(self, name, status, severity, detail):
        if not self.configured:
            return False, "gmail not configured"
        subject, body = compose(name, status, severity, detail, self.portal_url)
        msg = build(self.sender, self.to, subject, body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        try:
            req = urllib.request.Request(
                SEND_URL, data=json.dumps({"raw": raw}).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {self.access_token()}")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return True, f"sent ({r.status})"
        except urllib.error.HTTPError as e:
            # A 401 here nearly always means the refresh token was revoked —
            # which Google does on password change. Say so, rather than
            # leaving someone to read the raw JSON at 3am.
            body_txt = e.read().decode("utf-8", "replace")[:200]
            hint = (" — the refresh token looks revoked; re-run --gmail-auth"
                    if e.code == 401 else "")
            return False, f"gmail HTTP {e.code}: {body_txt}{hint}"
        except Exception as e:
            return False, f"gmail failed: {e}"


class Smtp:
    """The simpler path: a Workspace app password and smtplib."""

    def __init__(self, s):
        self.host = s.get("smtp_host") or ""
        self.port = int(s.get("smtp_port") or 587)
        self.user = s.get("smtp_user") or ""
        self.password = s.get("smtp_password") or ""
        self.sender = s.get("mail_from") or self.user
        self.to = [a.strip() for a in (s.get("mail_to") or "").split(",") if a.strip()]
        self.portal_url = s.get("portal_url") or ""
        self.timeout = s.get("timeout", 10)

    @property
    def configured(self):
        return bool(self.host and self.user and self.password and self.to)

    def send(self, name, status, severity, detail):
        if not self.configured:
            return False, "smtp not configured"
        subject, body = compose(name, status, severity, detail, self.portal_url)
        msg = build(self.sender, self.to, subject, body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(self.user, self.password)
                s.send_message(msg)
            return True, "sent"
        except smtplib.SMTPAuthenticationError as e:
            return False, (f"smtp auth refused: {e.smtp_code} — with Google this "
                           "usually means an app password is required, or the "
                           "account has 2FA off")
        except Exception as e:
            return False, f"smtp failed: {e}"


# ------------------------------------------------------------------ oauth


def consent_url(client_id, redirect_uri):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without this, Google returns a refresh token only on the FIRST
        # consent ever granted, and silently omits it afterwards — which is
        # a deeply confusing half hour if you have authorised before.
        "prompt": "consent",
    })


def exchange_code(client_id, client_secret, code, redirect_uri, timeout=15):
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def interactive_auth(client_id="", client_secret=""):
    """One-time OAuth consent, catching the redirect on loopback.

    Google disabled the out-of-band "copy this code" flow in 2022, so the only
    route for an installed app is a redirect to localhost. Run this where a
    browser is — a laptop, not a headless hypervisor — then paste the refresh
    token into .env on the host that sends.

    Deliberately importable without the rest of the app: it needs no config
    file, so it runs on any Python 3, including one too old for tomllib.
    """
    import http.server
    import threading
    import webbrowser

    cid = client_id or input("client id: ").strip()
    secret = client_secret or input("client secret: ").strip()
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

    url = consent_url(cid, redirect)
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
        r = exchange_code(cid, secret, got["code"], redirect)
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

