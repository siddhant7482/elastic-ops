#!/usr/bin/env python3
"""
The git layer — writing, and refusing to write.

The assertions that matter are the refusals: a path that escapes the tree, a
save against a file that moved underneath the editor, a commit with no
summary. Everything else is git doing its job.

Skips itself if git is not installed, because a host without git must still
be able to run the rest of the suite.

Run it from the repository root:

    python3 tests/repository.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

import repo as repomod                                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{('  — ' + str(extra)) if extra and not cond else ''}")


try:
    subprocess.run(["git", "--version"], capture_output=True, timeout=10,
                   check=True)
except (OSError, subprocess.SubprocessError):
    print("git is not installed — skipping (this is not a failure)")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="citadel-repo-")
try:
    print("\npath safety — the only place a path becomes a filesystem path")
    for bad in ["../escape.md", "/etc/passwd", "host/../../x.md", "", None,
                "host/pepper.txt", "host/pepper", ".hidden.md",
                "host/../pepper.md", "C:/windows/x.md", "a\\../b.md"]:
        check(f"refuses {bad!r}", repomod.safe_rel(bad) is None)
    for good in ["host/pepper.md", "runbook/restore-cfp.md", "a.md",
                 "guest/1100-cfp.md"]:
        check(f"allows {good!r}", repomod.safe_rel(good) == good)
    check("normalises a leading slash",
          repomod.safe_rel("/host/pepper.md") == "host/pepper.md")

    print("\nbefore there is a repository")
    r = repomod.Repo(os.path.join(TMP, "nothing"))
    ok, why = r.available()
    check("an absent tree is unavailable, with a reason", not ok and why)
    st = r.status()
    check("status says so rather than raising", st["ok"] is False and st["why"])
    check("blob on a missing file is None", r.blob("host/x.md") is None)
    check("log on a missing repo is empty", r.log() == [])

    print("\ninit and first write")
    root = os.path.join(TMP, "docs")
    r = repomod.Repo(root)
    check("init creates it", r.init())
    check("init is idempotent", r.init() is False)
    ok, why = r.available()
    check("now available", ok, why)

    sha1 = r.write("host/pepper.md", "## What it is\n\nThe newest one.\n",
                   "maria", "maria@elasticgroup.co.uk", "Create pepper")
    check("a write returns a commit sha", bool(sha1) and len(sha1) == 40, sha1)
    check("the file is on disk",
          os.path.isfile(os.path.join(root, "host", "pepper.md")))
    check("the file ends with a newline",
          open(os.path.join(root, "host", "pepper.md"),
               encoding="utf-8").read().endswith("\n"))

    log = r.log()
    check("the commit is in the log", len(log) == 1, log)
    check("it carries the author's name", log[0]["author"] == "maria")
    check("it carries the author's email",
          log[0]["email"] == "maria@elasticgroup.co.uk")
    check("it carries the summary", log[0]["summary"] == "Create pepper")
    check("a short sha is offered", len(log[0]["short"]) == 7)

    print("\nconflict detection — the blob is the version identity")
    b1 = r.blob("host/pepper.md")
    check("the blob hashes the file on disk", b1 and len(b1) == 40)
    r.write("host/pepper.md", "## What it is\n\nEdited elsewhere.\n",
            "siddhant", "sid@elasticgroup.co.uk", "Correct it")
    b2 = r.blob("host/pepper.md")
    check("the blob moved when the file did", b1 != b2)

    clashed = None
    try:
        r.write("host/pepper.md", "## Mine\n", "maria", "m@x", "Mine",
                expected_blob=b1)
    except repomod.Conflict as e:
        clashed = e
    check("saving against a stale blob raises Conflict", clashed is not None)
    check("the conflict names what it expected", clashed.expected == b1)
    check("and what it found", clashed.actual == b2)
    check("and carries their text", "Edited elsewhere" in clashed.theirs)
    check("nothing was written",
          "Mine" not in open(os.path.join(root, "host", "pepper.md"),
                             encoding="utf-8").read())
    check("and nothing was committed", len(r.log()) == 2, len(r.log()))

    check("saving against the current blob succeeds",
          bool(r.write("host/pepper.md", "## Mine\n\nkept.\n", "maria", "m@x",
                       "Keep mine", expected_blob=b2)))
    check("a write with no expectation still works",
          bool(r.write("host/pepper.md", "## Later\n", "maria", "m@x", "Later")))

    print("\nrefusals")
    for bad in ["../escape.md", "/etc/passwd", "host/evil.sh"]:
        try:
            r.write(bad, "x", "m", "m@x", "nope")
            caught = False
        except repomod.RepoError:
            caught = True
        check(f"refuses to write {bad!r}", caught)
    try:
        r.write("host/pepper.md", "x", "m", "m@x", "   ")
        caught = False
    except repomod.RepoError:
        caught = True
    check("refuses a commit with no summary", caught)
    check("writing identical bytes is not an error and not a commit",
          r.write("host/pepper.md", "## Later\n", "m", "m@x", "again") is None)

    print("\nan author cannot forge the commit header")
    sha = r.write("host/evil.md", "x\n", "bad\nname <a@b>", "e@x", "test")
    entry = r.log("host/evil.md")[0]
    check("newlines are stripped from the name", "\n" not in entry["author"])
    check("angle brackets are stripped", "<" not in entry["author"],
          entry["author"])
    check("the commit still landed", bool(sha))

    print("\nhistory and restore")
    hist = r.log("host/pepper.md")
    check("history is per file", len(hist) == 4, len(hist))
    check("newest first", hist[0]["ts"] >= hist[-1]["ts"])
    old = r.show(hist[-1]["sha"], "host/pepper.md")
    check("an old version can be read back", "The newest one" in (old or ""))
    check("show refuses a bad sha", r.show("not-a-sha", "host/pepper.md") is None)
    check("show refuses a bad path", r.show(hist[0]["sha"], "../x.md") is None)
    check("a diff is produced", "@@" in r.diff(hist[0]["sha"]))

    restored = r.write("host/pepper.md", old, "maria", "m@x",
                       "Restore the October wording")
    check("restoring writes a new commit", bool(restored))
    check("history is longer, never rewritten", len(r.log("host/pepper.md")) == 5)

    print("\nremoval")
    r.write("host/gone.md", "bye\n", "m", "m@x", "add")
    r.remove("host/gone.md", "m", "m@x", "Archive it")
    check("the file is gone",
          not os.path.isfile(os.path.join(root, "host", "gone.md")))
    check("but its history is not", len(r.log("host/gone.md")) == 2)

    print("\nstatus and the remote")
    st = r.status()
    check("status reports the branch", st["branch"] in ("main", "master"), st)
    check("status reports a clean tree", st["dirty"] == 0, st)
    check("no remote is reported, not guessed", st["remote"] is False)
    check("ahead is None with no upstream", st["ahead"] is None)
    ok, why = r.push()
    check("push without a remote fails politely", not ok and "remote" in why)
    ok, why = r.pull()
    check("pull without a remote fails politely", not ok and "remote" in why)

    print("\npushing to a real remote")
    bare = os.path.join(TMP, "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare],
                   check=True, capture_output=True)
    r._run(["remote", "add", "origin", bare])
    r._run(["push", "-q", "-u", "origin", "HEAD"], timeout=30, check=False)
    check("a remote is now seen", r.status()["remote"] is True)
    check("nothing is ahead right after a push", r.ahead() == 0, r.ahead())
    r.write("host/after.md", "later\n", "m", "m@x", "After the push")
    check("a new commit shows as ahead", r.ahead() == 1, r.ahead())
    ok, why = r.push()
    check("push succeeds", ok, why)
    check("and nothing is ahead again", r.ahead() == 0)
    ok, why = r.pull()
    check("pull succeeds and is a no-op", ok, why)

    print("\na diverged history is refused, not merged")
    other = os.path.join(TMP, "other")
    subprocess.run(["git", "clone", "-q", bare, other], check=True,
                   capture_output=True)
    r2 = repomod.Repo(other)
    r2.write("host/theirs.md", "theirs\n", "sid", "s@x", "Theirs")
    ok, _ = r2.push()
    check("someone else pushes", ok)
    r.write("host/mine.md", "mine\n", "maria", "m@x", "Mine")
    ok, why = r.pull()
    check("our fast-forward is refused", not ok, why)
    check("and it says why", "ff" in why.lower() or "diverg" in why.lower()
          or "reject" in why.lower(), why)
    check("our commit is still here",
          os.path.isfile(os.path.join(root, "host", "mine.md")))
    check("and still ahead, stacking locally", (r.ahead() or 0) >= 1, r.ahead())

finally:
    # git on Windows leaves read-only objects behind
    def _force(fn, path, _):
        try:
            os.chmod(path, 0o700)
            fn(path)
        except OSError:
            pass
    shutil.rmtree(TMP, onerror=_force)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
