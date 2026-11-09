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
  impact     TEXT,                 -- who is affected and how
  detected   TEXT,                 -- monitoring | user report | routine check | ...
  started    TEXT,                 -- when it actually began, if known
  root_cause TEXT,                 -- why it happened, as opposed to what was seen
  resolution TEXT,                 -- what was actually done about it
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

-- Every silence has an expiry. A mute with no end date is how monitoring
-- quietly dies: the thing stays quiet long after the reason is gone, and
-- nobody remembers it was ever muted.
CREATE TABLE IF NOT EXISTS silences (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  target     TEXT NOT NULL,          -- a check/job name, or '*' for everything
  until_ts   INTEGER NOT NULL,
  reason     TEXT,
  created_by TEXT,
  created_ts INTEGER NOT NULL,
  ended_ts   INTEGER                 -- set when lifted early
);
CREATE INDEX IF NOT EXISTS silences_target ON silences(target, until_ts);

CREATE TABLE IF NOT EXISTS printers (
  host       TEXT PRIMARY KEY,
  name       TEXT, model TEXT, location TEXT, serial TEXT,
  status     TEXT, state TEXT, detail TEXT,
  pages      INTEGER,
  payload    TEXT,                 -- supplies and trays, as json
  error      TEXT,
  ts         INTEGER NOT NULL
);

-- One row per poll where something moved. Toner changes slowly, so writing
-- only on change keeps this small while still carrying a usable trend.
CREATE TABLE IF NOT EXISTS printer_history (
  host     TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  pages    INTEGER,
  supplies TEXT                    -- {"Toner (Cyan)": 48.0, ...}
);
CREATE INDEX IF NOT EXISTS printer_hist_k ON printer_history(host, ts);

CREATE TABLE IF NOT EXISTS certs (
  name       TEXT PRIMARY KEY,      -- the check this cert belongs to
  host       TEXT, port INTEGER,
  not_before INTEGER, not_after INTEGER,
  days_left  REAL, lifetime_days REAL,
  expired    INTEGER, renew_soon INTEGER,
  detail     TEXT,
  ts         INTEGER NOT NULL
);

-- Array integrity, which is a different question from the capacity in pools:
-- a RAID 0 reporting 40% used is still one disk away from losing all of it.
CREATE TABLE IF NOT EXISTS arrays (
  host      TEXT NOT NULL,
  name      TEXT NOT NULL,          -- md0, rpool
  kind      TEXT,                   -- mdraid | zfs
  level     TEXT,                   -- raid0, raid5, zfs
  state     TEXT,                   -- array_state, or zpool state
  degraded  INTEGER,                -- disks missing from the array
  disks     INTEGER,
  redundant INTEGER,                -- survives losing one disk at all?
  sync_action    TEXT,
  sync_completed TEXT,
  members   TEXT,                   -- json: [{dev, state}]
  ts        INTEGER NOT NULL,
  PRIMARY KEY (host, name)
);

CREATE TABLE IF NOT EXISTS disks (
  host       TEXT NOT NULL,
  dev        TEXT NOT NULL,
  model      TEXT, serial TEXT,
  size_bytes INTEGER,
  health     TEXT,                  -- PASSED/OK, UNKNOWN, or a real fault
  wearout    TEXT,                  -- SSD life left, where the drive reports it
  type       TEXT, used TEXT,
  ts         INTEGER NOT NULL,
  PRIMARY KEY (host, dev)
);

CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pwhash TEXT NOT NULL,
  display TEXT, role TEXT DEFAULT 'admin', created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, username TEXT NOT NULL,
  created_ts INTEGER NOT NULL, expires_ts INTEGER NOT NULL
);

-- Citadel. The markdown files on disk are the source of truth; these tables
-- are a cache of what was last read, so a page view costs no disk walk and no
-- re-render. The cache is also the fallback: if the document tree goes
-- missing or unreadable, the catalogue still serves what it last saw, which
-- is the difference between degraded and down. Documentation that stops
-- working when its storage is unhappy fails exactly when it is needed.
CREATE TABLE IF NOT EXISTS records (
  shelf      TEXT PRIMARY KEY,      -- HST.PEP
  kind       TEXT NOT NULL,         -- host | guest | app | device | runbook | ...
  slug       TEXT NOT NULL,
  path       TEXT NOT NULL,         -- host/pepper.md, relative to the tree
  title      TEXT,
  owner      TEXT,
  verified   TEXT,                  -- ISO date, as written by a person
  review     TEXT,                  -- "6 months"
  bind       TEXT,                  -- the [bind] table, as JSON
  body       TEXT,                  -- markdown source
  html       TEXT,                  -- rendered, safe to serve as-is
  plain      TEXT,                  -- searchable text, markup stripped
  blob       TEXT,                  -- content hash; re-render only when it moves
  problems   TEXT,                  -- JSON list; malformed front matter etc.
  scanned_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS records_kind ON records(kind, title);

-- The cross-reference graph. Kept as rows rather than recomputed because a
-- broken link is a finding, and a finding needs somewhere to be counted from.
CREATE TABLE IF NOT EXISTS record_links (
  from_shelf TEXT NOT NULL,
  to_shelf   TEXT NOT NULL,
  resolved   INTEGER NOT NULL,      -- 0 means no such record
  PRIMARY KEY (from_shelf, to_shelf)
);
CREATE INDEX IF NOT EXISTS record_links_to ON record_links(to_shelf);

-- Phase 2: the push queue and the visible per-page history. The history
-- itself is git's; this only caches it so a page view does not shell out, and
-- it can be dropped and rebuilt from the repository. The one column that is
-- NOT in git is `pushed`, which is local knowledge and the difference between
-- written down and written down somewhere that survives this host.
CREATE TABLE IF NOT EXISTS doc_commits (
  sha     TEXT PRIMARY KEY,
  ts      INTEGER NOT NULL,
  author  TEXT,
  shelf   TEXT,
  path    TEXT,
  summary TEXT,
  via     TEXT,                     -- browser | git | scaffold | migration
  pushed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS doc_commits_ts ON doc_commits(ts);
CREATE INDEX IF NOT EXISTS doc_commits_shelf ON doc_commits(shelf, ts);
"""

# FTS5 is a compile-time option. It is present in every build we have met, but
# a database that cannot index is still a catalogue that must open, so the
# search falls back to LIKE over the same cached text. At 14,000 words the
# difference is not measurable.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  shelf, title, plain, tokenize='porter unicode61'
);
"""


# Columns added to tables that already exist in the wild. CREATE TABLE IF NOT
# EXISTS silently does nothing to a table that is already there, so a new
# column means every INSERT starts throwing on deployed databases while the
# page quietly serves yesterday's rows. Add to this list, never edit history.
MIGRATIONS = [
    ("printers", "serial", "TEXT"),
    ("incidents", "resolution", "TEXT"),
    ("incidents", "impact", "TEXT"),
    ("incidents", "detected", "TEXT"),
    ("incidents", "started", "TEXT"),
    ("incidents", "root_cause", "TEXT"),
]


class Store:
    """Check and job state. Every public method takes the lock itself."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Probe rather than assume. A build without FTS5 must still open the
        # catalogue; search degrades to LIKE and says so.
        try:
            self.db.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False
        self.db.commit()
        self.lock = threading.Lock()
        self.migrate()

    def migrate(self):
        """Add columns that newer code expects to tables created by older."""
        for table, column, decl in MIGRATIONS:
            rows = self.q(f"PRAGMA table_info({table})")
            if not rows:
                continue                    # table is new; SCHEMA already has it
            if column not in {r["name"] for r in rows}:
                self.x(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                print(f"migrated: {table}.{column} added", flush=True)

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
        not keep showing something nobody is measuring any more.

        Limited to the kinds that come FROM the config. Storage checks are
        discovered from what the collectors push, long after this runs at
        startup, so sweeping them here would delete every array on restart
        and then re-alert on the next poll as though it were news.
        """
        if not names:
            return
        marks = ",".join("?" * len(names))   # placeholders only; no data is interpolated
        sql = f"DELETE FROM state WHERE kind IN ('check','job') AND name NOT IN ({marks})"  # noqa: S608,E501
        self.x(sql, tuple(names))

    def arrays(self):
        return self.q("SELECT * FROM arrays ORDER BY host, name")

    def disks(self):
        return self.q("SELECT * FROM disks ORDER BY host, dev")

    def put_arrays(self, host, rows):
        """Replace this host's arrays. A removed array has to disappear, not
        linger as a healthy-looking row nobody is measuring any more."""
        t = now()
        seen = []
        for r in rows:
            name = r.get("name")
            if not name:
                continue
            seen.append(name)
            self.x(
                "INSERT INTO arrays(host,name,kind,level,state,degraded,disks,"
                "redundant,sync_action,sync_completed,members,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host,name) DO UPDATE SET kind=excluded.kind, "
                "level=excluded.level, state=excluded.state, "
                "degraded=excluded.degraded, disks=excluded.disks, "
                "redundant=excluded.redundant, sync_action=excluded.sync_action, "
                "sync_completed=excluded.sync_completed, "
                "members=excluded.members, ts=excluded.ts",
                (host, name, r.get("kind"), r.get("level"), r.get("state"),
                 int(r.get("degraded") or 0), int(r.get("disks") or 0),
                 1 if r.get("redundant") else 0, r.get("sync_action"),
                 r.get("sync_completed"),
                 json.dumps(r.get("members", [])), t))
        self._prune(host, "arrays", "name", seen)
        return len(seen)

    def put_disks(self, host, rows):
        t = now()
        seen = []
        for r in rows:
            dev = r.get("dev")
            if not dev:
                continue
            seen.append(dev)
            self.x(
                "INSERT INTO disks(host,dev,model,serial,size_bytes,health,"
                "wearout,type,used,ts) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host,dev) DO UPDATE SET model=excluded.model, "
                "serial=excluded.serial, size_bytes=excluded.size_bytes, "
                "health=excluded.health, wearout=excluded.wearout, "
                "type=excluded.type, used=excluded.used, ts=excluded.ts",
                (host, dev, r.get("model"), r.get("serial"),
                 r.get("size_bytes"), r.get("health"),
                 str(r.get("wearout")) if r.get("wearout") is not None else None,
                 r.get("type"), r.get("used"), t))
        self._prune(host, "disks", "dev", seen)
        return len(seen)

    def _prune(self, host, table, key, keep):
        """Drop this host's rows that the latest post did not mention.

        table and key are fixed strings from the two callers above, never
        anything that arrived over the wire; only the values are bound.
        """
        if table not in ("arrays", "disks") or key not in ("name", "dev"):
            raise ValueError("not a prunable table")
        if not keep:
            self.x(f"DELETE FROM {table} WHERE host=?", (host,))  # noqa: S608
            return
        marks = ",".join("?" * len(keep))
        self.x(
            f"DELETE FROM {table} WHERE host=? AND {key} NOT IN ({marks})",  # noqa: S608
            (host, *keep))


    def forget(self, name):
        """Drop one check by name, for something that used to be
        configured and is not any more."""
        self.x("DELETE FROM state WHERE name=?", (name,))

    def forget_storage(self, names):
        """Drop storage checks for arrays and disks that have stopped being
        reported — a disk pulled out of a host should leave the board, not sit
        there green forever on its last known reading."""
        if not names:
            self.x("DELETE FROM state WHERE kind='storage'")
            return
        marks = ",".join("?" * len(names))
        sql = f"DELETE FROM state WHERE kind='storage' AND name NOT IN ({marks})"  # noqa: S608
        self.x(sql, tuple(names))

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

    # -- certificates ----------------------------------------------------
    def certs(self):
        return self.q("SELECT * FROM certs ORDER BY days_left")

    def put_cert(self, name, host, port, info, detail):
        self.x(
            "INSERT INTO certs(name,host,port,not_before,not_after,days_left,"
            "lifetime_days,expired,renew_soon,detail,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET host=excluded.host, port=excluded.port, "
            "not_before=excluded.not_before, not_after=excluded.not_after, "
            "days_left=excluded.days_left, lifetime_days=excluded.lifetime_days, "
            "expired=excluded.expired, renew_soon=excluded.renew_soon, "
            "detail=excluded.detail, ts=excluded.ts",
            (name, host, port,
             (info or {}).get("not_before"), (info or {}).get("not_after"),
             (info or {}).get("days_left"), (info or {}).get("lifetime_days"),
             1 if (info or {}).get("expired") else 0,
             1 if (info or {}).get("renew_soon") else 0, detail, now()))


    # -- silences ---------------------------------------------------------
    def silences(self, include_past=False):
        if include_past:
            return self.q("SELECT * FROM silences ORDER BY until_ts DESC LIMIT 100")
        return self.q("SELECT * FROM silences WHERE ended_ts IS NULL AND until_ts > ? "
                      "ORDER BY until_ts", (now(),))

    def add_silence(self, target, until_ts, reason, who):
        return self.x("INSERT INTO silences(target,until_ts,reason,created_by,created_ts) "
                      "VALUES(?,?,?,?,?)", (target, int(until_ts), reason, who, now()))

    def end_silence(self, sid):
        self.x("UPDATE silences SET ended_ts=? WHERE id=? AND ended_ts IS NULL",
               (now(), sid))

    def silenced(self, name):
        """The live silence covering this check, or None. '*' covers everything."""
        r = self.one(
            "SELECT * FROM silences WHERE ended_ts IS NULL AND until_ts > ? "
            "AND target IN (?, '*') ORDER BY until_ts DESC LIMIT 1", (now(), name))
        return r

    # -- printers ---------------------------------------------------------
    def printers(self):
        rows = self.q("SELECT * FROM printers ORDER BY name")
        for r in rows:
            try:
                r.update(json.loads(r.pop("payload") or "{}"))
            except ValueError:
                r["supplies"], r["trays"] = [], []
        return rows

    def put_printer(self, host, name, model, location, status, state, detail,
                    pages, payload, error, serial=""):
        self.x(
            "INSERT INTO printers(host,name,model,location,serial,status,state,"
            "detail,pages,payload,error,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET name=excluded.name, "
            "model=excluded.model, location=excluded.location, "
            "serial=excluded.serial, status=excluded.status, state=excluded.state, "
            "detail=excluded.detail, pages=excluded.pages, payload=excluded.payload, "
            "error=excluded.error, ts=excluded.ts",
            (host, name, model, location, serial, status, state, detail, pages,
             json.dumps(payload), error, now()))

    def forget_printers(self, hosts):
        if not hosts:
            self.x("DELETE FROM printers")
            return
        marks = ",".join("?" * len(hosts))
        self.x(f"DELETE FROM printers WHERE host NOT IN ({marks})",  # noqa: S608
               tuple(hosts))

    def put_printer_history(self, host, pages, supplies):
        """Append only when something moved, so a quiet printer costs nothing."""
        last = self.one("SELECT pages, supplies FROM printer_history WHERE host=? "
                        "ORDER BY ts DESC LIMIT 1", (host,))
        blob = json.dumps(supplies, sort_keys=True)
        if last and last["pages"] == pages and last["supplies"] == blob:
            return False
        self.x("INSERT INTO printer_history(host,ts,pages,supplies) VALUES(?,?,?,?)",
               (host, now(), pages, blob))
        return True

    def printer_trend(self, host, days=60):
        """Depletion per supply and pages per day.

        A cartridge change makes the level jump upward, and averaging across
        that would report a supply filling itself. So each series is cut at
        the most recent increase and only the run since then is measured.
        """
        rows = self.q("SELECT ts, pages, supplies FROM printer_history "
                      "WHERE host=? AND ts>=? ORDER BY ts",
                      (host, now() - days * 86400))
        if len(rows) < 2:
            return {"supplies": {}, "pages_per_day": None, "samples": len(rows)}

        series = {}
        for r in rows:
            try:
                data = json.loads(r["supplies"] or "{}")
            except ValueError:
                continue
            for name, pct in data.items():
                if isinstance(pct, int | float):
                    series.setdefault(name, []).append((r["ts"], float(pct)))

        out = {}
        for name, points in series.items():
            for i in range(len(points) - 1, 0, -1):
                if points[i][1] > points[i - 1][1] + 2:     # refilled
                    points = points[i:]
                    break
            if len(points) < 2:
                continue
            span = (points[-1][0] - points[0][0]) / 86400.0
            drop = points[0][1] - points[-1][1]
            if span < 1 or drop <= 0:
                out[name] = {"per_day": 0.0, "days_left": None, "span_days": round(span, 1)}
                continue
            per_day = drop / span
            out[name] = {"per_day": round(per_day, 2),
                         "days_left": int(points[-1][1] / per_day),
                         "span_days": round(span, 1)}

        pages = [(r["ts"], r["pages"]) for r in rows if isinstance(r["pages"], int)]
        ppd = None
        if len(pages) >= 2:
            span = (pages[-1][0] - pages[0][0]) / 86400.0
            printed = pages[-1][1] - pages[0][1]
            if span >= 1 and printed >= 0:
                ppd = round(printed / span, 1)
        return {"supplies": out, "pages_per_day": ppd, "samples": len(rows)}

    # -- citadel ---------------------------------------------------------
    def put_records(self, rows):
        """Replace the catalogue snapshot, in one transaction.

        A whole-tree swap rather than a per-file diff: at this size it costs
        nothing, and it means a deleted file actually disappears instead of
        lingering as a record of something that is no longer written down.

        It is deliberately NOT called when the scan found nothing but errors —
        see `scan_failed`. Emptying the cache because the disk hiccuped would
        turn a degraded read into a catalogue that says the estate has no
        documentation at all.
        """
        ts = now()
        with self.lock:
            cur = self.db.cursor()
            cur.execute("DELETE FROM records")
            cur.execute("DELETE FROM record_links")
            if self.has_fts:
                cur.execute("DELETE FROM records_fts")
            for r in rows:
                cur.execute(
                    "INSERT INTO records(shelf,kind,slug,path,title,owner,"
                    "verified,review,bind,body,html,plain,blob,problems,"
                    "scanned_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["shelf"], r["kind"], r["slug"], r["path"], r["title"],
                     r["owner"], r["verified"], r["review"],
                     json.dumps(r.get("bind") or {}), r["body"], r["html"],
                     r["plain"], r["blob"],
                     json.dumps(r.get("problems") or []), ts))
                if self.has_fts:
                    cur.execute(
                        "INSERT INTO records_fts(shelf,title,plain) "
                        "VALUES(?,?,?)", (r["shelf"], r["title"], r["plain"]))
                for to_shelf, ok in r.get("links") or []:
                    cur.execute(
                        "INSERT OR REPLACE INTO record_links"
                        "(from_shelf,to_shelf,resolved) VALUES(?,?,?)",
                        (r["shelf"], to_shelf, 1 if ok else 0))
            self.db.commit()

    def records(self, kind=None):
        if kind:
            return self.q("SELECT * FROM records WHERE kind=? "
                          "ORDER BY title COLLATE NOCASE", (kind,))
        return self.q("SELECT * FROM records ORDER BY kind, title COLLATE NOCASE")

    # Not `record` — that is already taken by the method the check loop uses
    # to write a result, and shadowing it would have stopped every check
    # being recorded while the pages carried on looking fine.
    def get_record(self, shelf):
        return self.one("SELECT * FROM records WHERE shelf=?",
                        (str(shelf).upper(),))

    def record_by_slug(self, slug):
        return self.one(
            "SELECT * FROM records WHERE lower(replace(shelf,'.','-'))=?",
            (str(slug).lower(),))

    def record_age(self):
        """Seconds since the catalogue was last scanned, or None if never.
        The page shows this when the document tree is unreadable, because
        'what you are reading is from 40 minutes ago' is a different claim
        from 'this is current'."""
        row = self.one("SELECT MAX(scanned_ts) AS t FROM records")
        return None if not row or not row["t"] else now() - row["t"]

    def broken_links(self):
        return self.q("SELECT from_shelf, to_shelf FROM record_links "
                      "WHERE resolved=0 ORDER BY from_shelf, to_shelf")

    def search_records(self, text, limit=25):
        """FTS5 where the build has it, LIKE where it does not. The caller
        gets the same shape either way and `has_fts` says which ran."""
        q = (text or "").strip()
        if not q:
            return []
        if self.has_fts:
            # Quote the term so a stray " or NEAR cannot become syntax.
            safe = '"' + q.replace('"', '""') + '"*'
            try:
                return self.q(
                    "SELECT r.* FROM records_fts f JOIN records r "
                    "ON r.shelf=f.shelf WHERE records_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (safe, limit))
            except sqlite3.OperationalError:
                pass                       # fall through to LIKE
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        return self.q(
            "SELECT * FROM records WHERE shelf LIKE ? OR title LIKE ? "
            "OR plain LIKE ? ORDER BY kind, title COLLATE NOCASE LIMIT ?",
            (like, like, like, limit))

    def add_doc_commit(self, sha, author, shelf, path, summary, via,
                       pushed=False, ts=None):
        self.x("INSERT OR REPLACE INTO doc_commits"
               "(sha,ts,author,shelf,path,summary,via,pushed) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (sha, ts or now(), author, shelf, path, summary, via,
                1 if pushed else 0))

    def doc_commits(self, shelf=None, limit=60):
        if shelf:
            return self.q("SELECT * FROM doc_commits WHERE shelf=? "
                          "ORDER BY ts DESC LIMIT ?", (shelf, limit))
        return self.q("SELECT * FROM doc_commits ORDER BY ts DESC LIMIT ?",
                      (limit,))

    def unpushed(self):
        return self.q("SELECT * FROM doc_commits WHERE pushed=0 ORDER BY ts")

    def mark_pushed(self, shas):
        for sha in shas:
            self.x("UPDATE doc_commits SET pushed=1 WHERE sha=?", (sha,))

    def prune(self, results_days=120, events_days=400):
        t = now()
        self.x("DELETE FROM results WHERE ts < ?", (t - results_days * 86400,))
        self.x("DELETE FROM events WHERE ts < ?", (t - events_days * 86400,))
        self.x("DELETE FROM pool_history WHERE ts < ?", (t - events_days * 86400,))
        self.x("DELETE FROM hostmetrics WHERE ts < ?", (t - 30 * 86400,))
        self.x("DELETE FROM printer_history WHERE ts < ?", (t - 400 * 86400,))
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

    def add_incident(self, title, severity, system, detail, owner,
                     impact="", detected="", started=""):
        ts = now()
        ref = self.next_ref(ts)
        self.s.x(
            "INSERT INTO incidents(ref,title,severity,system,detail,impact,"
            "detected,started,status,owner,opened_ts) "
            "VALUES(?,?,?,?,?,?,?,?,'open',?,?)",
            (ref, title, severity, system, detail, impact, detected, started,
             owner, ts))
        return self.s.one("SELECT * FROM incidents WHERE ref=?", (ref,))

    # Create-and-close was the whole surface, so a typo in a title was
    # permanent and an incident raised by mistake could only be "resolved" —
    # leaving a lie in the record rather than removing it.
    INCIDENT_FIELDS = ("title", "severity", "system", "detail", "impact",
                       "detected", "started", "root_cause",
                       "resolution", "prevention")

    def edit_incident(self, iid, **fields):
        sets, args = [], []
        for k in self.INCIDENT_FIELDS:          # allowlist, never the caller's keys
            if k in fields and fields[k] is not None:
                sets.append(f"{k}=?")
                args.append(fields[k])
        if not sets:
            return self.s.one("SELECT * FROM incidents WHERE id=?", (iid,))
        self.s.x(f"UPDATE incidents SET {', '.join(sets)} WHERE id=?",  # noqa: S608
                 (*args, iid))
        return self.s.one("SELECT * FROM incidents WHERE id=?", (iid,))

    def resolve_incident(self, iid, prevention, owner, resolution="",
                         root_cause=""):
        """Closing records two different things, and both matter.

        What was DONE is the thing you need at 2am next time the same symptom
        appears — it was missing until now, so the log kept the lesson and
        threw away the fix. What would have CAUGHT IT SOONER is the thing that
        stops it happening again, so it becomes a follow-up rather than a
        sentence nobody re-reads.
        """
        self.s.x("UPDATE incidents SET status='resolved', closed_ts=?, "
                 "prevention=?, resolution=?, root_cause=? WHERE id=?",
                 (now(), prevention, resolution, root_cause, iid))
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

    FOLLOWUP_FIELDS = ("action", "owner", "due")

    def edit_followup(self, fid, **fields):
        sets, args = [], []
        for k in self.FOLLOWUP_FIELDS:
            if k in fields and fields[k] is not None:
                sets.append(f"{k}=?")
                args.append(fields[k])
        if not sets:
            return self.s.one("SELECT * FROM followups WHERE id=?", (fid,))
        self.s.x(f"UPDATE followups SET {', '.join(sets)} WHERE id=?",  # noqa: S608
                 (*args, fid))
        return self.s.one("SELECT * FROM followups WHERE id=?", (fid,))

    def delete_followup(self, fid):
        row = self.s.one("SELECT * FROM followups WHERE id=?", (fid,))
        if row:
            self.s.x("DELETE FROM followups WHERE id=?", (fid,))
        return row

    # -- changes ---------------------------------------------------------
    def changes(self, limit=100):
        return self.s.q("SELECT * FROM changes ORDER BY ts DESC LIMIT ?", (limit,))

    def add_change(self, title, who, system, detail, ts=None):
        return self.s.x("INSERT INTO changes(ts,title,who,system,detail) VALUES(?,?,?,?,?)",
                        (ts or now(), title, who, system, detail))

    CHANGE_FIELDS = ("title", "system", "detail")

    def edit_change(self, cid, **fields):
        sets, args = [], []
        for k in self.CHANGE_FIELDS:
            if k in fields and fields[k] is not None:
                sets.append(f"{k}=?")
                args.append(fields[k])
        if not sets:
            return self.s.one("SELECT * FROM changes WHERE id=?", (cid,))
        self.s.x(f"UPDATE changes SET {', '.join(sets)} WHERE id=?",  # noqa: S608
                 (*args, cid))
        return self.s.one("SELECT * FROM changes WHERE id=?", (cid,))

    def delete_change(self, cid):
        row = self.s.one("SELECT * FROM changes WHERE id=?", (cid,))
        if row:
            self.s.x("DELETE FROM changes WHERE id=?", (cid,))
        return row

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

    # -- array and disk integrity ----------------------------------------
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
