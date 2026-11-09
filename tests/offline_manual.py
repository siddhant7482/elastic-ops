#!/usr/bin/env python3
"""
The offline manual — the file you open when the portal is what is broken.

Every assertion here is about independence. A web font, a script tag or an
absolute URL back to 10.10.1.70 would each turn this into a blank page on a
train, which is precisely the situation it exists for.

Run it from the repository root:

    python3 tests/offline_manual.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

try:
    import tomllib                                               # noqa: F401
except ModuleNotFoundError:                                      # Python 3.10
    import tomli
    sys.modules["tomllib"] = tomli
    tomllib = tomli

import citadel                                                   # noqa: E402
import manual                                                    # noqa: E402
import repo as repomod                                           # noqa: E402
import store as store_mod                                        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{('  — ' + str(extra)) if extra and not cond else ''}")


class FakePortal:
    def guests(self):
        return [{"vmid": 1100, "name": "CFP", "host": "pepper",
                 "status": "running", "kind": "qemu", "addresses": ""}]

    def backups(self):
        return []

    def host_latest(self):
        return [{"host": "pepper", "mem_used": 80_000_000_000,
                 "mem_total": 134_000_000_000}]


DOCS = {
    "host/pepper.md":
        '+++\ntitle = "pepper"\nshelf = "HST.PEP"\nowner = "sid"\n'
        'verified = "2026-11-05"\nreview = "6 months"\n'
        '[bind]\nhost = "pepper"\nguests = true\n+++\n\n'
        "## What it is\n\nThe newest one. See [[RUN.PEP]] and [[NO.PE]].\n\n"
        "> [!danger] Restore proxmox first.\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    "runbook/restore-pepper.md":
        '+++\ntitle = "Restore pepper"\nshelf = "RUN.PEP"\nowner = "sid"\n'
        'verified = "2020-01-01"\nreview = "6 months"\n+++\n\n'
        "## Steps\n\n1. One\n2. Two\n\n```sh\nls -la\n```\n",
    "governance/roles.md":
        '+++\ntitle = "Roles"\nshelf = "GOV.ROLE"\nowner = "sid"\n'
        'verified = "2026-11-01"\n+++\n\nWho is who. <script>alert(1)</script>\n',
}

TMP = tempfile.mkdtemp(prefix="citadel-manual-")
try:
    docs = os.path.join(TMP, "docs")
    for rel, text in DOCS.items():
        p = os.path.join(docs, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    st = store_mod.Store(os.path.join(TMP, "t.db"))
    svc = citadel.Service(docs, st, FakePortal(),
                          {"hosts": [{"name": "pepper"}]}, interval=0)
    svc.scan(force=True)
    html = manual.build(svc, generated_by="the test")

    print("\nit needs nothing")
    check("no web font", "fonts.googleapis" not in html
          and "fonts.gstatic" not in html)
    check("no script tag", "<script" not in html.lower())
    check("no absolute url anywhere", "http://" not in html
          and "https://" not in html)
    check("no link back to the portal", "/citadel/" not in html)
    check("no external stylesheet", "<link" not in html.lower())
    check("the css is inline", "<style>" in html)

    print("\nit works on a phone and on paper")
    check("viewport meta", 'name="viewport"' in html)
    check("print styles", "@media print" in html)
    check("tables can scroll inside their own frame", 'class="tw"' in html)

    print("\nevery record is in it")
    check("the host", 'id="hst-pep"' in html)
    check("the runbook", 'id="run-pep"' in html)
    check("the governance note", 'id="gov-role"' in html)
    check("the contents links to each", html.count('href="#') >= 3)
    check("grouped by kind, not alphabetical",
          html.index("Hosts") < html.index("Runbooks") < html.index("Governance"))

    print("\nrecord links resolve in-page, not to a dead host")
    check("a resolved link is an anchor", 'href="#run-pep"' in html)
    check("an unresolved one is still marked", 'class="sm q"' in html)
    check("NO.PE is shown as missing", "NO.PE" in html)

    print("\nthe markdown subset renders, and still refuses")
    check("callout", 'class="callout danger"' in html)
    check("ordered list", "<ol>" in html)
    check("fenced code", "<pre>" in html)
    check("table", "<table>" in html)
    check("raw html is escaped here too",
          "&lt;script&gt;" in html and "alert(1)</script>" not in html)

    print("\nit is honest about being a snapshot")
    check("it says so at the top", "snapshot, not a live view" in html)
    check("it is stamped", "Generated" in html)
    check("measured figures are labelled as at that time",
          "only as at the time above" in html)
    check("live panels are present at all", 'class="panels"' in html)
    check("outstanding findings are carried", "Outstanding when this was built"
          in html)
    check("the unverified runbook is among them", "RUN.PEP" in html)

    print("\na degraded read is declared, not hidden")
    svc.degraded = True
    check("it says the tree could not be fully read",
          "built from a cached read" in manual.build(svc))
    svc.degraded = False

    print("\npublishing commits it, or declines to")
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10,
                       check=True)
        have_git = True
    except (OSError, subprocess.SubprocessError):
        have_git = False

    if not have_git:
        print("  --   git is not installed; skipping the commit path")
    else:
        sha, why = svc.publish_manual("tester")
        check("without a repository it declines, with a reason",
              sha is None and why, (sha, why))

        r = repomod.Repo(docs)
        r.init()
        r._run(["add", "-A"])
        r._run(["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q",
                "-m", "seed"], check=False)
        svc.repo = r

        sha, rel = svc.publish_manual("tester")
        check("it commits", bool(sha), (sha, rel))
        check("into dist/", rel == "dist/estate-manual.html", rel)
        check("the file is on disk",
              os.path.isfile(os.path.join(docs, "dist", "estate-manual.html")))
        check("the commit is in the log",
              any("offline manual" in c["summary"] for c in r.log()))
        check("it is attributed to the system, not a person",
              r.log()[0]["author"] == "citadel", r.log()[0]["author"])
        check("and recorded in the activity log",
              any(c["via"] == "manual" for c in st.doc_commits()))

        sha2, why2 = svc.publish_manual("tester")
        check("rebuilding with nothing changed commits nothing",
              sha2 is None and why2 == "unchanged", (sha2, why2))

        svc.cat.get("HST.PEP").body += "\n## Added\n\nmore.\n"
        svc.cat.get("HST.PEP").blob = "moved"
        svc.scan = lambda *a, **k: False          # keep the mutation
        sha3, rel3 = svc.publish_manual("tester")
        check("a real change does commit", bool(sha3), (sha3, rel3))
        check("and the new text is in the file",
              "Added" in open(os.path.join(docs, "dist",
                                           "estate-manual.html"),
                              encoding="utf-8").read())

finally:
    def _force(fn, path, _):
        try:
            os.chmod(path, 0o700)
            fn(path)
        except OSError:
            pass
    shutil.rmtree(TMP, onerror=_force)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
