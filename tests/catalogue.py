#!/usr/bin/env python3
"""
The catalogue — records on disk, matched against what the portal knows.

The interesting assertions are the ones about *gaps*: an object with no
record, a record binding something that no longer exists, a runbook nobody
has verified. Those are the whole reason this is built inside the monitoring
rather than beside it.

Run it from the repository root:

    python3 tests/catalogue.py
"""

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

try:
    import tomllib                                               # noqa: F401
except ModuleNotFoundError:                                      # Python 3.10
    import tomli
    sys.modules["tomllib"] = tomli      # the parser that became tomllib, so
    tomllib = tomli                     # the path under test is the real one

import citadel                                                   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{('  — ' + str(extra)) if extra and not cond else ''}")


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class FakeStore:
    """The half of the real store Citadel reads: arrays, printers, checks."""

    def __init__(self, printers=(), arrays=(), checks=()):
        self._p, self._a = list(printers), list(arrays)
        self._c = {c["name"]: c for c in checks}

    def printers(self):
        return self._p

    def arrays(self):
        return self._a

    def get(self, name):
        return self._c.get(name)


class FakePortal:
    """The other half: guests, backups, host metrics."""

    def __init__(self, guests=(), backups=(), hosts=()):
        self._g, self._b, self._h = list(guests), list(backups), list(hosts)

    def guests(self):
        return self._g

    def backups(self):
        return self._b

    def host_latest(self):
        return self._h


TMP = tempfile.mkdtemp(prefix="citadel-test-")
try:
    write(TMP, "host/pepper.md", f'''+++
title    = "pepper"
kind     = "host"
shelf    = "HST.PEP"
owner    = "siddhant"
verified = "{ago(10)}"
review   = "6 months"

[bind]
host = "pepper"
+++

## What it is

The newest hypervisor. Restore order is in [[RUN.PEP]], creds in [[REG.NOPE]].
''')
    write(TMP, "host/proxmox.md", f'''+++
title = "proxmox"
owner = "siddhant"
verified = "{ago(400)}"
review = "6 months"
[bind]
host = "proxmox"
+++

## What it is

Carries the business.
''')
    write(TMP, "guest/1100-cfp.md", f'''+++
title = "CFP"
owner = "siddhant"
verified = "{ago(5)}"
[bind]
guest = 1100
+++
Runs WinMan.
''')
    write(TMP, "guest/1203-old.md", f'''+++
title = "old relay"
owner = "siddhant"
verified = "{ago(5)}"
[bind]
guest = 1203
+++
Replaced.
''')
    write(TMP, "runbook/restore-pepper.md", f'''+++
title = "Restore pepper"
shelf = "RUN.PEP"
owner = "siddhant"
verified = "{ago(500)}"
review = "6 months"
+++
Steps.
''')
    write(TMP, "runbook/fresh.md", f'''+++
title = "Fresh runbook"
shelf = "RUN.FRE"
owner = "maria"
verified = "{ago(3)}"
review = "6 months"
+++
Steps.
''')
    write(TMP, "governance/roles.md", '''+++
title = "Roles"
shelf = "GOV.ROLE"
owner = "nobody-at-all"
verified = "2099-01-01"
review = "12 months"
+++
Who is who.
''')
    write(TMP, "host/broken.md", '''+++
title = "broken
+++
Bad TOML above.
''')
    write(TMP, "host/noowner.md", f'''+++
title = "noowner"
shelf = "HST.NOO"
verified = "{ago(1)}"
[bind]
host = "nosuchhost"
+++
Body.
''')

    cat = citadel.Catalogue(TMP).scan()

    print("\nscanning")
    check("every record is found", len(cat.records) == 9, len(cat.records))
    check("shelf mark from front matter wins", cat.exists("HST.PEP"))
    check("shelf mark is derived when absent", cat.exists("HST.PRO"))
    check("a guest derives its mark from the vmid", cat.exists("GST.1100"))
    check("kind comes from the directory",
          cat.get("GST.1100").kind == "guest")
    check("lookup is case-insensitive", cat.get("hst.pep") is not None)
    check("slug routing", cat.by_slug("hst-pep").shelf == "HST.PEP")
    check("of_kind is sorted by title",
          [r.shelf for r in cat.of_kind("runbook")] == ["RUN.FRE", "RUN.PEP"])
    check("a content hash is recorded", len(cat.get("HST.PEP").blob) == 7)

    print("\nbad input does not take the catalogue down")
    bad = [r for r in cat.records.values() if r.path == "host/broken.md"][0]
    check("broken TOML is a problem, not an exception", bad.problems)
    check("the other eight records still loaded", len(cat.records) == 9)
    check("a record with no front matter is flagged",
          "no front matter" in " ".join(
              citadel.parse("just prose", "host", "x").problems))

    print("\nrendering")
    html, links = cat.html(cat.get("HST.PEP"))
    check("record links resolve against the catalogue",
          ('RUN.PEP', True) in links)
    check("a missing record link is reported unresolved",
          ('REG.NOPE', False) in links)
    check("render is memoised on the content hash",
          cat.html(cat.get("HST.PEP")) is cat.html(cat.get("HST.PEP")))
    check("search text drops the front matter",
          "siddhant" not in cat.search_text(cat.get("HST.PEP")))

    print("\nreview periods")
    check("'6 months' parses", citadel.review_months("6 months") == 6)
    check("'1 year' is twelve", citadel.review_months("1 year") == 12)
    check("nonsense falls back to six, not to never",
          citadel.review_months("whenever") == 6)
    check("a fresh record is not overdue", cat.get("RUN.FRE").overdue is None)
    check("an old record is overdue", cat.get("RUN.PEP").overdue > 0)
    check("no verified date counts as overdue",
          citadel.parse("+++\ntitle='x'\n+++\nb", "host", "x").overdue > 9000)

    print("\nmatching against the live estate")
    store = FakeStore(
        printers=[{"host": "10.10.1.177", "name": "Konica"}],
        arrays=[{"host": "pepper", "name": "md0", "state": "clean",
                 "degraded": 0},
                {"host": "proxmox", "name": "Array A", "state": "Interim",
                 "degraded": 1}],
        checks=[{"name": "Pepper web UI", "status": "up"}])
    portal = FakePortal(
        guests=[{"vmid": 1100, "name": "CFP", "host": "cinnamon",
                 "status": "running", "kind": "qemu",
                 "addresses": "10.10.1.90"},
                {"vmid": 9507, "name": "scratch", "host": "pepper",
                 "status": "running", "kind": "lxc", "addresses": ""}],
        backups=[{"vmid": "1100", "host": "cinnamon", "last_ts": 1762300000,
                  "state": "ok"}],
        hosts=[{"host": "pepper", "mem_used": 80000000000,
                "mem_total": 134000000000}])
    inv = {"hosts": [{"name": "pepper", "address": "10.10.1.70"},
                     {"name": "proxmox", "address": "10.10.1.10"},
                     {"name": "networkserver", "address": "10.10.1.40"}],
           "apps": [{"name": "WinMan"}]}
    live = citadel.live_objects(store, portal, inv)
    check("live objects come from both sources", len(live) == 7, len(live))
    cov = citadel.coverage(cat, live)
    check("coverage counts matched objects", cov == {"have": 3, "total": 7}, cov)

    print("\nfindings")
    f = citadel.findings(cat, live, users=["siddhant", "maria"])
    kinds = {}
    for x in f:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1

    def of(kind):
        return [x for x in f if x["kind"] == kind]

    check("an undocumented host is found",
          any("networkserver" in x["what"] for x in of("undocumented")))
    check("an undocumented guest is found",
          any("9507" in x["what"] for x in of("undocumented")))
    check("an undocumented app is found",
          any("WinMan" in x["what"] for x in of("undocumented")))
    check("undocumented findings carry a create action",
          all("create" in x for x in of("undocumented")))
    check("an orphaned record is found",
          any(x["shelf"] == "GST.1203" for x in of("orphaned")))
    check("a record bound to a missing host is orphaned",
          any(x["shelf"] == "HST.NOO" for x in of("orphaned")))
    check("a broken cross-reference is found",
          any("REG.NOPE" in x["what"] for x in of("broken-link")))
    check("a resolved cross-reference is not",
          not any("RUN.PEP]]" in x["what"] for x in of("broken-link")))
    check("an unverified runbook is found",
          any(x["shelf"] == "RUN.PEP" for x in of("unverified-runbook")))
    check("a fresh runbook is not",
          not any(x["shelf"] == "RUN.FRE" for x in of("unverified-runbook")))
    check("a stale non-runbook is quiet, not paging",
          all(x["severity"] == "quiet" for x in of("stale")))
    check("an owner with no account is flagged",
          any("nobody-at-all" in x["what"] for x in of("unowned")))
    check("a record with no owner is flagged",
          any(x["shelf"] == "HST.NOO" for x in of("unowned")))
    check("malformed front matter is a finding", of("malformed"))

    print("\nseverity — only one thing pages")
    paging = [x for x in f if x["severity"] == "page"]
    check("exactly one kind pages", {x["kind"] for x in paging}
          == {"unverified-runbook"}, {x["kind"] for x in paging})
    check("findings are sorted worst first", f[0]["severity"] == "page")

    print("\naccepted risk silences a finding, with a reason")
    acc = [{"check": "RUN.PEP", "reason": "superseded",
            "decided": "2026-10-22", "review": "2027-01-31"}]
    f2 = citadel.findings(cat, live, accepted=acc, users=["siddhant", "maria"])
    check("the accepted record no longer pages",
          not any(x["shelf"] == "RUN.PEP" for x in f2))
    check("everything else is untouched", len(f2) < len(f))

    print("\nfixing the cause removes the finding")
    rec = cat.get("HST.PEP")
    rec.body = rec.body.replace("[[REG.NOPE]]", "[[GOV.ROLE]]")
    rec.blob = "changed"
    f3 = citadel.findings(cat, live, users=["siddhant", "maria"])
    check("the broken link is gone",
          not any("REG.NOPE" in x["what"] for x in f3
                  if x["kind"] == "broken-link"))

    print("\na live source that cannot be read must not flatter the numbers")

    class BrokenStore:
        """A portal whose guests table will not read."""

        def guests(self):
            raise RuntimeError("no such table: guests")

        def backups(self):
            return []

        def host_latest(self):
            return []

    errs = []
    live_bad = citadel.live_objects(store, BrokenStore(), inv, errors=errs)
    check("the failure is reported, not swallowed", errs, errs)
    check("the guests it could not read are absent",
          not any(o["kind"] == "guest" for o in live_bad))
    cov_bad = citadel.coverage(cat, live_bad)
    check("coverage would otherwise read better than the truth",
          cov_bad["total"] < cov["total"], (cov_bad, cov))
    f_bad = citadel.findings(cat, live_bad, users=["siddhant"], live_errors=errs)
    check("so an unreadable source pages",
          any(x["kind"] == "live-source" and x["severity"] == "page"
              for x in f_bad))
    check("and says the coverage is understated",
          any("understated" in x["what"] for x in f_bad))
    check("not passing the errors through loses the warning",
          not any(x["kind"] == "live-source"
                  for x in citadel.findings(cat, live_bad)))

    print("\nlive panels, resolved from [bind]")
    perr = []
    pep = citadel.panels(cat.get("HST.PEP"), store, portal, errors=perr)
    labels = [p["label"] for p in pep]
    check("memory comes from hostmetrics", "Memory" in labels)
    check("memory is a percentage of the real total",
          next(p for p in pep if p["label"] == "Memory")["value"] == "60%", pep)
    check("arrays are counted for this host only",
          next(p for p in pep if p["label"] == "Arrays")["value"] == "1 ok")
    check("no binding, no panel - guests was not bound", "Guests" not in labels)
    check("nothing errored", not perr, perr)

    prx = citadel.panels(cat.get("HST.PRO"), store, portal)
    check("a degraded array reports as degraded",
          next(p for p in prx if p["label"] == "Arrays")["value"] == "1 degraded")

    gst = citadel.panels(cat.get("GST.1100"), store, portal)
    glabels = [p["label"] for p in gst]
    check("a guest resolves its vmid", "VMID" in glabels)
    check("a guest reports its state",
          next(p for p in gst if p["label"] == "State")["value"] == "running")
    check("a guest shows its address", "Address" in glabels)
    check("a guest joins the backups table", "Backup" in glabels)

    check("a binding to a host that does not exist yields no panels",
          citadel.panels(cat.get("HST.NOO"), store, portal) == [])
    check("a record with no bindings yields no panels",
          citadel.panels(cat.get("GOV.ROLE"), store, portal) == [])

    perr2 = []
    citadel.panels(cat.get("GST.1100"), store, BrokenStore(), errors=perr2)
    check("a panel source that fails is reported, not silently empty", perr2)

    print("\ncarried guests, for the live diagram")
    carried = citadel.carried_guests(cat.get("HST.PEP"), portal)
    check("only this host's guests", [g["vmid"] for g in carried] == [9507])
    check("a record with no host binding carries nothing",
          citadel.carried_guests(cat.get("GOV.ROLE"), portal) == [])

    print("\nan empty or missing catalogue is not a crash")
    empty = citadel.Catalogue(os.path.join(TMP, "nope")).scan()
    check("a missing root is an error, not an exception", empty.errors)
    check("findings still run", citadel.findings(empty, live))
    check("coverage on an empty catalogue",
          citadel.coverage(empty, live) == {"have": 0, "total": 7})

    print("\nthe service: scan, cache, and the degraded read")
    import store as store_mod
    dbdir = tempfile.mkdtemp(prefix="citadel-db-")
    real = store_mod.Store(os.path.join(dbdir, "t.db"))
    svc = citadel.Service(TMP, real, portal, inv, interval=0)
    svc.portal = portal

    check("a forced scan reads the tree", svc.scan(force=True))
    check("records reached the database", len(real.records()) == 9,
          len(real.records()))
    check("rendered html was cached",
          "<h3>" in (real.get_record("HST.PEP")["html"] or ""))
    check("the cross-reference graph was stored",
          any(r["to_shelf"] == "REG.NOPE" for r in real.broken_links()))
    check("search finds a word from the body",
          any(r["shelf"] == "HST.PEP"
              for r in real.search_records("hypervisor")))

    summ = svc.summary()
    check("summary counts records", summ["records"] == 9)
    # 3 hosts + 1 app from inventory + 2 guests from the fake portal. No
    # printers: this service is wired to a real, empty Store, so the device
    # count is genuinely zero rather than the fake's one.
    check("summary carries coverage", summ["coverage"]["total"] == 6,
          summ["coverage"])
    check("summary counts findings", summ["findings"] > 0)
    check("exactly one thing pages", summ["paging"] == 1, summ["paging"])
    check("summary reports whether FTS is available", "fts" in summ)

    idx = svc.index("host")
    check("the index lists every live host", len(idx) >= 3, len(idx))
    check("an undocumented host offers a create action",
          any(r["create"] for r in idx))
    check("a documented host carries its record",
          any(r["record"] and r["record"]["shelf"] == "HST.PEP" for r in idx))
    check("a record with no live object still appears",
          any(r["record"] and r["record"]["shelf"] == "HST.NOO" for r in idx))

    rec = svc.record("hst-pep")
    check("record() resolves by slug", rec["shelf"] == "HST.PEP")
    check("record() renders html", "<h3>" in rec["html"])
    check("record() resolves live panels", rec["panels"])
    check("record() reports its links",
          any(x["shelf"] == "REG.NOPE" and not x["resolved"]
              for x in rec["links"]))
    check("an unknown slug is None", svc.record("nope-nope") is None)

    check("search goes through the service",
          any(r["shelf"] == "RUN.PEP" for r in svc.search("restore")))

    print("\na tree that disappears degrades, it does not empty")
    gone = os.path.join(TMP, "..", "citadel-gone-" + str(os.getpid()))
    svc2 = citadel.Service(gone, real, portal, inv, interval=0)
    before = len(real.records())
    check("scanning a missing tree refuses to write", not svc2.scan(force=True))
    check("the cached records are still there", len(real.records()) == before)
    check("and it says it is degraded", svc2.degraded)
    check("the cache age is known", real.record_age() is not None)

    print("\nrescan is rate limited")
    svc3 = citadel.Service(TMP, real, portal, inv, interval=3600)
    svc3.scan(force=True)
    check("a second scan inside the interval is skipped", not svc3.scan())
    check("force overrides the interval", svc3.scan(force=True))

    shutil.rmtree(dbdir, ignore_errors=True)

    print("\nhealth as alertable conditions")
    roll = svc.rollup()
    check("every kind has a row, including the clear ones",
          set(roll) == set(citadel.CHECKS), set(citadel.CHECKS) - set(roll))
    check("a row carries name, severity, count and detail",
          all(len(v) == 4 for v in roll.values()))
    check("the unverified runbook is counted",
          roll["unverified-runbook"][2] >= 1, roll["unverified-runbook"])
    check("and it pages", roll["unverified-runbook"][1] == "page")
    check("undocumented objects notify, they do not page",
          roll["undocumented"][1] == "notify")
    check("stale records are quiet", roll["stale"][1] == "quiet")
    check("exactly two kinds page",
          sorted(k for k, v in citadel.CHECKS.items() if v[1] == "page")
          == ["live-source", "unverified-runbook"])
    check("a kind with nothing wrong still reports, as none",
          roll["live-source"] == (citadel.CHECKS["live-source"][0], "page", 0,
                                  "none"), roll["live-source"])
    check("the detail names the records, not just a count",
          "RUN.PEP" in roll["unverified-runbook"][3],
          roll["unverified-runbook"][3])

    print("\na category that clears must be able to recover")
    before = svc.rollup()["broken-link"][2]
    check("a broken link is counted now", before >= 1, before)
    # the service has its own Catalogue; mutating the standalone one proves
    # nothing about what the sweep will see
    fixed = svc.cat.get("HST.PEP")
    fixed.body = fixed.body.replace("[[REG.NOPE]]", "[[GOV.ROLE]]")
    fixed.blob = "fixed-blob"
    after = svc.rollup()["broken-link"]
    check("fixing the cause clears the row", after[2] == 0, after)
    check("and the row says none, so it can transition up",
          after[3] == "none", after)

    print("\nusers come from the store, not from the caller")
    check("no accounts means nobody is checked against",
          svc.users() == [], svc.users())
    real.x("INSERT INTO users(username,pwhash,display,role,created_ts) "
           "VALUES(?,?,?,?,?)", ("maria", "x", "Maria", "admin", 0))
    check("an account is seen", svc.users() == ["maria"], svc.users())
    unowned = [f for f in svc.findings() if f["kind"] == "unowned"]
    check("an owner with no account is now a finding",
          any("nobody-at-all" in f["what"] for f in unowned), unowned)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
