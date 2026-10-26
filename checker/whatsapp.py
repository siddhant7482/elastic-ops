"""
WhatsApp notifications.

No gateway was running on the estate when this was written, so rather than
guess at one API this supports the shapes the realistic options actually use.
Set `whatsapp_provider` and the three or four values it needs.

    waha         devlikeapro/waha            POST {base}/api/sendText
    wppconnect   wppconnect-server           POST {base}/api/{session}/send-message
    evolution    evolution-api               POST {base}/message/sendText/{session}
    twilio       Twilio WhatsApp API         POST .../Messages.json   (form encoded)
    callmebot    callmebot.com               GET  with query params
    generic      anything else               POST {base} with {"to":…, "text":…}

A WARNING WORTH READING. The first three drive a real WhatsApp account over
the WhatsApp Web protocol. They are unofficial: the session drops when the
linked phone is offline too long, and the account can be banned. They are
fine for "notify", and a poor choice for "page" — the alert you most need is
the one that arrives when things are already going wrong.

Worse, a gateway hosted on pepper dies with the portal, so a pepper failure
produces no alert at all. That is exactly the 4 October failure rebuilt. For
paging, prefer something off the estate: Twilio, or the dead-man's switch,
which notifies from outside and cannot fail with what it watches.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

WORD = {"down": "DOWN", "up": "RECOVERED", "unknown": "UNKNOWN"}
MARK = {"down": "⚠", "up": "✓", "unknown": "?"}


def compose(name, status, severity, detail):
    """One line that reads on a phone's lock screen, where only the first
    few words are visible."""
    return (f"{MARK.get(status, '')} {WORD.get(status, status.upper())}: {name}\n"
            f"{detail}\n({severity})")


class WhatsApp:
    def __init__(self, s):
        self.provider = (s.get("whatsapp_provider") or "").strip().lower()
        self.base = (s.get("whatsapp_url") or "").rstrip("/")
        self.token = s.get("whatsapp_token") or ""
        self.session = s.get("whatsapp_session") or "default"
        self.to = s.get("whatsapp_to") or ""
        self.sender = s.get("whatsapp_from") or ""
        self.timeout = s.get("timeout", 10)

    @property
    def configured(self):
        if not self.provider:
            return False
        if self.provider == "callmebot":
            return bool(self.to and self.token)
        if self.provider == "twilio":
            return bool(self.base and self.token and self.to and self.sender)
        return bool(self.base and self.to)

    def _request(self, text):
        """Returns a prepared urllib Request for this provider."""
        p, t = self.provider, self.to

        if p == "callmebot":
            q = urllib.parse.urlencode({"phone": t, "text": text, "apikey": self.token})
            return urllib.request.Request(
                f"https://api.callmebot.com/whatsapp.php?{q}", method="GET"), None

        if p == "twilio":
            # Twilio is form encoded and uses basic auth, unlike every other
            # option here. account_sid:auth_token goes in whatsapp_token.
            body = urllib.parse.urlencode({
                "To": f"whatsapp:{t}", "From": f"whatsapp:{self.sender}", "Body": text,
            }).encode()
            req = urllib.request.Request(self.base, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            import base64
            req.add_header("Authorization", "Basic " +
                           base64.b64encode(self.token.encode()).decode())
            return req, None

        if p == "waha":
            url = f"{self.base}/api/sendText"
            payload = {"session": self.session, "chatId": t, "text": text}
        elif p == "wppconnect":
            url = f"{self.base}/api/{urllib.parse.quote(self.session)}/send-message"
            payload = {"phone": t, "message": text}
        elif p == "evolution":
            url = f"{self.base}/message/sendText/{urllib.parse.quote(self.session)}"
            payload = {"number": t, "text": text}
        else:                                   # generic
            url = self.base
            payload = {"to": t, "text": text}

        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            # wppconnect uses a bearer token; evolution uses its own header.
            if p == "evolution":
                req.add_header("apikey", self.token)
            else:
                req.add_header("Authorization", f"Bearer {self.token}")
        return req, payload

    def send(self, name, status, severity, detail):
        if not self.configured:
            return False, "whatsapp not configured"
        try:
            req, _ = self._request(compose(name, status, severity, detail))
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return True, f"sent ({r.status})"
        except urllib.error.HTTPError as e:
            return False, (f"whatsapp HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:
            return False, f"whatsapp failed: {e}"
