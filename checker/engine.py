"""
Engine — config, maintenance windows, notifications, the check loop.

Four decisions here are the whole point of the thing, so they are worth stating:

  * Alerts fire on the Nth CONSECUTIVE failure, never the first. A single
    dropped packet should not wake anyone.
  * Recovery is always announced. Being told something broke and never that it
    came back is how people stop trusting a channel.
  * Maintenance windows suppress alerts but still record results, so the
    02:00-04:00 backup timeouts don't train you to ignore the channel.
  * The checker pings an external dead-man's switch every cycle. On 4 October
    the monitoring was on the host that froze, so nothing reported it. Only
    something outside the estate can tell you the estate's watchman has died.
"""

import json
import os
import time
import tomllib
import urllib.request
from datetime import datetime, timezone

import mailer
import matrix as matrixmod
import printers as prt
import probes
import whatsapp as wamod
from store import now

DEADMAN = "Dead-man switch"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"{iso(now())} {msg}", flush=True)


# --------------------------------------------------------------- config


# Settings that are secrets. The file in the repository holds empty strings;
# the real values arrive from the environment, so nothing sensitive is ever
# committed and rotating one does not mean editing a tracked file.
ENV_OVERRIDES = {
    "ingest_token": "OPS_INGEST_TOKEN",
    "webhook_url": "OPS_WEBHOOK_URL",
    "webhook_header": "OPS_WEBHOOK_HEADER",
    "deadman_url": "OPS_DEADMAN_URL",
    "matrix_token": "OPS_MATRIX_TOKEN",
    "whatsapp_token": "OPS_WHATSAPP_TOKEN",
    "gmail_client_secret": "OPS_GMAIL_CLIENT_SECRET",
    "gmail_refresh_token": "OPS_GMAIL_REFRESH_TOKEN",
    "smtp_password": "OPS_SMTP_PASSWORD",
    # Not a secret — the container wants /data, a systemd unit wants
    # /var/lib/elastic-ops, and neither should have to edit a tracked file.
    "db": "OPS_DB",
    "listen_port": "OPS_PORT",
    # Tunable without a deploy, because the right number is whatever the box
    # turns out to stand up to.
    "http_workers": "OPS_HTTP_WORKERS",
    "http_backlog": "OPS_HTTP_BACKLOG",
    "login_concurrency": "OPS_LOGIN_CONCURRENCY",
}


def load_dotenv(*candidates):
    """Read KEY=value files into the environment, without overwriting anything
    already set.

    systemd hands the service its EnvironmentFile, but a human running the CLI
    by hand gets nothing — so --mail-test reported "not configured" about a
    perfectly good .env sitting two directories up. Loading it here makes the
    command line and the service see the same world.
    """
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            # Anything already in the environment wins: systemd and an
            # explicit export are both more deliberate than a file.
            if k and k not in os.environ:
                os.environ[k] = v
        return path
    return None


def placeholder(value):
    """True if a setting is still the example value out of .env.example.

    This matters more than it looks. An empty setting trips the startup
    warning; a placeholder like "https://hc-ping.com/<your-uuid>" reads as
    configured, so the warning never fires and the failure is silent. Pepper
    pinged that exact string every 60 seconds for weeks, logging a failure
    each time, and nothing escalated it — because as far as the code was
    concerned the dead-man switch was set up.
    """
    v = str(value or "")
    return "<" in v and ">" in v


def load_config(path):
    here = os.path.dirname(os.path.abspath(path)) or "."
    load_dotenv(os.path.join(here, ".env"),
                os.path.join(here, os.pardir, ".env"))
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    s = cfg.setdefault("settings", {})
    s.setdefault("db", "/data/ops.db")
    s.setdefault("listen_port", 8080)
    s.setdefault("default_interval", 60)
    s.setdefault("default_retries", 3)
    s.setdefault("timeout", 5)
    s.setdefault("webhook_url", "")
    s.setdefault("webhook_header", "")
    s.setdefault("deadman_url", "")
    s.setdefault("gmail_client_id", "")
    s.setdefault("gmail_client_secret", "")
    s.setdefault("gmail_refresh_token", "")
    s.setdefault("smtp_host", "")
    s.setdefault("smtp_port", 587)
    s.setdefault("smtp_user", "")
    s.setdefault("smtp_password", "")
    s.setdefault("mail_from", "")
    s.setdefault("mail_to", "")
    s.setdefault("portal_url", "")
    s.setdefault("printer_interval", 300)   # seconds between printer polls
    # Severity for array and disk faults. "page" by default: a degraded array
    # is the one failure where the clock is already running — the second disk
    # is what costs the data, and nobody chooses when that happens.
    s.setdefault("storage_severity", "page")
    # A broken heartbeat means a total failure of this host would go
    # unnoticed. That is the one thing the portal cannot report after
    # the fact, so it is a page.
    s.setdefault("deadman_severity", "page")
    s.setdefault("whatsapp_provider", "")
    s.setdefault("whatsapp_url", "")
    s.setdefault("whatsapp_token", "")
    s.setdefault("whatsapp_session", "default")
    s.setdefault("whatsapp_to", "")
    s.setdefault("whatsapp_from", "")
    s.setdefault("matrix_homeserver", "")
    s.setdefault("matrix_room", "")
    s.setdefault("matrix_token", "")
    s.setdefault("notify_hours", "")      # "08:00-18:00", empty = any hour
    s.setdefault("notify_days", DAYS)
    s.setdefault("ingest_token", "")
    s.setdefault("web_dir", "web")
    s.setdefault("session_hours", 12)
    s.setdefault("cookie_secure", False)
    # Concurrency limits. The store serialises every query behind one mutex
    # that the check loop also takes, so throughput tops out around 900 req/s
    # whatever this is set to. Measured against 200 concurrent clients, 8 gave
    # the best pair of numbers — 239 ms request p99 and 6.6 ms of check-loop
    # wait. Raising it to 32 left throughput flat and pushed the loop to 45 ms.
    # More workers here do not buy speed; they only make checks run late.
    s.setdefault("http_workers", 8)
    s.setdefault("http_backlog", 256)     # connections queued for a worker
    s.setdefault("http_idle", 5)          # seconds a stalled socket may hold a worker
    s.setdefault("http_shed_after", 5)    # seconds the queue may stay full before 503s
    # scrypt at n=2**15 needs 128*n*r bytes — exactly 32 MB — every time, and
    # auth.login pays it even for a username that does not exist. Two at once
    # caps that at 64 MB on a host whose spare memory belongs to guests.
    s.setdefault("login_concurrency", 2)
    # Kept short on purpose: a login waiting for a slot is still holding one of
    # the http_workers, so this is also the longest a burst of sign-ins can tie
    # up the pool. One second admits any realistic burst (two hashes per ~90 ms,
    # so about twenty) and sheds the rest rather than blocking the dashboard.
    s.setdefault("login_wait", 1)
    for c in cfg.get("checks", []):
        c.setdefault("group", "Other")
        c.setdefault("interval", s["default_interval"])
        c.setdefault("retries", s["default_retries"])
        c.setdefault("severity", "notify")
    for j in cfg.get("jobs", []):
        j.setdefault("grace_hours", 26)
        j.setdefault("severity", "notify")
    for key, var in ENV_OVERRIDES.items():
        val = os.environ.get(var)
        if not val:
            continue
        # Everything from the environment is a string; keep the type the rest
        # of the code already expects, or binding the port fails at startup.
        s[key] = int(val) if isinstance(s.get(key), int) else val
    return cfg


def load_inventory(path):
    """Hosts, drives, apps, credentials, blast radius, routing. Missing file is
    not an error — the portal just shows those sections empty."""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        log(f"no inventory at {path} — estate pages will be empty")
        return {}


# --------------------------------------------------------------- windows


def in_maintenance(cfg):
    t = datetime.now()
    today = DAYS[t.weekday()]
    hhmm = t.strftime("%H:%M")
    for w in cfg.get("maintenance", []):
        if not w.get("active", True):
            continue
        if today not in [d.lower() for d in w.get("days", DAYS)]:
            continue
        start, end = w.get("start", "00:00"), w.get("end", "00:00")
        inside = (start <= hhmm < end) if start <= end else (hhmm >= start or hhmm < end)
        if inside:
            return w.get("name", "maintenance")
    return None


# --------------------------------------------------------------- notify


class Notifier:
    """Severity decides who gets woken, and when.

      page    sent at any hour. Keep this list to about five things; that it
              stays short is the only reason the channel stays worth reading.
      notify  sent inside notify_hours on notify_days, held outside them.
      quiet   recorded, never sent.

    Until this existed the Alerting page described these rules and the code
    posted everything regardless, which is worse than having no rules — you
    would believe you had configured something.
    """

    def __init__(self, cfg):
        s = cfg["settings"]
        self.url = s["webhook_url"]
        self.header = s["webhook_header"]
        self.timeout = s["timeout"]
        self.hours = s.get("notify_hours", "")
        self.days = [d.lower() for d in s.get("notify_days", DAYS)]
        self.matrix = matrixmod.Matrix(s.get("matrix_homeserver"),
                                       s.get("matrix_room"),
                                       s.get("matrix_token"), s["timeout"])
        self.whatsapp = wamod.WhatsApp(s)
        # Gmail first; SMTP is the same destination by a simpler road, so
        # configuring both would send everything twice.
        self.mail = mailer.Gmail(s)
        if not self.mail.configured:
            self.mail = mailer.Smtp(s)

    def off_hours(self, at=None):
        """True when a 'notify' alert should be held. Empty notify_hours
        means around the clock, so nothing is ever held."""
        if not self.hours or "-" not in self.hours:
            return False
        t = at or datetime.now()
        if DAYS[t.weekday()] not in self.days:
            return True
        start, _, end = self.hours.partition("-")
        hhmm = t.strftime("%H:%M")
        inside = (start <= hhmm < end) if start <= end else (hhmm >= start or hhmm < end)
        return not inside

    def withhold(self, severity, window, at=None):
        """Why this will not be sent, or None if it will be."""
        if severity == "quiet":
            return "quiet severity"
        if window:
            return window
        if severity != "page" and self.off_hours(at):
            return f"outside notify hours ({self.hours})"
        return None

    def send(self, name, status, severity, detail, window):
        word = {"up": "RECOVERED", "down": "DOWN", "unknown": "UNKNOWN"}[status]
        text = f"[{word}] {name} — {detail}"
        held = self.withhold(severity, window)
        if held:
            text += f"  (not sent: {held})"
        log(text)
        if held:
            return

        for channel in (self.mail, self.whatsapp, self.matrix):
            if channel.configured:
                ok, why = channel.send(name, status, severity, detail)
                if not ok:
                    log(why)

        if not self.url:
            return
        body = json.dumps({"name": name, "status": status, "severity": severity,
                           "detail": detail, "text": text, "at": iso(now())}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        if self.header and ":" in self.header:
            k, _, v = self.header.partition(":")
            req.add_header(k.strip(), v.strip())
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as e:
            log(f"webhook failed: {e}")


# --------------------------------------------------------------- engine


class Engine:
    def __init__(self, cfg, store, notifier, inventory=None):
        self.cfg, self.store, self.note = cfg, store, notifier
        # Risks someone has looked at and decided to live with. Kept in
        # inventory.toml rather than as silences, because a silence expires by
        # design and this is a standing decision — but kept with a review date,
        # because an acceptance nobody revisits is just a forgotten problem.
        self.accepted = {a["check"]: a
                         for a in (inventory or {}).get("accepted", [])
                         if a.get("check")}
        today = iso(now())[:10]
        for name, a in self.accepted.items():
            if a.get("review") and a["review"] < today:
                log(f"WARNING: accepted risk '{name}' was due for review on "
                    f"{a['review']} — it is still suppressing alerts.")
        self.next_run = {}
        self.next_cert = {}
        names = []
        for c in cfg.get("checks", []):
            store.register(c["name"], "check", c["group"], c["target"], c["severity"])
            names.append(c["name"])
        for j in cfg.get("jobs", []):
            store.register(j["name"], "job", "Jobs",
                           f"every {j['grace_hours']}h", j["severity"])
            names.append(j["name"])
        store.forget_missing(names)
        self.deadman_fails = 0
        # The watchdog gets watched. Not circular: the external heartbeat
        # catches this whole host dying, while this catches the heartbeat
        # itself being misconfigured — different failures, both real.
        if cfg["settings"]["deadman_url"]:
            store.register(DEADMAN, "internal", "Alerting", "external heartbeat",
                           cfg["settings"]["deadman_severity"])
        else:
            store.forget(DEADMAN)
        self.printers = cfg.get("printers", [])
        self.next_full_printer = 0
        self.printer_fails = {}
        store.forget_printers([p["host"] for p in self.printers if p.get("host")])
        self.next_printer = 0

    def transition(self, name, new_status, detail, severity):
        st = self.store.get(name) or {}
        old = st.get("status", "unknown")
        if old == new_status:
            return
        self.store.update(name, status=new_status, since=now(), detail=detail)
        self.store.event(name, old, new_status, detail)
        if old == "unknown" and new_status == "up":
            return                      # the first successful check is not news
        # An accepted risk outranks both silences and windows: it is a written
        # decision, not a temporary hush. proxmox's Array A is degraded on
        # purpose and will be until VM 1001 is deleted — paging about it would
        # make the board permanently red, which is how a board stops being
        # read. Recorded and shown, never sent.
        acc = self.accepted.get(name)
        if acc:
            hold = "accepted risk"
            if acc.get("reason"):
                hold += f" — {acc['reason']}"
            if acc.get("review"):
                hold += f" (review due {acc['review']})"
            self.note.send(name, new_status, "quiet", detail, hold)
            return
        # A silence stops the message, never the measurement: the transition is
        # already recorded above and still shows in the portal.
        sil = self.store.silenced(name)
        if sil:
            hold = f"silenced until {iso(sil['until_ts'])}"
            if sil["reason"]:
                hold += f" — {sil['reason']}"
        else:
            mw = in_maintenance(self.cfg)
            hold = f"maintenance window: {mw}" if mw else None
        self.note.send(name, new_status, severity, detail, hold)

    def refresh_cert(self, c):
        """Certificates move slowly, so look every six hours rather than every
        cycle — this opens a TLS session, which is not free."""
        if c["type"] != "https":
            return
        host, port = probes.hostport_of(c["target"])
        if not host:
            return
        info, detail = probes.cert_info(host, port, self.cfg["settings"]["timeout"])
        self.store.put_cert(c["name"], host, port, info, detail)

    def poll_printers(self):
        """How often is a trade-off, not a free choice: SNMP on a sleeping
        printer takes a few seconds and the walk touches several tables, so
        this runs on its own timer rather than every cycle. printer_interval
        in checks.toml sets it."""
        # The full read is sixty-odd round trips; the light one is thirteen.
        # Static facts — serial, trays, covers, storage — do not change
        # between polls, so they are refreshed hourly and everything that
        # moves is read every cycle.
        full = now() >= self.next_full_printer
        if full:
            self.next_full_printer = now() + 3600

        for p in self.printers:
            host = p.get("host")
            if not host:
                continue
            data, err = prt.read(host, p.get("community", "public"),
                                 self.cfg["settings"]["timeout"] + 2, full=full)
            if err:
                # One missed poll is not a dead printer. Keep the last good
                # reading and only call it unreachable after three in a row,
                # the same rule the service checks use.
                fails = self.printer_fails.get(host, 0) + 1
                self.printer_fails[host] = fails
                grace = p.get("offline_polls", 12)
                if fails < grace:
                    log(f"printer {host}: {err} (quiet poll {fails} of {grace})")
                    continue
                # Silent on every port too: almost certainly deep sleep, which
                # is normal overnight. Say so rather than crying wolf nightly.
                sleeping = prt.asleep(host)
                prev = self.store.one("SELECT payload, pages FROM printers "
                                      "WHERE host=?", (host,)) or {}
                try:
                    keep = json.loads(prev.get("payload") or "{}")
                except ValueError:
                    keep = {}
                self.store.put_printer(
                    host, p.get("name") or host, "", p.get("location", ""),
                    "asleep" if sleeping else "unreachable",
                    "dim" if sleeping else "bad",
                    ("not answering on any port — most likely deep sleep"
                     if sleeping else f"{err} ({fails} polls)"),
                    prev.get("pages"), keep, None if sleeping else err)
                continue
            self.printer_fails[host] = 0
            prev = self.store.one("SELECT payload FROM printers WHERE host=?",
                                  (host,)) or {}
            try:
                prev = json.loads(prev.get("payload") or "{}")
            except ValueError:
                prev = {}
            state, detail = prt.worst(data, p.get("warn_percent", 20),
                                      p.get("bad_percent", 10))
            self.store.put_printer(
                host, p.get("name") or data["name"], data["model"],
                # The device usually knows where it is; config only overrides
                # it when someone has bothered to fill that in.
                p.get("location") or data.get("location", ""),
                data["status"], state, detail, data["pages"],
                {"supplies": data["supplies"],
                 "trays": data.get("trays", prev.get("trays", [])),
                 "errors": data["errors"], "descr": data["descr"],
                 "console": data.get("console", prev.get("console", "")),
                 "covers": data.get("covers", prev.get("covers", [])),
                 "alerts": data.get("alerts", prev.get("alerts", [])),
                 "colorants": data.get("colorants", prev.get("colorants", [])),
                 "storage": data.get("storage", prev.get("storage", [])),
                 "contact": data.get("contact", ""),
                 "uptime_s": data.get("uptime_s", 0)},
                None, data.get("serial", ""))
            self.store.put_printer_history(
                host, data["pages"],
                {s["name"]: s["percent"] for s in data["supplies"]
                 if s["percent"] is not None})

    def sweep_storage(self):
        """Alert on array and disk integrity — which nothing else here did.

        Capacity was already watched; integrity was not, and the two fail
        differently. The portal would have reported local-lvm at 40% used on
        a RAID 0 right up to the moment a single disk took the lot.

        Deliberately NOT an alert: an array having no redundancy at all. That
        is a standing property of how the box was built, not an event, and a
        row that is red from the day it appears is a row people learn to scroll
        past. It is shown plainly on the page and left out of the notifier.
        """
        sev = self.cfg["settings"]["storage_severity"]
        live = []

        for a in self.store.arrays():
            name = f"array {a['host']}/{a['name']}"
            live.append(name)
            self.store.register(name, "storage", "Disks & arrays",
                                f"{a['host']} {a['level'] or '?'}", sev)
            try:
                members = json.loads(a.get("members") or "[]")
            except ValueError:
                members = []
            # Three vocabularies land in this one field: mdraid says "faulty",
            # ZFS says "FAULTED"/"UNAVAIL", and HPE says "Failed" or
            # "Predictive Failure". Matching on all of them is what lets the
            # alert name the actual bay instead of just a count.
            bad = [m for m in members
                   if any(w in (m.get("state") or "").upper()
                          for w in ("FAULTY", "FAULTED", "UNAVAIL", "REMOVED",
                                    "OFFLINE", "FAILED", "PREDICTIVE"))]
            state = (a.get("state") or "").strip()
            degraded = int(a.get("degraded") or 0)

            if degraded or state in ("DEGRADED", "FAULTED") or bad:
                why = []
                # Lead with the backend's own words where it has any. HPE's
                # "Interim Recovery Mode" says more than any count could, and
                # mdraid says "clean" even while degraded, so a healthy-looking
                # state is dropped rather than printed as reassurance.
                if state and state.lower() not in ("clean", "active", "ok",
                                                   "online"):
                    why.append(state)
                if bad:
                    why.append("faulty: " + ", ".join(
                        m.get("dev", "?") for m in bad))
                elif degraded:
                    # No member named as bad, yet the array is short — the disk
                    # is not failed, it is gone.
                    why.append(f"{degraded} of {a['disks']} disks missing")
                if not a["redundant"]:
                    # Degraded with no redundancy is not "repair it soon".
                    why.append("NO REDUNDANCY — data is already at risk")
                self.transition(name, "down",
                                f"{a['level']} — " + "; ".join(why), sev)
            elif a.get("sync_action") not in ("", "idle", None):
                # Rebuilding is not healthy and not broken. Record it, keep it
                # off the pager: it resolves itself or it turns into a fault.
                self.transition(name, "up",
                                f"{a['level']} — {a['sync_action']} "
                                f"{a.get('sync_completed') or ''}".strip(), sev)
            else:
                note = "no redundancy by design" if not a["redundant"] else "clean"
                self.transition(name, "up", f"{a['level']} — {note}", sev)

        for d in self.store.disks():
            health = (d.get("health") or "UNKNOWN").upper()
            # UNKNOWN is the normal answer from a virtual disk, not a fault.
            if health in ("UNKNOWN", ""):
                continue
            name = f"disk {d['host']} {d['dev']}"
            live.append(name)
            self.store.register(name, "storage", "Disks & arrays",
                                f"{d['host']} {d.get('model') or d['dev']}", sev)
            if health in ("PASSED", "OK"):
                self.transition(name, "up", f"SMART {health}", sev)
            else:
                self.transition(name, "down",
                                f"SMART {health} — {d.get('model') or ''} "
                                f"{d.get('serial') or ''}".strip(), sev)

        self.store.forget_storage(live)

    def run_check(self, c):
        ok, ms, detail = probes.run(c, self.cfg["settings"]["timeout"])
        self.store.record(c["name"], ok, ms, detail)
        st = self.store.get(c["name"]) or {}
        fails = 0 if ok else st.get("fails", 0) + 1
        fields = {"fails": fails, "last_run": now(), "last_ms": ms, "detail": detail}
        if ok:
            fields["last_ok"] = now()
        self.store.update(c["name"], **fields)
        if ok:
            self.transition(c["name"], "up", detail, c["severity"])
        elif fails >= c["retries"]:
            self.transition(c["name"], "down", f"{detail} ({fails} consecutive)",
                            c["severity"])

    def sweep_jobs(self):
        """Silence is the alert. Nothing was down when the SQL dumps stopped in
        July — the work simply stopped, and no endpoint would have shown it."""
        for j in self.cfg.get("jobs", []):
            st = self.store.get(j["name"]) or {}
            last = st.get("last_ok")
            if last is None:
                self.transition(j["name"], "down", "has never checked in", j["severity"])
            elif now() - last > j["grace_hours"] * 3600:
                hrs = (now() - last) // 3600
                self.transition(j["name"], "down",
                                f"no check-in for {hrs}h "
                                f"(expected within {j['grace_hours']}h)", j["severity"])
            else:
                self.transition(j["name"], "up", "checking in", j["severity"])

    def checkin(self, token):
        for j in self.cfg.get("jobs", []):
            if j.get("token") and j["token"] == token:
                self.store.update(j["name"], last_ok=now(), last_run=now(),
                                  fails=0, detail="checked in")
                self.transition(j["name"], "up", "checked in", j["severity"])
                return j["name"]
        return None

    def deadman(self):
        """Ping the external heartbeat — and notice when that stops working.

        Nine days were lost to this. The URL was set to an example value, so
        it was not empty and nothing warned; the ping then failed every 60
        seconds into the journal, 2853 times, and the only place that said so
        was a log line nobody reads.

        Guessing whether a URL *looks* valid does not work — a placeholder can
        be any string. Observing whether the heartbeat is *accepted* does.
        """
        url = self.cfg["settings"]["deadman_url"]
        if not url:
            return
        sev = self.cfg["settings"]["deadman_severity"]
        try:
            urllib.request.urlopen(url, timeout=self.cfg["settings"]["timeout"]).read()
        except Exception as e:
            self.deadman_fails += 1
            log(f"dead-man ping failed: {e}")
            # Five in a row is five minutes: past a blip, and the heartbeat is
            # genuinely not arriving — which means this host dying would go
            # unnoticed, the exact thing the switch exists to prevent.
            if self.deadman_fails >= 5:
                self.transition(DEADMAN, "down",
                                f"{self.deadman_fails} consecutive failures — {e}",
                                sev)
            return
        self.deadman_fails = 0
        self.transition(DEADMAN, "up", "heartbeat accepted", sev)

    def loop(self):
        last_prune = 0
        last_deadman = 0
        while True:
            t = now()
            for c in self.cfg.get("checks", []):
                if t >= self.next_run.get(c["name"], 0):
                    self.next_run[c["name"]] = t + c["interval"]
                    try:
                        self.run_check(c)
                    except Exception as e:
                        log(f"check '{c['name']}' raised: {e}")
                if t >= self.next_cert.get(c["name"], 0):
                    self.next_cert[c["name"]] = t + 6 * 3600
                    try:
                        self.refresh_cert(c)
                    except Exception as e:
                        log(f"cert check '{c['name']}' raised: {e}")
            self.sweep_jobs()
            try:
                self.sweep_storage()
            except Exception as e:
                log(f"storage sweep raised: {e}")
            if t >= self.next_printer:
                self.next_printer = t + self.cfg["settings"]["printer_interval"]
                try:
                    self.poll_printers()
                except Exception as e:
                    log(f"printer poll raised: {e}")
            if t - last_deadman >= 60:
                self.deadman()
                last_deadman = t
            if t - last_prune > 86400:
                self.store.prune()
                last_prune = t
            time.sleep(5)
