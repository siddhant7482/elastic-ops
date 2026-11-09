"""
Citadel — the estate catalogue.

Every real thing in the estate has exactly one record, and the written
knowledge attaches to that record instead of floating in a folder. A record is
a markdown file with TOML front matter; the front matter declares what the
record is *about*, and that declaration is the whole coupling between prose
and live data.

    estate-docs/
      host/pepper.md          HST.PEP
      guest/1100-cfp.md       GST.1100
      app/winman.md           APP.WIN
      runbook/restore-cfp.md  RUN.CFP

The path is the shelf mark, lowercased. No database row decides where a
document lives, so the repository stays readable and editable with nothing but
a text editor — which is the property that lets us walk away from this later
without losing anything.

Phase 1 reads a plain directory. Git is not required to read the catalogue,
only to write it, so none of this is blocked on a push credential existing on
pepper. The history and commit paths land in phase 2 and sit on top.

The catalogue does not keep its own list of hosts, guests, applications or
devices. It reads the lists the portal already maintains and attaches a
document to each entry, which is what makes "this thing exists and nobody has
written it down" a *detectable* condition rather than something noticed
eighteen months later.

Shelf marks use a full stop, not a middle dot: HST.PEP. The design sketch used
U+00B7 and it looked better on a page, but a mark you cannot type is a mark
nobody searches for.
"""

import hashlib
import os
import re
import tomllib
from datetime import date, datetime, timezone

import markdown
import repo
from engine import log

# kind -> shelf prefix. The order here is the order the catalogue lists them.
KINDS = {
    "host": "HST",
    "guest": "GST",
    "app": "APP",
    "device": "DEV",
    "runbook": "RUN",
    "register": "REG",
    "governance": "GOV",
    "order": "ORD",
}
PREFIXES = {v: k for k, v in KINDS.items()}
OBJECT_KINDS = ("host", "guest", "app", "device")   # kinds with a live counterpart

DEFAULT_REVIEW = "6 months"
_MONTHS = re.compile(r"(\d+)\s*(month|year|week|day)", re.I)
_SHELF_OK = re.compile(r"^[A-Z]{3}\.[A-Z0-9][A-Z0-9-]*$")


def _today():
    return datetime.now(timezone.utc).date()


def months_between(iso, until=None):
    """Whole-ish months since an ISO date. Returns None for a missing or
    unparseable date, which the caller must treat as 'never verified' rather
    than 'verified today'."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(str(iso).strip()[:10])
    except ValueError:
        return None
    return ((until or _today()) - d).days / 30.44


def review_months(text):
    """'6 months' -> 6. Anything unrecognised falls back to the default rather
    than to infinity: a review period nobody can parse should still come due."""
    m = _MONTHS.search(str(text or ""))
    if not m:
        return 6
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"month": n, "year": n * 12, "week": max(1, n // 4),
            "day": max(1, n // 30)}[unit]


def slug_of(shelf):
    """HST.PEP -> hst-pep, for URLs."""
    return re.sub(r"[^a-z0-9]+", "-", str(shelf).lower()).strip("-")


def derive_shelf(kind, slug):
    """A shelf mark from the file name, when the front matter does not give
    one. Guests key on their VMID because that is what people say; everything
    else takes the first three alphanumerics of the slug."""
    prefix = KINDS.get(kind, "DOC")
    digits = re.match(r"(\d+)", slug or "")
    if kind == "guest" and digits:
        return f"{prefix}.{digits.group(1)}"
    letters = re.sub(r"[^A-Za-z0-9]", "", slug or "")[:3].upper()
    return f"{prefix}.{letters or 'X'}"



_STAMP = re.compile(r"Generated <b>[^<]*</b>|generated \\d{4}-\\d\\d-\\d\\d[^.<]*")


def _same_but_for_stamp(a, b):
    return _STAMP.sub("", a or "") == _STAMP.sub("", b or "")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""

class Record:
    """One record. `bind` is the only part that touches live data."""

    __slots__ = ("shelf", "kind", "slug", "path", "title", "owner", "verified",
                 "review", "bind", "body", "blob", "problems")

    def __init__(self, **kw):
        for f in self.__slots__:
            setattr(self, f, kw.get(f))
        self.bind = self.bind or {}
        self.problems = self.problems or []

    @property
    def url(self):
        return "/citadel/r/" + slug_of(self.shelf)

    @property
    def overdue(self):
        """Months past its review date, or None if it is in date. A record
        with no verified date at all is overdue by definition — we do not know
        when anyone last looked at it, which is the same risk."""
        age = months_between(self.verified)
        if age is None:
            return 9999.0
        limit = review_months(self.review)
        return age - limit if age > limit else None

    def as_row(self):
        return {"shelf": self.shelf, "kind": self.kind, "slug": self.slug,
                "title": self.title, "owner": self.owner,
                "verified": self.verified, "review": self.review,
                "url": self.url, "overdue": self.overdue}


def parse(text, kind, slug, path=""):
    """One file into a Record. A broken front matter block is a problem on the
    record, not an exception: one bad file must not take the catalogue down."""
    fm_text, body = markdown.split_front_matter(text)
    problems, fm = [], {}
    if fm_text.strip():
        try:
            fm = tomllib.loads(fm_text)
        except tomllib.TOMLDecodeError as e:
            problems.append(f"front matter is not valid TOML: {e}")
    elif text.strip():
        problems.append("no front matter")

    shelf = str(fm.get("shelf") or derive_shelf(kind, slug)).strip().upper()
    if not _SHELF_OK.match(shelf):
        problems.append(f"shelf mark {shelf!r} is not PREFIX.KEY")
    declared = fm.get("kind")
    if declared and declared != kind:
        problems.append(f"front matter says kind={declared!r}, "
                        f"but the file is in {kind}/")

    bind = fm.get("bind") or {}
    if not isinstance(bind, dict):
        problems.append("[bind] is not a table")
        bind = {}

    return Record(
        shelf=shelf, kind=kind, slug=slug, path=path,
        title=str(fm.get("title") or slug or shelf),
        owner=str(fm.get("owner") or "").strip(),
        verified=str(fm.get("verified") or "").strip(),
        review=str(fm.get("review") or DEFAULT_REVIEW).strip(),
        bind=bind, body=body,
        blob=hashlib.sha1(text.encode("utf-8"),
                          usedforsecurity=False).hexdigest()[:7],
        problems=problems)


class Catalogue:
    """The records on disk, plus what the portal knows about the estate."""

    def __init__(self, root):
        self.root = root
        self.records = {}          # shelf -> Record
        self.by_path = {}          # relative path -> shelf
        self.errors = []           # files that could not be read at all
        self._html = {}            # blob -> (html, links)

    # -- reading ---------------------------------------------------------
    def scan(self):
        """Re-read the whole tree. Cheap at this size — 34 files today — and
        rescanning beats trying to work out what changed."""
        self.records, self.by_path, self.errors = {}, {}, []
        if not self.root or not os.path.isdir(self.root):
            self.errors.append(f"no catalogue at {self.root}")
            return self
        for kind in KINDS:
            d = os.path.join(self.root, kind)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".md") or name.startswith("."):
                    continue
                path = os.path.join(d, name)
                rel = f"{kind}/{name}"
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except OSError as e:
                    self.errors.append(f"{rel}: {e}")
                    continue
                rec = parse(text, kind, name[:-3], rel)
                if rec.shelf in self.records:
                    other = self.records[rec.shelf].path
                    rec.problems.append(f"shelf mark collides with {other}")
                self.records[rec.shelf] = rec
                self.by_path[rel] = rec.shelf
        return self

    def exists(self, shelf):
        return str(shelf).strip().upper() in self.records

    def get(self, shelf):
        return self.records.get(str(shelf).strip().upper())

    def by_slug(self, slug):
        for rec in self.records.values():
            if slug_of(rec.shelf) == slug:
                return rec
        return None

    def of_kind(self, kind):
        return sorted((r for r in self.records.values() if r.kind == kind),
                      key=lambda r: r.title.lower())

    # -- rendering -------------------------------------------------------
    def html(self, rec):
        """Rendered body plus its record links, memoised on the content hash
        so a page re-renders exactly when it changes and never otherwise."""
        hit = self._html.get(rec.blob)
        if hit is None:
            hit = markdown.render(rec.body, resolve=self.exists,
                                  media_prefix="/citadel/media/")
            self._html[rec.blob] = hit
        return hit

    def links(self, rec):
        return self.html(rec)[1]

    def search_text(self, rec):
        return markdown.plain(rec.body)


# ---------------------------------------------------------------- live side

def live_objects(store, portal, inventory, errors=None):
    """Everything the portal knows exists, in one shape.

    Nothing here is entered by hand. Hosts and applications come from
    inventory.toml, guests from what the collectors pushed, devices from what
    answered SNMP.

    A source that cannot be read appends to `errors` rather than being
    swallowed, and the caller must pass that list into `findings`. This is not
    tidiness: an unreadable guests table would otherwise look exactly like an
    estate with no guests, and coverage would silently report *better* than
    the truth. Failing quiet in the flattering direction is the one failure
    this system exists to prevent.
    """
    errors = [] if errors is None else errors
    out = []
    for h in (inventory or {}).get("hosts", []):
        name = str(h.get("name") or "").strip()
        if name:
            out.append({"kind": "host", "key": name, "label": name,
                        "detail": str(h.get("address") or ""),
                        "bind": {"host": name}})
    for a in (inventory or {}).get("apps", []):
        name = str(a.get("name") or "").strip()
        if name:
            out.append({"kind": "app", "key": name, "label": name,
                        "detail": str(a.get("host") or a.get("note") or ""),
                        "bind": {"app": name}})
    try:
        for g in portal.guests():
            out.append({"kind": "guest", "key": str(g["vmid"]),
                        "label": f"{g['vmid']} {g['name']}",
                        "detail": f"on {g['host']}",
                        "bind": {"guest": int(g["vmid"])}})
    except Exception as e:                                       # noqa: BLE001
        errors.append(f"cannot read guests: {e}")
    try:
        for p in store.printers():
            out.append({"kind": "device", "key": p["host"],
                        "label": str(p["name"] or p["host"]),
                        "detail": str(p["host"]),
                        "bind": {"printer": p["host"]}})
    except Exception as e:                                       # noqa: BLE001
        errors.append(f"cannot read printers: {e}")
    return out


def _bound(rec, obj):
    """Does this record claim this object?"""
    b, ob = rec.bind, obj["bind"]
    for k, v in ob.items():
        if k in b and str(b[k]).strip().lower() == str(v).strip().lower():
            return True
    return False


def match_records(catalogue, live):
    """(object -> record or None, record -> object or None).

    Two dictionaries rather than one, because the interesting questions run
    both ways: which objects have no record, and which records point at
    nothing.
    """
    for_obj, for_rec = {}, {}
    objs = {}
    for obj in live:
        key = (obj["kind"], obj["key"])
        objs[key] = obj
        for_obj[key] = None
    for rec in catalogue.records.values():
        if rec.kind not in OBJECT_KINDS:
            continue
        hit = None
        for obj in objs.values():
            if obj["kind"] == rec.kind and _bound(rec, obj):
                hit = obj
                break
        for_rec[rec.shelf] = hit
        if hit is not None:
            for_obj[(hit["kind"], hit["key"])] = rec
    return for_obj, for_rec


def coverage(catalogue, live):
    for_obj, _ = match_records(catalogue, live)
    have = sum(1 for r in for_obj.values() if r is not None)
    return {"have": have, "total": len(for_obj)}


# ---------------------------------------------------------------- findings

PAGE, NOTIFY, QUIET = "page", "notify", "quiet"

# One check per finding *kind*, not per finding. Six red rows that each name a
# category is readable; ninety that each name a record is a wall, and a wall is
# something people learn to scroll past.
#
# The severities are the argument of the whole feature. Only two page, and both
# are conditions where the documentation is actively lying: a restore runbook
# nobody has verified, and a live source we could not read — which makes
# coverage understate the gap and the estate look tidier than it is.
CHECKS = {
    "unverified-runbook": ("docs: unverified runbook", PAGE),
    "live-source":        ("docs: live source unreadable", PAGE),
    "undocumented":       ("docs: undocumented objects", NOTIFY),
    "orphaned":           ("docs: orphaned records", NOTIFY),
    "unreadable":         ("docs: tree unreadable", NOTIFY),
    "stale":              ("docs: stale records", QUIET),
    "broken-link":        ("docs: broken cross-references", QUIET),
    "unowned":            ("docs: unowned records", QUIET),
    "malformed":          ("docs: malformed front matter", QUIET),
}


def findings(catalogue, live, accepted=(), users=(), live_errors=()):
    """The six conditions, computed rather than stored.

    Only one of them pages, and it is the right one. An unverified restore
    runbook is the condition that actually hurts: you find out it is wrong at
    the worst possible moment. The other five are nags, and a nag that pages
    gets silenced, which teaches people to silence things.
    """
    acc = {str(a.get("check") or "").strip().upper(): a for a in accepted}
    known = {str(u).strip().lower() for u in users if u}
    out = []

    def add(sev, kind, shelf, what, **extra):
        if shelf and shelf.upper() in acc:
            return
        row = {"severity": sev, "kind": kind, "shelf": shelf, "what": what}
        row.update(extra)
        out.append(row)

    for_obj, for_rec = match_records(catalogue, live)

    for (okind, okey), rec in sorted(for_obj.items()):
        if rec is not None:
            continue
        shelf = derive_shelf(okind, okey)
        add(NOTIFY, "undocumented", shelf,
            f"{okind} {okey} exists in the estate and has no record",
            create={"kind": okind, "key": okey})

    for rec in sorted(catalogue.records.values(), key=lambda r: r.shelf):
        if rec.kind in OBJECT_KINDS and rec.bind and for_rec.get(rec.shelf) is None:
            add(NOTIFY, "orphaned", rec.shelf,
                f"{rec.shelf} binds {rec.bind} — no such object any more")

        over = rec.overdue
        if over is not None:
            age = months_between(rec.verified)
            when = "never verified" if age is None else \
                f"verified {rec.verified}, {age:.0f} months ago"
            if rec.kind == "runbook":
                add(PAGE, "unverified-runbook", rec.shelf,
                    f"{rec.shelf} {rec.title} — {when}, policy is "
                    f"{review_months(rec.review)} months")
            else:
                add(QUIET, "stale", rec.shelf,
                    f"{rec.shelf} {rec.title} — {when}")

        for shelf, ok in catalogue.links(rec):
            if not ok:
                add(QUIET, "broken-link", rec.shelf,
                    f"{rec.shelf} links [[{shelf}]] — no such record")

        if not rec.owner:
            add(QUIET, "unowned", rec.shelf, f"{rec.shelf} has no owner")
        elif known and rec.owner.lower() not in known:
            add(QUIET, "unowned", rec.shelf,
                f"{rec.shelf} is owned by {rec.owner}, who has no account")

        for p in rec.problems:
            add(QUIET, "malformed", rec.shelf, f"{rec.path}: {p}")

    for e in catalogue.errors:
        out.append({"severity": NOTIFY, "kind": "unreadable", "shelf": None,
                    "what": e})
    # A live source we could not read is a *page*, not a nag. Coverage and
    # every undocumented finding are computed against that source, so while
    # it is down the catalogue reports an estate that is smaller and tidier
    # than the real one.
    for e in live_errors:
        out.append({"severity": PAGE, "kind": "live-source", "shelf": None,
                    "what": f"{e} — coverage is understated until this is fixed"})

    order = {PAGE: 0, NOTIFY: 1, QUIET: 2}
    out.sort(key=lambda f: (order[f["severity"]], f["kind"], f["shelf"] or ""))
    return out


# ---------------------------------------------------------------- panels

def _pct(used, total):
    if not used or not total:
        return None
    return round(100.0 * used / total)


def _gb(n):
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


def _when(ts):
    if not ts:
        return "never"
    mins = (datetime.now(timezone.utc).timestamp() - ts) / 60
    if mins < 90:
        return f"{mins:.0f}m ago"
    if mins < 2880:
        return f"{mins / 60:.0f}h ago"
    return f"{mins / 1440:.0f}d ago"


def panels(rec, store, portal, errors=None):
    """The live figures for one record, resolved from its [bind] block.

    Every value here is measured, never typed. The template puts them in a
    bordered panel for exactly that reason: if it is in a panel, nobody wrote
    it, and it cannot be out of date in the way prose can.

    A binding that names something the portal does not know returns no panel
    rather than a zero. A zero is a claim; absence is the truth.
    """
    errors = [] if errors is None else errors
    out = []

    def add(label, value, note=""):
        out.append({"label": label, "value": value, "note": note})

    b = rec.bind or {}

    if b.get("host"):
        host = str(b["host"])
        try:
            latest = {h["host"]: h for h in portal.host_latest()}.get(host)
        except Exception as e:                                   # noqa: BLE001
            latest, _ = None, errors.append(f"host metrics: {e}")
        if latest:
            pct = _pct(latest.get("mem_used"), latest.get("mem_total"))
            add("Memory", f"{pct}%" if pct is not None else "—",
                f"of {_gb(latest.get('mem_total'))}")

        try:
            rows = [a for a in store.arrays() if a["host"] == host]
            bad = [a for a in rows if (a["degraded"] or 0) > 0
                   or str(a["state"] or "").lower() not in
                   ("clean", "active", "online", "ok", "optimal")]
            if rows:
                add("Arrays", f"{len(bad)} degraded" if bad else f"{len(rows)} ok",
                    bad[0]["name"] if bad else f"{len(rows)} on this host")
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"arrays: {e}")

        if b.get("guests"):
            try:
                gs = [g for g in portal.guests() if g["host"] == host]
                add("Guests", str(len(gs)), "on this host")
            except Exception as e:                               # noqa: BLE001
                errors.append(f"guests: {e}")

        if b.get("backups"):
            try:
                rows = [x for x in portal.backups() if x["host"] == host]
                last = max((x["last_ts"] or 0) for x in rows) if rows else 0
                bad = [x for x in rows if x["state"] != "ok"]
                add("Backup", _when(last),
                    f"{len(bad)} not ok" if bad else f"{len(rows)} covered")
            except Exception as e:                               # noqa: BLE001
                errors.append(f"backups: {e}")

    if b.get("guest"):
        vmid = str(b["guest"])
        try:
            g = next((x for x in portal.guests() if str(x["vmid"]) == vmid), None)
        except Exception as e:                                   # noqa: BLE001
            g = None
            errors.append(f"guests: {e}")
        if g:
            add("VMID", vmid, f"on {g['host']}")
            add("State", str(g["status"] or "—"), str(g["kind"] or ""))
            if g["addresses"]:
                add("Address", g["addresses"].split(",")[0], "from the agent")
        try:
            bk = next((x for x in portal.backups() if str(x["vmid"]) == vmid), None)
            if bk:
                add("Backup", _when(bk["last_ts"]), str(bk["state"] or ""))
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"backups: {e}")

    if b.get("printer"):
        try:
            p = next((x for x in store.printers()
                      if x["host"] == str(b["printer"])), None)
            if p:
                add("Status", str(p["status"] or "—"), str(p["host"]))
                if p.get("supplies"):
                    worst = min(p["supplies"],
                                key=lambda s: s.get("pct", 101) or 101)
                    add("Lowest supply", f"{worst.get('pct', '?')}%",
                        str(worst.get("name", "")))
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"printers: {e}")

    for name in (b.get("checks") or []):
        try:
            st = store.get(str(name))
            if st:
                add("Check", str(st["status"] or "—"), str(name))
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"check {name}: {e}")

    return out


def carried_guests(rec, portal):
    """For the live diagram on a host record: what it carries, and which of
    those are written down. The undocumented ones are the point of drawing
    it at all."""
    host = (rec.bind or {}).get("host")
    if not host:
        return []
    try:
        return [{"vmid": g["vmid"], "name": g["name"], "status": g["status"]}
                for g in portal.guests() if g["host"] == str(host)]
    except Exception:                                            # noqa: BLE001
        return []


# ---------------------------------------------------------------- service

class Service:
    """Owns the catalogue: scans the tree, caches it, answers the API.

    The markdown on disk is the source of truth. This keeps a parsed copy in
    memory and a rendered copy in SQLite, and re-reads on a timer rather than
    on every request. At 34 files a rescan is a few milliseconds; the cache
    exists so a page view costs nothing, and so the catalogue still answers
    when the tree underneath it goes away.
    """

    def __init__(self, root, store, portal, inventory, interval=60,
                 git=True):
        self.root = root
        self.repo = repo.Repo(root) if git else None
        self.store = store
        self.portal = portal
        self.inventory = inventory or {}
        self.interval = interval
        self.cat = Catalogue(root)
        self.last_scan = 0
        self.last_ok = 0
        self.live_errors = []
        self.degraded = False      # serving the cache because the tree failed

    # -- scanning --------------------------------------------------------
    def scan(self, force=False, now_ts=None):
        now_ts = now_ts or int(datetime.now(timezone.utc).timestamp())
        if not force and now_ts - self.last_scan < self.interval:
            return False
        self.last_scan = now_ts
        cat = Catalogue(self.root).scan()

        # Do not overwrite a good cache with a bad read. A tree that has gone
        # missing is a reason to keep showing what we last saw and say so --
        # not a reason to report that the estate has no documentation.
        if not cat.records and cat.errors and self.store.records():
            self.degraded = True
            return False

        self.cat = cat
        self.degraded = bool(cat.errors)
        self.last_ok = now_ts
        rows = []
        for rec in cat.records.values():
            html, links = cat.html(rec)
            rows.append({**rec.as_row(), "bind": rec.bind, "body": rec.body,
                         "html": html, "plain": cat.search_text(rec),
                         "blob": rec.blob, "problems": rec.problems,
                         "links": links, "path": rec.path})
        self.store.put_records(rows)
        return True

    # -- reading ---------------------------------------------------------
    def live(self):
        self.live_errors = []
        return live_objects(self.store, self.portal, self.inventory,
                            errors=self.live_errors)

    def users(self):
        """Who has an account, for the unowned check. Read here rather than
        plumbed in from the API, so the sweep and the page agree."""
        try:
            return [r["username"] for r in
                    self.store.q("SELECT username FROM users")]
        except Exception:                                        # noqa: BLE001
            return []

    def findings(self, users=None):
        live = self.live()
        accepted = self.inventory.get("accepted", [])
        return findings(self.cat, live, accepted=accepted,
                        users=self.users() if users is None else users,
                        live_errors=self.live_errors)

    def rollup(self):
        """Findings grouped into one row per kind, for the notifier.

        Returns {kind: (check_name, severity, count, detail)} covering every
        kind in CHECKS, including the ones with nothing wrong — a category
        that has cleared must be able to transition back up, or a resolved
        problem stays red forever and the board stops being read.
        """
        found = {}
        for f in self.findings():
            found.setdefault(f["kind"], []).append(f)
        out = {}
        for kind, (name, sev) in CHECKS.items():
            rows = found.get(kind, [])
            if rows:
                shelves = [r["shelf"] for r in rows if r["shelf"]]
                head = ", ".join(shelves[:4]) or rows[0]["what"][:80]
                more = f" and {len(shelves) - 4} more" if len(shelves) > 4 else ""
                detail = f"{len(rows)}: {head}{more}"
            else:
                detail = "none"
            out[kind] = (name, sev, len(rows), detail)
        return out

    def summary(self):
        live = self.live()
        cov = coverage(self.cat, live)
        f = self.findings()
        counts = {k: len(self.cat.of_kind(k)) for k in KINDS}
        words = sum(len(self.cat.search_text(r).split())
                    for r in self.cat.records.values())
        return {
            "counts": counts,
            "records": len(self.cat.records),
            "words": words,
            "coverage": cov,
            "findings": len(f),
            "paging": sum(1 for x in f if x["severity"] == PAGE),
            "degraded": self.degraded,
            "errors": self.cat.errors,
            "age": self.store.record_age(),
            "fts": getattr(self.store, "has_fts", False),
        }

    def index(self, kind):
        """One index, with the live objects of that kind folded in so an
        undocumented thing appears in the same table as the documented ones
        rather than hiding on a findings page."""
        live = [o for o in self.live() if o["kind"] == kind]
        for_obj, _ = match_records(self.cat, self.live())
        rows = []
        seen = set()
        for obj in live:
            rec = for_obj.get((obj["kind"], obj["key"]))
            if rec:
                seen.add(rec.shelf)
            rows.append({"label": obj["label"], "detail": obj["detail"],
                         "record": rec.as_row() if rec else None,
                         "create": None if rec else
                                   {"kind": obj["kind"], "key": obj["key"]}})
        # Records of this kind with no live counterpart still belong in the
        # list -- a runbook has none by definition, and an orphan must not
        # vanish from the page that would let you find and archive it.
        for rec in self.cat.of_kind(kind):
            if rec.shelf not in seen:
                rows.append({"label": rec.title, "detail": "",
                             "record": rec.as_row(), "create": None,
                             "orphan": kind in OBJECT_KINDS and bool(rec.bind)})
        return rows

    def record(self, slug):
        rec = self.cat.by_slug(slug)
        if not rec:
            return None
        html, links = self.cat.html(rec)
        errs = []
        return {
            **rec.as_row(),
            "body": rec.body,
            "html": html,
            "bind": rec.bind,
            "problems": rec.problems,
            "links": [{"shelf": sh, "resolved": ok} for sh, ok in links],
            "panels": panels(rec, self.store, self.portal, errors=errs),
            "guests": carried_guests(rec, self.portal),
            "runbooks": [r.as_row() for r in self.cat.of_kind("runbook")
                         if rec.shelf in (r.bind.get("records") or [])],
            "panel_errors": errs,
        }

    def search(self, q, limit=25):
        rows = self.store.search_records(q, limit)
        out = []
        for r in rows:
            rec = self.cat.get(r["shelf"])
            out.append({"shelf": r["shelf"], "kind": r["kind"],
                        "title": r["title"],
                        "url": "/citadel/r/" + slug_of(r["shelf"]),
                        "overdue": rec.overdue if rec else None})
        return out


    # -- writing ---------------------------------------------------------
    def writable(self):
        """(can we write, why not). A host with no git is a catalogue you can
        read but not edit, which is a far better failure than one that is
        down."""
        if self.repo is None:
            return False, "no repository configured"
        return self.repo.available()

    def _email(self, user):
        return f"{user}@elasticgroup.co.uk" if user else "unknown@elastic-ops"

    def load_for_edit(self, slug):
        """The source, plus the blob hash that identifies the version being
        edited. The hash travels down to the browser with the text and comes
        back with the save; that round trip is the whole conflict story."""
        rec = self.cat.by_slug(slug)
        if not rec:
            return None
        ok, why = self.writable()
        fm, body = markdown.split_front_matter(
            _read(os.path.join(self.root, rec.path)))
        return {
            "shelf": rec.shelf, "kind": rec.kind, "slug": rec.slug,
            "path": rec.path, "title": rec.title, "owner": rec.owner,
            "verified": rec.verified, "review": rec.review,
            "bind": rec.bind, "body": body, "front_matter": fm,
            "blob": self.repo.blob(rec.path) if self.repo else None,
            "writable": ok, "why": why,
        }

    def save(self, slug, body, fields, expected_blob, user, summary):
        """Write one record as one person.

        Returns {"sha": ...} on success, or raises repo.Conflict, which the
        API turns into a 409 carrying both versions. Nothing is merged
        automatically — the person decides, and that is a decision rather
        than a default.
        """
        ok, why = self.writable()
        if not ok:
            raise repo.RepoError(why)
        rec = self.cat.by_slug(slug)
        if not rec:
            raise repo.RepoError("no such record")

        current = _read(os.path.join(self.root, rec.path))
        fm, _ = markdown.split_front_matter(current)
        fm = update_front_matter(fm, fields or {})
        text = compose(fm, body)

        sha = self.repo.write(rec.path, text, user, self._email(user),
                              summary, expected_blob=expected_blob)
        self.scan(force=True)
        if sha:
            self._log_commit(sha, user, rec.path, summary, "browser")
        return {"sha": sha, "shelf": rec.shelf,
                "blob": self.repo.blob(rec.path)}

    def create(self, kind, key, user, label=None):
        """Scaffold a record from a live object and commit the stub.

        The bindings are written for you because the portal already knows
        them. Getting them wrong by hand is how a record ends up describing
        nothing.
        """
        ok, why = self.writable()
        if not ok:
            raise repo.RepoError(why)
        if kind not in KINDS:
            raise repo.RepoError(f"no such kind: {kind}")

        obj = None
        for o in self.live():
            if o["kind"] == kind and str(o["key"]) == str(key):
                obj = o
                break
        title = label or (obj["label"] if obj else str(key))
        bind = dict(obj["bind"]) if obj else {}
        if kind == "host":
            bind.update({"guests": True, "backups": True})
        slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-") or "x"
        if kind == "guest":
            slug = f"{key}-{slug}"
        path = f"{kind}/{slug}.md"
        if os.path.exists(os.path.join(self.root, path)):
            raise repo.RepoError(f"{path} already exists")

        shelf, text = scaffold(kind, str(key), slug, title, bind,
                               user, _today().isoformat())
        if self.cat.exists(shelf):
            raise repo.RepoError(f"{shelf} is already taken")
        sha = self.repo.write(path, text, user, self._email(user),
                              f"Create {shelf} from the live object")
        self.scan(force=True)
        if sha:
            self._log_commit(sha, user, path, f"Create {shelf}", "scaffold")
        return {"shelf": shelf, "slug": slug_of(shelf), "sha": sha}

    def archive(self, slug, user, reason=""):
        ok, why = self.writable()
        if not ok:
            raise repo.RepoError(why)
        rec = self.cat.by_slug(slug)
        if not rec:
            raise repo.RepoError("no such record")
        msg = f"Archive {rec.shelf}" + (f" — {reason}" if reason else "")
        sha = self.repo.remove(rec.path, user, self._email(user), msg)
        self.scan(force=True)
        if sha:
            self._log_commit(sha, user, rec.path, msg, "browser")
        return {"sha": sha}

    # -- history ---------------------------------------------------------
    def _log_commit(self, sha, user, path, summary, via):
        shelf = self.cat.by_path.get(path)
        self.store.add_doc_commit(sha, user, shelf, path, summary, via,
                                  pushed=False)

    def sync_log(self, limit=200):
        """Fold git's own history into doc_commits.

        The history *is* git's; this table only caches it so a page view does
        not shell out. It can be dropped and rebuilt from the repository at
        any time, which is why nothing depends on it except `pushed` — and
        that is local knowledge git does not have.
        """
        if self.repo is None:
            return 0
        known = {r["sha"] for r in self.store.doc_commits(limit=limit * 2)}
        added = 0
        for c in self.repo.log(limit=limit):
            if c["sha"] in known:
                continue
            self.store.add_doc_commit(c["sha"], c["author"], None, None,
                                      c["summary"], "git", pushed=True,
                                      ts=c["ts"])
            added += 1
        return added

    def history(self, slug):
        rec = self.cat.by_slug(slug)
        if not rec or self.repo is None:
            return None
        pushed = {r["sha"]: r["pushed"] for r in self.store.doc_commits(limit=400)}
        rows = []
        for c in self.repo.log(rec.path, limit=60):
            rows.append({**c, "pushed": bool(pushed.get(c["sha"], 1))})
        return {"shelf": rec.shelf, "title": rec.title, "path": rec.path,
                "commits": rows}

    def activity(self, limit=80):
        rows = self.store.doc_commits(limit=limit)
        for r in rows:
            r["short"] = (r["sha"] or "")[:7]
        return rows

    def repo_status(self):
        if self.repo is None:
            return {"ok": False, "why": "no repository configured"}
        st = self.repo.status()
        st["unpushed"] = len(self.store.unpushed())
        return st

    # -- the remote ------------------------------------------------------
    def push(self):
        ok, why = self.writable()
        if not ok:
            return False, why
        done, msg = self.repo.push()
        if done:
            self.store.mark_pushed([r["sha"] for r in self.store.unpushed()])
        return done, msg

    def pull(self):
        ok, why = self.writable()
        if not ok:
            return False, why
        done, msg = self.repo.pull()
        if done:
            self.scan(force=True)
            self.sync_log()
        return done, msg

    MANUAL_PATH = "dist/estate-manual.md"

    def publish_manual(self, user="citadel", force=False):
        """Render the whole catalogue to one offline file and commit it.

        Written through the repository like any other change, so it is
        versioned, pushed with everything else, and visible in the activity
        log. If it were written straight to disk it would be the one artefact
        that never left this host — which is the exact failure it exists to
        prevent.

        The file is committed as .html but kept under dist/, so a reader
        never confuses it with a record.
        """
        import manual                      # late: only the writer needs it
        ok, why = self.writable()
        if not ok:
            return None, why
        self.scan()
        html = manual.build(self, generated_by=user)
        rel = "dist/estate-manual.html"
        full = os.path.join(self.root, rel)
        if not force and os.path.isfile(full):
            try:
                with open(full, encoding="utf-8") as fh:
                    old = fh.read()
                # Only the timestamp changed: committing that every night
                # would bury real edits under a year of noise.
                if _same_but_for_stamp(old, html):
                    return None, "unchanged"
            except OSError:
                pass
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        os.replace(tmp, full)
        sha = None
        try:
            self.repo._run(["add", "--", rel])
            code, out, err = self.repo._run(
                ["-c", "user.name=citadel", "-c",
                 "user.email=citadel@elastic-ops.local",
                 "commit", "--no-verify", "-m",
                 f"Rebuild the offline manual ({len(self.cat.records)} records)",
                 "--", rel], check=False)
            if code == 0:
                _, head, _ = self.repo._run(["rev-parse", "HEAD"])
                sha = head.strip()
                self.store.add_doc_commit(sha, "citadel", None, rel,
                                          "Rebuild the offline manual",
                                          "manual", pushed=False)
        except repo.RepoError as e:
            return None, str(e)
        return sha, rel

    def manual_html(self):
        """The same file, served live. Useful for checking what the nightly
        job will produce without waiting for the night."""
        import manual
        self.scan()
        return manual.build(self, generated_by="this request")

    def sync_tick(self):
        """One round of keeping up with the remote. Called on a timer, never
        from a request: a save must not wait for the network."""
        if self.repo is None or not self.repo.has_remote():
            return
        try:
            if self.store.unpushed():
                self.push()
            self.pull()
        except repo.RepoError as e:
            log(f"citadel sync: {e}")

# ---------------------------------------------------------------- writing
_FM_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*=\s*)(.*)$")
EDITABLE = ("title", "owner", "verified", "review")


def _toml_str(value):
    """A TOML basic string. Only ever used for the four scalar fields the
    editor owns, so the escaping needed is small and complete."""
    s = str(value if value is not None else "")
    s = (s.replace("\\", "\\\\").replace('"', '\\"')
          .replace("\n", " ").replace("\r", " ").replace("\t", " "))
    return '"' + s + '"'


def update_front_matter(original, changes):
    """Rewrite only the keys the editor owns, in place.

    Deliberately line-based rather than parse-and-regenerate. tomllib has no
    writer, and more importantly a round trip through one would reorder keys,
    drop comments and reformat the [bind] table — turning a one-word change
    into a forty-line diff. The whole value of the git history is that a diff
    shows what a person actually changed.

    Keys the editor does not own are never touched. A key that is absent is
    appended above the first table header, so it stays in the scalar block.
    """
    lines = (original or "").split("\n")
    want = {k: v for k, v in (changes or {}).items() if k in EDITABLE}
    seen = set()
    out = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_table = True
        m = _FM_LINE.match(line)
        if m and not in_table and m.group(2) in want:
            key = m.group(2)
            seen.add(key)
            out.append(f"{m.group(1)}{key}{m.group(3)}{_toml_str(want[key])}")
        else:
            out.append(line)

    missing = [k for k in EDITABLE if k in want and k not in seen]
    if missing:
        new = [f"{k} = {_toml_str(want[k])}" for k in missing]
        head = next((i for i, x in enumerate(out) if x.strip().startswith("[")),
                    len(out))
        # keep a blank line between the scalars and the first table
        while head > 0 and not out[head - 1].strip():
            head -= 1
        out = out[:head] + new + out[head:]

    return "\n".join(out)


def compose(front_matter, body):
    fm = (front_matter or "").strip("\n")
    body = (body or "").lstrip("\n")
    return f"+++\n{fm}\n+++\n\n{body}" if fm else body


SCAFFOLD = {
    "host": ["What it is", "How it is built", "If it dies", "Who owns it",
             "Where its credentials live"],
    "guest": ["What it is", "What runs on it", "If it dies"],
    "app": ["What it is", "Who uses it", "If it dies", "The thing that bites"],
    "device": ["What it is", "Known behaviour", "If it misbehaves"],
    "runbook": ["When to use this", "Steps", "Verification"],
    "register": ["Policy"],
    "governance": ["Rules"],
    "order": ["Read in this order"],
}


def scaffold(kind, key, slug, title, bind, owner, today_iso):
    """A new record, pre-filled from the live object.

    The thing that stops documentation getting written is the blank page, so
    there is never a blank page: the front matter and the bindings are
    already correct, and the headings the template expects are already there
    with nothing under them. An empty section renders as a visible gap, which
    is a question somebody can answer.
    """
    shelf = derive_shelf(kind, slug if kind == "guest" else (key or slug))
    fm = [f'title    = {_toml_str(title)}',
          f'kind     = {_toml_str(kind)}',
          f'shelf    = {_toml_str(shelf)}',
          f'owner    = {_toml_str(owner)}',
          f'verified = {_toml_str(today_iso)}',
          f'review   = {_toml_str(DEFAULT_REVIEW)}']
    if bind:
        fm.append("")
        fm.append("[bind]")
        for k, v in bind.items():
            if isinstance(v, bool):
                fm.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, int):
                fm.append(f"{k} = {v}")
            elif isinstance(v, list | tuple):
                items = ", ".join(_toml_str(x) for x in v)
                fm.append(f"{k} = [{items}]")
            else:
                fm.append(f"{k} = {_toml_str(v)}")
    body = "\n\n".join(f"## {h}\n" for h in SCAFFOLD.get(kind, ["What it is"]))
    return shelf, compose("\n".join(fm), body + "\n")
