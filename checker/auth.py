"""
Auth — scrypt password hashing and server-side sessions.

Three people on a LAN did not justify standing up Authelia and a Redis beside
this, so login is built in. The surface is deliberately small and boring:

  * Passwords go through hashlib.scrypt with a per-user random salt. No
    reversible storage, no home-made hashing.
  * The cookie holds a random 32-byte token and nothing else. Sessions live in
    SQLite, so logging out or deleting a user ends the session immediately —
    which a self-contained signed token could not do.
  * Every comparison that touches a secret uses hmac.compare_digest.
  * Failed logins back off per client address, so the login form is not a
    free password oracle.

Users are managed from the command line, not from the portal. Adding an account
should take a human with shell access, and there is no "forgot password" to
abuse:

    python3 app.py checks.toml --adduser siddhant --display "Siddhant"
    python3 app.py checks.toml --passwd siddhant
    python3 app.py checks.toml --users
"""

import base64
import getpass
import hashlib
import hmac
import secrets
import time
from http import cookies

from store import now

COOKIE = "eo_session"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MAX_FAILS = 6
LOCKOUT = 300


def _maxmem(n, r, p):
    """OpenSSL refuses scrypt above its own memory ceiling, which defaults to
    32 MB — exactly what n=2**15, r=8 needs, so it fails. Ask for twice the
    requirement rather than tuning the cost down."""
    return max(64 * 1024 * 1024, 256 * n * r * p)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, dklen=32,
                        maxmem=_maxmem(**SCRYPT), **SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(
        s=base64.b64encode(salt).decode(), h=base64.b64encode(dk).decode(), **SCRYPT)


def verify_password(pw: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if kind != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), dklen=32,
                            n=n, r=r, p=p, maxmem=_maxmem(n, r, p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))


class Auth:
    def __init__(self, store, session_hours=12, cookie_secure=False):
        self.s = store
        self.ttl = int(session_hours * 3600)
        self.secure = cookie_secure
        self.fails = {}          # client address -> (count, first_fail_ts)

    # -- accounts --------------------------------------------------------
    def add_user(self, username, password, display=None, role="admin"):
        self.s.x("INSERT INTO users(username,pwhash,display,role,created_ts) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                 "pwhash=excluded.pwhash, display=excluded.display",
                 (username, hash_password(password), display or username, role, now()))

    def delete_user(self, username):
        self.s.x("DELETE FROM users WHERE username=?", (username,))
        self.s.x("DELETE FROM sessions WHERE username=?", (username,))

    def users(self):
        return self.s.q("SELECT username, display, role, created_ts FROM users "
                        "ORDER BY username")

    def any_users(self):
        return bool(self.s.one("SELECT 1 AS x FROM users LIMIT 1"))

    # -- login -----------------------------------------------------------
    def locked(self, addr):
        n, first = self.fails.get(addr, (0, 0))
        if n >= MAX_FAILS and now() - first < LOCKOUT:
            return LOCKOUT - (now() - first)
        if n and now() - first >= LOCKOUT:
            self.fails.pop(addr, None)
        return 0

    def note_fail(self, addr):
        n, first = self.fails.get(addr, (0, 0))
        self.fails[addr] = (n + 1, first or now())

    def login(self, username, password, addr):
        if self.locked(addr):
            return None
        u = self.s.one("SELECT * FROM users WHERE username=?", (username,))
        # Hash even when the user does not exist, so a missing account and a
        # wrong password take the same time.
        ok = verify_password(password, u["pwhash"] if u else
                             "scrypt$32768$8$1$" + base64.b64encode(b"x" * 16).decode()
                             + "$" + base64.b64encode(b"y" * 32).decode())
        if not (u and ok):
            self.note_fail(addr)
            return None
        self.fails.pop(addr, None)
        token = secrets.token_urlsafe(32)
        self.s.x("INSERT INTO sessions(token,username,created_ts,expires_ts) "
                 "VALUES(?,?,?,?)", (token, username, now(), now() + self.ttl))
        return token

    def logout(self, token):
        if token:
            self.s.x("DELETE FROM sessions WHERE token=?", (token,))

    def whoami(self, token):
        if not token:
            return None
        r = self.s.one(
            "SELECT s.token, s.expires_ts, u.username, u.display, u.role "
            "FROM sessions s JOIN users u ON u.username = s.username WHERE s.token=?",
            (token,))
        if not r:
            return None
        if r["expires_ts"] < now():
            self.s.x("DELETE FROM sessions WHERE token=?", (token,))
            return None
        return {"username": r["username"], "display": r["display"], "role": r["role"]}

    # -- cookie plumbing --------------------------------------------------
    def read_cookie(self, header):
        if not header:
            return None
        try:
            c = cookies.SimpleCookie()
            c.load(header)
        except cookies.CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def set_cookie(self, token):
        bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                f"Max-Age={self.ttl}"]
        if self.secure:
            bits.append("Secure")
        return "; ".join(bits)

    def clear_cookie(self):
        return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


# --------------------------------------------------------------- cli


def cli(auth, args):
    """Returns True if the process should exit after running a user command."""
    if "--users" in args:
        rows = auth.users()
        if not rows:
            print("no users yet — create one with --adduser <name>")
        for u in rows:
            print(f"  {u['username']:<16} {u['display'] or '':<22} {u['role']}  "
                  f"created {time.strftime('%Y-%m-%d', time.localtime(u['created_ts']))}")
        return True

    for flag in ("--adduser", "--passwd", "--deluser"):
        if flag not in args:
            continue
        name = args[args.index(flag) + 1]
        if flag == "--deluser":
            auth.delete_user(name)
            print(f"deleted {name}")
            return True
        display = None
        if "--display" in args:
            display = args[args.index("--display") + 1]
        pw = getpass.getpass(f"password for {name}: ")
        if len(pw) < 10:
            print("too short — use at least 10 characters")
            return True
        if pw != getpass.getpass("again: "):
            print("they do not match")
            return True
        auth.add_user(name, pw, display)
        print(f"{'updated' if flag == '--passwd' else 'created'} {name}")
        return True
    return False
