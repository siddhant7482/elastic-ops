"""
HTTP layer — static files, login, the JSON API, and ingest from the estate.

Three classes of route, with three different gates:

  open      /health and /checkin/<token>. Jobs call check-in from cron with no
            credentials; the token in the URL is the credential, and the worst
            a leaked one does is mark a job healthy.
  ingest    /api/ingest/* needs the shared ingest_token in a header. Used by
            the collector on cinnamon, which has no browser session.
  session   everything else. No cookie, no data.

Cross-site request forgery is handled by two things together: the session
cookie is SameSite=Lax, and every state-changing API route requires a JSON
content type, which a cross-origin HTML form cannot send without a preflight
the browser will refuse.
"""

import html
import http.server
import json
import mimetypes
import os
import urllib.parse

from engine import in_maintenance, iso, log
from store import now

MAX_BODY = 256 * 1024


def make_handler(cfg, inventory, store, portal, engine, auth, web_dir):
    ingest_token = cfg["settings"]["ingest_token"]

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "elastic-ops"
        sys_version = ""

        # -- plumbing ----------------------------------------------------
        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, dict | list):
                body = json.dumps(body, default=str).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, to, extra=None):
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            for k, v in (extra or {}):
                self.send_header(k, v)
            self.end_headers()

        def _read_body(self):
            """Consume the request body before anything can answer.

            Replying without draining it leaves unread bytes in the socket, and
            closing on top of those sends a reset — so the caller sees a dropped
            connection instead of the 400 or 401 we carefully wrote. The
            collector posting a megabyte of backup rows with a stale token has
            to get the error, not a reset.
            """
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                self._raw = b""
                return True
            self._raw = self.rfile.read(min(n, MAX_BODY + 1))
            if n > MAX_BODY:
                self.close_connection = True   # too big to drain; hang up after replying
                return False
            return True

        def _json(self):
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("expected application/json")
            return json.loads(self._raw or b"{}")

        def _form(self):
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(self._raw.decode("utf-8", "replace")).items()}

        def _addr(self):
            return self.client_address[0] if self.client_address else "?"

        def _user(self):
            return auth.whoami(auth.read_cookie(self.headers.get("Cookie")))

        def _ingest_ok(self):
            if not ingest_token:
                return False
            sent = (self.headers.get("X-Ingest-Token") or "").strip()
            return sent and sent == ingest_token

        # -- static ------------------------------------------------------
        def _static(self, rel):
            root = os.path.realpath(web_dir)
            path = os.path.realpath(os.path.join(root, rel.lstrip("/")))
            if not (path == root or path.startswith(root + os.sep)):
                return self._send(403, {"error": "forbidden"})
            if not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)

        # -- routing -----------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/health":
                return self._send(200, {"ok": True, "at": iso(now())})

            if path.startswith("/checkin/"):
                name = engine.checkin(path.rsplit("/", 1)[-1])
                if name:
                    log(f"check-in: {name}")
                    return self._send(200, {"ok": True, "job": name}, "text/plain")
                return self._send(404, {"ok": False, "error": "unknown token"})

            if path == "/login":
                return self._login_page()

            user = self._user()
            if not user:
                if path.startswith("/api/"):
                    return self._send(401, {"error": "not signed in"})
                return self._redirect("/login")

            if path == "/":
                return self._static("index.html")
            if path.startswith("/assets/"):
                return self._static(path)

            if path.startswith("/api/"):
                return self._api_get(path, qs, user)

            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if not self._read_body():
                return self._send(413, {"error": "body too large"})

            if path == "/login":
                return self._do_login()
            if path == "/logout":
                auth.logout(auth.read_cookie(self.headers.get("Cookie")))
                return self._redirect("/login", [("Set-Cookie", auth.clear_cookie())])

            if path.startswith("/api/ingest/"):
                if not self._ingest_ok():
                    return self._send(401, {"error": "bad or missing ingest token"})
                return self._ingest(path)

            user = self._user()
            if not user:
                return self._send(401, {"error": "not signed in"})
            try:
                return self._api_post(path, user)
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        # -- login -------------------------------------------------------
        def _login_page(self, message=""):
            try:
                with open(os.path.join(web_dir, "login.html"), encoding="utf-8") as fh:
                    page = fh.read()
            except OSError:
                return self._send(500, {"error": "login page missing"})
            note = (f'<p class="err">{html.escape(message)}</p>' if message else "")
            if not auth.any_users():
                note += ('<p class="err">No accounts exist yet. Create one on the host: '
                         '<code>python3 app.py checks.toml --adduser you</code></p>')
            self._send(200, page.replace("<!--MESSAGE-->", note), "text/html; charset=utf-8")

        def _do_login(self):
            try:
                f = self._form()
            except ValueError:
                return self._login_page("Could not read that.")
            wait = auth.locked(self._addr())
            if wait:
                return self._login_page(f"Too many attempts. Try again in {wait}s.")
            token = auth.login((f.get("username") or "").strip(),
                               f.get("password") or "", self._addr())
            if not token:
                log(f"failed login for '{f.get('username', '')}' from {self._addr()}")
                return self._login_page("Wrong username or password.")
            log(f"signed in: {f.get('username')} from {self._addr()}")
            return self._redirect("/", [("Set-Cookie", auth.set_cookie(token))])

        # -- read API ----------------------------------------------------
        def _api_get(self, path, qs, user):
            if path == "/api/me":
                return self._send(200, user)

            if path == "/api/status":
                rows = store.all_state()
                for r in rows:
                    r["history"] = store.history(r["name"])
                    r["uptime30"] = store.uptime(r["name"])
                    r["since_iso"] = iso(r["since"])
                    r["last_run_iso"] = iso(r["last_run"])
                    r["last_ok_iso"] = iso(r["last_ok"])
                checks = [r for r in rows if r["kind"] == "check"]
                jobs = [r for r in rows if r["kind"] == "job"]
                return self._send(200, {
                    "generated": iso(now()),
                    "maintenance": in_maintenance(cfg),
                    "deadman_configured": bool(cfg["settings"]["deadman_url"]),
                    "webhook_configured": bool(cfg["settings"]["webhook_url"]),
                    "notify_hours": cfg["settings"].get("notify_hours", ""),
                    "notify_days": cfg["settings"].get("notify_days", []),
                    "off_hours_now": engine.note.off_hours(),
                    "summary": {
                        "checks": len(checks),
                        "up": sum(1 for r in checks if r["status"] == "up"),
                        "down": sum(1 for r in checks if r["status"] == "down"),
                        "unknown": sum(1 for r in checks if r["status"] == "unknown"),
                        "jobs": len(jobs),
                        "jobs_late": sum(1 for r in jobs if r["status"] == "down"),
                    },
                    "daily": store.daily_uptime(30),
                    "items": rows,
                })

            if path == "/api/events":
                evs = store.recent_events(int(qs.get("limit", [150])[0]))
                for e in evs:
                    e["ts_iso"] = iso(e["ts"])
                return self._send(200, {"events": evs})

            if path == "/api/incidents":
                return self._send(200, {"incidents": portal.incidents()})
            if path == "/api/followups":
                return self._send(200, {"followups": portal.followups()})
            if path == "/api/changes":
                return self._send(200, {"changes": portal.changes()})
            if path == "/api/backups":
                return self._send(200, {"backups": portal.backups(),
                                        "drills": inventory.get("drills", [])})

            if path == "/api/capacity":
                out = []
                for p in portal.pools():
                    per_week, days = portal.pool_growth(p["host"], p["name"])
                    pct = (100.0 * p["used_bytes"] / p["total_bytes"]
                           if p["total_bytes"] else None)
                    out.append({**p,
                                "pct": round(pct, 1) if pct is not None else None,
                                "growth_per_week": per_week,
                                "days_to_full": days})
                return self._send(200, {"pools": out})

            if path == "/api/hosts":
                hosts = portal.host_latest()
                for h in hosts:
                    h["series"] = portal.host_series(h["host"])
                return self._send(200, {"hosts": hosts,
                                        "inventory": inventory.get("hosts", []),
                                        "drives": inventory.get("drives", [])})

            if path == "/api/coverage":
                rows = portal.coverage(cfg.get("checks", []))
                ignore = {str(x) for x in inventory.get("unmonitored_ok", [])}
                for r in rows:
                    r["ignored"] = r["vmid"] in ignore or r["key"] in ignore
                gaps = [r for r in rows
                        if not r["watched_by"] and not r["ignored"]
                        and r["status"] == "running"]
                return self._send(200, {
                    "guests": rows,
                    "total": len(rows),
                    "running": sum(1 for r in rows if r["status"] == "running"),
                    "watched": sum(1 for r in rows if r["watched_by"]),
                    "ignored": sum(1 for r in rows if r["ignored"]),
                    "gaps": gaps,
                })

            if path == "/api/certs":
                rows = store.certs()
                return self._send(200, {
                    "certs": rows,
                    "expired": sum(1 for r in rows if r["expired"]),
                    "renew_soon": sum(1 for r in rows if r["renew_soon"]),
                    "unreadable": sum(1 for r in rows if r["not_after"] is None),
                })

            if path == "/api/inventory":
                return self._send(200, {
                    "hosts": inventory.get("hosts", []),
                    "drives": inventory.get("drives", []),
                    "apps": inventory.get("apps", []),
                    "credentials": inventory.get("credentials", []),
                    "blast": inventory.get("blast", []),
                    "routing": inventory.get("routing", []),
                    "patching": inventory.get("patching", []),
                    "maintenance": cfg.get("maintenance", []),
                })

            return self._send(404, {"error": "not found"})

        # -- write API ---------------------------------------------------
        def _api_post(self, path, user):
            who = user["display"] or user["username"]

            if path == "/api/incidents":
                d = self._json()
                title = (d.get("title") or "").strip()
                if not title:
                    raise ValueError("a title is required")
                inc = portal.add_incident(
                    title, (d.get("severity") or "major").lower(),
                    (d.get("system") or "").strip(), (d.get("detail") or "").strip(), who)
                log(f"incident opened by {who}: {inc['ref']} {title}")
                return self._send(201, inc)

            if path.startswith("/api/incidents/") and path.endswith("/resolve"):
                iid = int(path.split("/")[3])
                d = self._json()
                inc = portal.resolve_incident(iid, (d.get("prevention") or "").strip(), who)
                if not inc:
                    return self._send(404, {"error": "no such incident"})
                log(f"incident resolved by {who}: {inc['ref']}")
                return self._send(200, inc)

            if path == "/api/followups":
                d = self._json()
                action = (d.get("action") or "").strip()
                if not action:
                    raise ValueError("an action is required")
                fid = portal.add_followup(d.get("incident_id"), action,
                                          d.get("owner") or who, d.get("due"))
                return self._send(201, {"id": fid})

            if path.startswith("/api/followups/"):
                fid = int(path.split("/")[3])
                portal.set_followup(fid, "done" if path.endswith("/done") else "open")
                return self._send(200, {"ok": True})

            if path == "/api/changes":
                d = self._json()
                title = (d.get("title") or "").strip()
                if not title:
                    raise ValueError("a title is required")
                cid = portal.add_change(title, d.get("who") or who,
                                        d.get("system"), d.get("detail"))
                return self._send(201, {"id": cid})

            return self._send(404, {"error": "not found"})

        # -- ingest ------------------------------------------------------
        def _ingest(self, path):
            try:
                d = self._json()
            except ValueError as e:
                return self._send(400, {"error": str(e)})

            if path == "/api/ingest/backups":
                n = portal.put_backups(d.get("backups", []))
                log(f"ingest: {n} backup rows")
                return self._send(200, {"ok": True, "rows": n})

            if path == "/api/ingest/host":
                host = d.get("host")
                if not host:
                    return self._send(400, {"error": "host is required"})
                portal.put_host(host, d)
                n = portal.put_pools(host, d.get("pools", []))
                g = 0
                # Only the daily run sends guests; the ten-minute metrics post
                # omits the key entirely and must not wipe the list.
                if "guests" in d:
                    g = portal.put_guests(host, d["guests"])
                    log(f"ingest: {host} — {g} guests")
                return self._send(200, {"ok": True, "pools": n, "guests": g})

            return self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass                      # the engine does its own logging

    return H
