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
    def __init__(self, cfg, store, notifier):
        self.cfg, self.store, self.note = cfg, store, notifier
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
        self.printers = cfg.get("printers", [])
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
        """Toner does not move quickly, and SNMP on a sleeping printer can be
        slow, so this runs every fifteen minutes rather than every cycle."""
        for p in self.printers:
            host = p.get("host")
            if not host:
                continue
            data, err = prt.read(host, p.get("community", "public"),
                                 self.cfg["settings"]["timeout"] + 2)
            if err:
                self.store.put_printer(host, p.get("name") or host, "",
                                       p.get("location", ""), "unreachable",
                                       "bad", err, None,
                                       {"supplies": [], "trays": []}, err)
                continue
            state, detail = prt.worst(data, p.get("warn_percent", 20),
                                      p.get("bad_percent", 10))
            self.store.put_printer(
                host, p.get("name") or data["name"], data["model"],
                # The device usually knows where it is; config only overrides
                # it when someone has bothered to fill that in.
                p.get("location") or data.get("location", ""),
                data["status"], state, detail, data["pages"],
                {"supplies": data["supplies"], "trays": data["trays"],
                 "errors": data["errors"], "descr": data["descr"],
                 "console": data.get("console", ""),
                 "covers": data.get("covers", []),
                 "alerts": data.get("alerts", []),
                 "colorants": data.get("colorants", []),
                 "contact": data.get("contact", ""),
                 "uptime_s": data.get("uptime_s", 0)},
                None, data.get("serial", ""))

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
        url = self.cfg["settings"]["deadman_url"]
        if not url:
            return
        try:
            urllib.request.urlopen(url, timeout=self.cfg["settings"]["timeout"]).read()
        except Exception as e:
            log(f"dead-man ping failed: {e}")

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
            if t >= self.next_printer:
                self.next_printer = t + 15 * 60
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
