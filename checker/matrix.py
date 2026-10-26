"""
Matrix notifications.

The estate already runs Synapse behind messenger.elasticgroup.co.uk with an
Element client branded "Elastic Group Messenger", so alerts go where the team
already looks rather than into another app nobody opens. End-to-end encryption
is force-disabled on that deployment, which means a bot needs nothing but an
access token — no device verification, no key sharing, no state to lose.

What it needs:

    matrix_homeserver = "https://messenger.elasticgroup.co.uk"
    matrix_room       = "!someroom:chat.internal"
    OPS_MATRIX_TOKEN  = <access token>        (environment, never the file)

Get both with the helpers in app.py:

    python3 app.py checks.toml --matrix-login     # prints an access token
    python3 app.py checks.toml --matrix-rooms     # lists rooms and their IDs

Standard library only, like everything else here.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

COLOUR = {"down": "#BE123C", "up": "#0A7557", "unknown": "#B07E07"}
WORD = {"down": "DOWN", "up": "RECOVERED", "unknown": "UNKNOWN"}


def _call(base, path, token, payload=None, method="GET", timeout=10):
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def login(homeserver, user, password, timeout=10):
    """Password login, returning an access token. Used by the CLI helper so
    nobody has to hand-roll curl against the client API."""
    return _call(homeserver, "/_matrix/client/v3/login", None, {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": password,
        "initial_device_display_name": "elastic-ops",
    }, method="POST", timeout=timeout)


def joined_rooms(homeserver, token, timeout=10):
    rooms = _call(homeserver, "/_matrix/client/v3/joined_rooms", token,
                  timeout=timeout).get("joined_rooms", [])
    out = []
    for rid in rooms:
        try:
            name = _call(homeserver,
                         f"/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}"
                         "/state/m.room.name/", token, timeout=timeout).get("name", "")
        except Exception:
            # Plenty of rooms have no m.room.name at all, which is a 404 here
            # and not a problem — the id is what we actually need.
            name = "(unnamed)"
        out.append((rid, name))
    return out


class Matrix:
    """Posts one message per state change. Never raises into the check loop."""

    def __init__(self, homeserver, room, token, timeout=10):
        self.base = (homeserver or "").rstrip("/")
        self.room = room or ""
        self.token = token or ""
        self.timeout = timeout
        self._n = 0

    @property
    def configured(self):
        return bool(self.base and self.room and self.token)

    def _txn(self):
        self._n += 1
        return f"eo-{int(time.time())}-{self._n}"

    def send(self, name, status, severity, detail):
        if not self.configured:
            return False, "matrix not configured"
        word = WORD.get(status, status.upper())
        colour = COLOUR.get(status, "#5C6879")
        plain = f"[{word}] {name} — {detail}"
        html = (f'<p><strong><font color="{colour}">{word}</font></strong> '
                f"<strong>{_esc(name)}</strong><br>{_esc(detail)}</p>"
                f"<p><code>severity: {_esc(severity)}</code></p>")
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(self.room)}"
                f"/send/m.room.message/{self._txn()}")
        try:
            _call(self.base, path, self.token, {
                "msgtype": "m.text",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            }, method="PUT", timeout=self.timeout)
            return True, "sent"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            return False, f"matrix HTTP {e.code}: {body}"
        except Exception as e:
            return False, f"matrix failed: {e}"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
