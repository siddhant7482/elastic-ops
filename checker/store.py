"""
Storage — one SQLite file, two concerns.

  Store   check and job state, raw results, state-change events.
  Portal  the things people and other machines write: incidents, follow-ups,
          changes, backup runs, host metrics.

Inventory (hosts, drives, apps, credentials, blast radius, routing) is NOT in
here. It lives in inventory.toml because it changes a few times a year and is
worth having in version control, and because a CRUD screen for it would be more
code to maintain than the data is worth.
"""

import json
import os
import sqlite3
import threading
import time


def now() -> int:
    return int(time.time())


# The only columns Store.update may write. Anything else is a typo that would
# otherwise fail silently or widen a query it has no business widening.
STATE_COLUMNS = frozenset({
    "kind", "grp", "target", "severity", "status", "fails",
    "since", "last_ok", "last_run", "last_ms", "detail",
})


SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
  name     TEXT PRIMARY KEY,
  kind     TEXT NOT NULL,          -- 'check' or 'job'
  grp      TEXT,
  target   TEXT,
  severity TEXT,
  status   TEXT NOT NULL,          -- up | down | unknown
  fails    INTEGER NOT NULL DEFAULT 0,
  since    INTEGER,                -- when the current status began
  last_ok  INTEGER,
  last_run INTEGER,
  last_ms  INTEGER,
  detail   TEXT
);
CREATE TABLE IF NOT EXISTS results (
  name TEXT NOT NULL, ts INTEGER NOT NULL, ok INTEGER NOT NULL,
  ms INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS results_name_ts ON results(name, ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, name TEXT NOT NULL,
  from_status TEXT, to_status TEXT, detail TEXT, notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incidents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ref        TEXT UNIQUE,
  title      TEXT NOT NULL,
  severity   TEXT NOT NULL,        -- minor | major | critical
  system     TEXT,
  detail     TEXT,
  status     TEXT NOT NULL,        -- open | resolved
  owner      TEXT,
  prevention TEXT,                 -- answer to "what would have caught this sooner"
  opened_ts  INTEGER NOT NULL,
  closed_ts  INTEGER
);
CREATE TABLE IF NOT EXISTS followups (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER,
  action      TEXT NOT NULL,
  owner       TEXT,
  due         TEXT,
  status      TEXT NOT NULL DEFAULT 'open',   -- open | done
  created_ts  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  title  TEXT NOT NULL,
  who    TEXT,
  system TEXT,
  detail TEXT
);

-- Written by the collector on cinnamon, not by this process.
CREATE TABLE IF NOT EXISTS backups (
  vmid       TEXT PRIMARY KEY,
  name       TEXT,
  host       TEXT,
  last_ts    INTEGER,              -- NULL means it has never run
  size_bytes INTEGER,
  state      TEXT,                 -- ok | warn | bad
  detail     TEXT,
  updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pools (
  name       TEXT NOT NULL,
  host       TEXT NOT NULL,
  used_bytes INTEGER,
  total_bytes INTEGER,
  ts         INTEGER NOT NULL,
  PRIMARY KEY (host, name)
);
CREATE TABLE IF NOT EXISTS pool_history (
  host TEXT NOT NULL, name TEXT NOT NULL, ts INTEGER NOT NULL, used_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS pool_hist_k ON pool_history(host, name, ts);
CREATE TABLE IF NOT EXISTS hostmetrics (
  host TEXT NOT NULL, ts INTEGER NOT NULL, payload TEXT NOT NULL
);

-- Every guest the collectors can see. The point of this table is subtraction:
-- what exists, minus what is checked, is what nobody is watching.
CREATE TABLE IF NOT EXISTS guests (
  key        TEXT PRIMARY KEY,      -- host/vmid
  vmid       TEXT,
  name       TEXT,
  host       TEXT,
  kind       TEXT,                  -- qemu | lxc
  status     TEXT,                  -- running | stopped
  addresses  TEXT,                  -- comma separated, from the guest agent
  updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS hostmetrics_k ON hostmetrics(host, ts);

CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pwhash TEXT NOT NULL,
  display TEXT, role TEXT DEFAULT 'admin', created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, username TEXT NOT NULL,
  created_ts INTEGER NOT NULL, expires_ts INTEGER NOT NULL
);
"""


class Store:
    """Check and job state. Every public method takes the lock itself."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # -- checks and jobs -------------------------------------------------
    def register(self, name, kind, grp, target, severity):
        self.x(
            "INSERT INTO state(name,kind,grp,target,severity,status,since) "
            "VALUES(?,?,?,?,?,'unknown',?) ON CONFLICT(name) DO UPDATE SET "
            "kind=excluded.kind, grp=excluded.grp, target=excluded.target, "
            "severity=excluded.severity",
            (name, kind, grp, target, severity, now()),
        )

    def forget_missing(self, names):
        """Drop state for checks deleted from the config, so the portal does
        not keep showing something nobody is measuring any more."""
        if not names:
            return
        marks = ",".join("?" * len(names))   # placeholders only; no data is interpolated
        self.x(f"DELETE FROM state WHERE name NOT IN ({marks})", tuple(names))  # noqa: S608

    def get(self, name):
        return self.one("SELECT * FROM state WHERE name=?", (name,))

    def all_state(self):
        return self.q("SELECT * FROM state ORDER BY kind, grp, name")

    def record(self, name, ok, ms, detail):
        self.x("INSERT INTO results(name,ts,ok,ms,detail) VALUES(?,?,?,?,?)",
               (name, now(), 1 if ok else 0, ms, detail))

    def update(self, name, **kw):
        # Column names cannot be placeholders, so they are interpolated — and
        # therefore checked against the schema rather than trusted. Values
        # always go through placeholders.
        unknown = set(kw) - STATE_COLUMNS
        if unknown:
            raise ValueError(f"not columns of state: {sorted(unknown)}")
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE state SET {cols} WHERE name=?", (*kw.values(), name))  # noqa: S608

    def event(self, name, frm, to, detail):
        self.x("INSERT INTO events(ts,name,from_status,to_status,detail) VALUES(?,?,?,?,?)",
               (now(), name, frm, to, detail))

    def recent_events(self, limit=150):
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def history(self, name, days=45):
        """One bucket per day: 0 fine, 1 partial failure, 2 all failed, -1 no data."""
        t = now()
        rows = self.q("SELECT ts, ok FROM results WHERE name=? AND ts>=?",
                      (name, t - days * 86400))
        buckets = {}
        for r in rows:
            day = (t - r["ts"]) // 86400
            tot, bad = buckets.get(day, (0, 0))
            buckets[day] = (tot + 1, bad + (0 if r["ok"] else 1))
        out = []
        for d in range(days - 1, -1, -1):
            if d not in buckets:
                out.append(-1)
            else:
                tot, bad = buckets[d]
                out.append(0 if bad == 0 else (2 if bad == tot else 1))
        return out

    def uptime(self, name, days=30):
        t = now()
        r = self.one(
            "SELECT COUNT(1) AS n, SUM(ok) AS good FROM results WHERE name=? AND ts>=?",
            (name, t - days * 86400))
        if not r or not r["n"]:
            return None
        return round(100.0 * (r["good"] or 0) / r["n"], 2)

    def daily_uptime(self, days=30):
        """Estate-wide: share of all check results that answered, per day.
        One series, which is what the overview chart draws."""
        t = now()
        rows = self.q("SELECT ts, ok FROM results WHERE ts>=?", (t - days * 86400,))
        buckets = {}
        for r in rows:
            day = (t - r["ts"]) // 86400
            tot, good = buckets.get(day, (0, 0))
            buckets[day] = (tot + 1, good + r["ok"])
        out = []
        for d in range(days - 1, -1, -1):
            tot, good = buckets.get(d, (0, 0))
            out.append({"days_ago": d,
                        "pct": round(100.0 * good / tot, 2) if tot else None})
        return out

    def prune(self, results_days=120, events_days=400):
        t = now()
        self.x("DELETE FROM results WHERE ts < ?", (t - results_days * 86400,))
        self.x("DELETE FROM events WHERE ts < ?", (t - events_days * 86400,))
        self.x("DELETE FROM pool_history WHERE ts < ?", (t - events_days * 86400,))
        self.x("DELETE FROM hostmetrics WHERE ts < ?", (t - 30 * 86400,))
        self.x("DELETE FROM sessions WHERE expires_ts < ?", (t,))


class Portal:
    """Records people and other machines write. Shares the Store's connection."""

    def __init__(self, store: Store):
        self.s = store

    # -- incidents -------------------------------------------------------
    def incidents(self):
        rows = self.s.q("SELECT * FROM incidents ORDER BY opened_ts DESC")
        for r in rows:
            r["followups"] = self.s.q(
                "SELECT * FROM followups WHERE incident_id=? ORDER BY id", (r["id"],))
        return rows

    def next_ref(self, ts):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        n = self.s.one("SELECT COUNT(1) AS n FROM incidents WHERE ref LIKE ?",
                       (f"INC-{day}%",))["n"]
        return f"INC-{day}" + (f"-{n + 1}" if n else "")

    def add_incident(self, title, severity, system, detail, owner):
        ts = now()
        ref = self.next_ref(ts)
        self.s.x(
            "INSERT INTO incidents(ref,title,severity,system,detail,status,owner,opened_ts)"
            " VALUES(?,?,?,?,?,'open',?,?)",
            (ref, title, severity, system, detail, owner, ts))
        return self.s.one("SELECT * FROM incidents WHERE ref=?", (ref,))

    def resolve_incident(self, iid, prevention, owner):
        """Closing asks one question — what would have caught this sooner — and
        the answer becomes a follow-up rather than a sentence nobody re-reads."""
        self.s.x("UPDATE incidents SET status='resolved', closed_ts=?, prevention=? "
                 "WHERE id=?", (now(), prevention, iid))
        if prevention and prevention.strip():
            self.add_followup(iid, prevention.strip(), owner, None)
        return self.s.one("SELECT * FROM incidents WHERE id=?", (iid,))

    # -- follow-ups ------------------------------------------------------
    def followups(self):
        return self.s.q(
            "SELECT f.*, i.ref AS incident_ref, i.title AS incident_title "
            "FROM followups f LEFT JOIN incidents i ON i.id = f.incident_id "
            "ORDER BY CASE f.status WHEN 'open' THEN 0 ELSE 1 END, f.due IS NULL, f.due")

    def add_followup(self, incident_id, action, owner, due):
        return self.s.x(
            "INSERT INTO followups(incident_id,action,owner,due,created_ts) VALUES(?,?,?,?,?)",
            (incident_id, action, owner, due, now()))

    def set_followup(self, fid, status):
        self.s.x("UPDATE followups SET status=? WHERE id=?", (status, fid))

    # -- changes ---------------------------------------------------------
    def changes(self, limit=100):
        return self.s.q("SELECT * FROM changes ORDER BY ts DESC LIMIT ?", (limit,))

    def add_change(self, title, who, system, detail, ts=None):
        return self.s.x("INSERT INTO changes(ts,title,who,system,detail) VALUES(?,?,?,?,?)",
                        (ts or now(), title, who, system, detail))

    # -- ingested from the estate ----------------------------------------
    def backups(self):
        return self.s.q("SELECT * FROM backups ORDER BY state DESC, host, vmid")

    def put_backups(self, rows):
        t = now()
        for r in rows:
            self.s.x(
                "INSERT INTO backups(vmid,name,host,last_ts,size_bytes,state,detail,updated_ts)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(vmid) DO UPDATE SET "
                "name=excluded.name, host=excluded.host, last_ts=excluded.last_ts, "
                "size_bytes=excluded.size_bytes, state=excluded.state, "
                "detail=excluded.detail, updated_ts=excluded.updated_ts",
                (str(r.get("vmid")), r.get("name"), r.get("host"), r.get("last_ts"),
                 r.get("size_bytes"), r.get("state", "ok"), r.get("detail"), t))
        return len(rows)

    def pools(self):
        return self.s.q("SELECT * FROM pools ORDER BY host, name")

    def put_pools(self, host, rows):
        t = now()
        for r in rows:
            self.s.x(
                "INSERT INTO pools(name,host,used_bytes,total_bytes,ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(host,name) DO UPDATE SET used_bytes=excluded.used_bytes, "
                "total_bytes=excluded.total_bytes, ts=excluded.ts",
                (r["name"], host, r.get("used_bytes"), r.get("total_bytes"), t))
            self.s.x("INSERT INTO pool_history(host,name,ts,used_bytes) VALUES(?,?,?,?)",
                     (host, r["name"], t, r.get("used_bytes")))
        return len(rows)

    def pool_growth(self, host, name, days=30):
        """Bytes per week, measured from the oldest sample we hold. Returns
        (bytes_per_week, days_until_full) — a date is more use than a percentage."""
        t = now()
        rows = self.s.q(
            "SELECT ts, used_bytes FROM pool_history WHERE host=? AND name=? AND ts>=? "
            "ORDER BY ts", (host, name, t - days * 86400))
        if len(rows) < 2:
            return None, None
        span = rows[-1]["ts"] - rows[0]["ts"]
        delta = (rows[-1]["used_bytes"] or 0) - (rows[0]["used_bytes"] or 0)
        if span < 86400:
            return None, None
        per_week = delta / span * 604800
        pool = self.s.one("SELECT * FROM pools WHERE host=? AND name=?", (host, name))
        if not pool or per_week <= 0 or not pool["total_bytes"]:
            return round(per_week), None
        free = pool["total_bytes"] - (pool["used_bytes"] or 0)
        return round(per_week), int(free / (per_week / 7))

    # -- guests, and what nobody is watching -----------------------------
    def guests(self):
        return self.s.q("SELECT * FROM guests ORDER BY host, CAST(vmid AS INTEGER)")

    def put_guests(self, host, rows):
        """Replace this host's guest list. Replaced rather than merged, so a
        guest that has been deleted stops being reported as unmonitored."""
        t = now()
        self.s.x("DELETE FROM guests WHERE host=?", (host,))
        for r in rows:
            self.s.x(
                "INSERT INTO guests(key,vmid,name,host,kind,status,addresses,updated_ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (f"{host}/{r.get('vmid')}", str(r.get("vmid")), r.get("name"), host,
                 r.get("kind"), r.get("status"),
                 ",".join(r.get("addresses") or []), t))
        return len(rows)

    def coverage(self, checks):
        """Match guests against checks by address first, then by name.

        A guest counts as watched if some check points at one of its
        addresses, or if a check's name matches the guest's. Deliberately
        generous: the aim is to surface the genuinely forgotten, not to
        produce a long list of near-misses nobody will read.
        """
        targets, names = [], []
        for c in checks:
            targets.append((c.get("target") or "").lower())
            names.append((c.get("name") or "").lower())

        out = []
        for g in self.guests():
            addrs = [a for a in (g["addresses"] or "").split(",") if a]
            gname = (g["name"] or "").lower()
            hit = None
            for i, t in enumerate(targets):
                if any(a and a in t for a in addrs):
                    hit = names[i]
                    break
            if not hit and gname:
                for n in names:
                    if gname == n or (len(gname) > 3 and gname in n):
                        hit = n
                        break
            out.append({**g, "addresses": addrs, "watched_by": hit})
        return out

    def put_host(self, host, payload):
        self.s.x("INSERT INTO hostmetrics(host,ts,payload) VALUES(?,?,?)",
                 (host, now(), json.dumps(payload)))

    def host_latest(self):
        rows = self.s.q(
            "SELECT h.host, h.ts, h.payload FROM hostmetrics h JOIN "
            "(SELECT host, MAX(ts) AS m FROM hostmetrics GROUP BY host) x "
            "ON x.host = h.host AND x.m = h.ts")
        out = []
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except ValueError:
                continue
            p["host"], p["ts"] = r["host"], r["ts"]
            out.append(p)
        return out

    def host_series(self, host, field="mem_used", points=24):
        rows = self.s.q(
            "SELECT ts, payload FROM hostmetrics WHERE host=? ORDER BY ts DESC LIMIT ?",
            (host, points))
        out = []
        for r in reversed(rows):
            try:
                out.append(json.loads(r["payload"]).get(field))
            except ValueError:
                pass
        return [v for v in out if v is not None]
