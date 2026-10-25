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

import probes
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
}


def load_config(path):
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
        if val:
            s[key] = val
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
    def __init__(self, cfg):
        s = cfg["settings"]
        self.url = s["webhook_url"]
        self.header = s["webhook_header"]
        self.timeout = s["timeout"]

    def send(self, name, status, severity, detail, window):
        word = {"up": "RECOVERED", "down": "DOWN", "unknown": "UNKNOWN"}[status]
        text = f"[{word}] {name} — {detail}"
        if window:
            text += f"  (suppressed: {window})"
        log(text)
        if window or not self.url:
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
        names = []
        for c in cfg.get("checks", []):
            store.register(c["name"], "check", c["group"], c["target"], c["severity"])
            names.append(c["name"])
        for j in cfg.get("jobs", []):
            store.register(j["name"], "job", "Jobs",
                           f"every {j['grace_hours']}h", j["severity"])
            names.append(j["name"])
        store.forget_missing(names)

    def transition(self, name, new_status, detail, severity):
        st = self.store.get(name) or {}
        old = st.get("status", "unknown")
        if old == new_status:
            return
        self.store.update(name, status=new_status, since=now(), detail=detail)
        self.store.event(name, old, new_status, detail)
        if old == "unknown" and new_status == "up":
            return                      # the first successful check is not news
        self.note.send(name, new_status, severity, detail, in_maintenance(self.cfg))

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
            self.sweep_jobs()
            if t - last_deadman >= 60:
                self.deadman()
                last_deadman = t
            if t - last_prune > 86400:
                self.store.prune()
                last_prune = t
            time.sleep(5)
