"""
The document tree as a git repository.

Phase 1 reads a directory. This is what lets the portal write to it, so Maria
can open a record, type, and have a real commit come out the other end with
her name on it — no Node, no container rebuild, no git on her machine.

Four things this module is careful about, each because the obvious version is
wrong:

  * **One lock, and it is not the database lock.** Two concurrent `git add`
    runs in the same checkout corrupt the index, and the failure is ugly and
    intermittent. Every invocation here serialises. It must never take the
    store's lock, or a slow push would block the check loop.

  * **Argument lists, never a shell string.** A document title will
    eventually contain a quote. `probes.py` already runs `ping` this way.

  * **`GIT_TERMINAL_PROMPT=0`, always.** Without it a push with a stale
    credential does not fail — it blocks forever waiting for a username that
    nobody is there to type, and takes a worker with it.

  * **A timeout on everything.** `git push` talks to the network.

The version identity is `git hash-object` of the file **on disk**, not the
blob at HEAD. That catches someone editing the file directly on the host as
well as someone committing, which is the whole point of detecting a clash.

    r = Repo("/var/lib/elastic-ops/estate-docs")
    ok, why = r.available()
    sha = r.blob("host/pepper.md")
    r.write("host/pepper.md", text, "maria", "maria@...", "Add the note", sha)
"""

import os
import re
import subprocess
import threading

GIT = "git"
SHORT = 12            # seconds: local operations
LONG = 45             # seconds: anything that talks to a remote

_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.md$")
_BAD_NAME = re.compile(r"[<>\n\r]")


class RepoError(Exception):
    """A git operation failed in a way the caller must show someone."""


class Conflict(Exception):
    """The file moved under us. Carries both sides so the page can show them."""

    def __init__(self, path, expected, actual, theirs):
        super().__init__(f"{path} changed: expected {expected}, found {actual}")
        self.path = path
        self.expected = expected
        self.actual = actual
        self.theirs = theirs


def safe_rel(path):
    """A path inside the tree, or None.

    Rejects traversal, absolute paths, and anything that is not markdown.
    This is the only place a caller-supplied path becomes a filesystem path,
    so it is the only place that has to be right.
    """
    p = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not p or ".." in p.split("/") or not _SAFE_PATH.match(p):
        return None
    return p


class Repo:
    def __init__(self, root, home=None, timeout=SHORT):
        self.root = os.path.abspath(root)
        self.lock = threading.RLock()
        self.timeout = timeout
        # ProtectHome=true means the service has no home directory to read
        # config from. Give git somewhere, or it fails in a way that reads
        # like a permissions bug.
        self.home = home or self.root
        self._why = None

    # -- running git -----------------------------------------------------
    def _env(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "GIT_TERMINAL_PROMPT": "0",     # fail, do not hang, on a prompt
            "GIT_ASKPASS": "",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",                  # parseable output, always
        })
        return env

    def _run(self, args, timeout=None, check=True, stdin=None):
        with self.lock:
            try:
                p = subprocess.run(
                    [GIT, "-C", self.root, *args],
                    capture_output=True, text=True, env=self._env(),
                    input=stdin, timeout=timeout or self.timeout)
            except FileNotFoundError as e:
                raise RepoError("git is not installed on this host") from e
            except subprocess.TimeoutExpired as e:
                raise RepoError(f"git {args[0]} timed out") from e
        if check and p.returncode != 0:
            raise RepoError((p.stderr or p.stdout or "git failed").strip())
        return p.returncode, p.stdout, p.stderr

    # -- state -----------------------------------------------------------
    def available(self):
        """(usable, reason). Phase 1 stays read-only when this is False, so a
        host with no git is a catalogue you can read but not edit — not a
        catalogue that is down."""
        if not os.path.isdir(self.root):
            return False, f"no document tree at {self.root}"
        try:
            code, out, _ = self._run(["rev-parse", "--git-dir"], check=False)
        except RepoError as e:
            return False, str(e)
        if code != 0:
            return False, f"{self.root} is not a git repository"
        return True, ""

    def branch(self):
        _, out, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return out.strip() or "HEAD"

    def has_remote(self):
        _, out, _ = self._run(["remote"], check=False)
        return bool(out.strip())

    def ahead(self):
        """Commits made here that the remote has not got. The difference
        between written down and written down somewhere that survives this
        host."""
        code, out, _ = self._run(
            ["rev-list", "--count", "@{u}..HEAD"], check=False)
        if code != 0:
            return None                 # no upstream configured
        try:
            return int(out.strip())
        except ValueError:
            return None

    def status(self):
        ok, why = self.available()
        if not ok:
            return {"ok": False, "why": why, "branch": None, "ahead": None,
                    "remote": False, "dirty": None}
        _, out, _ = self._run(["status", "--porcelain"], check=False)
        return {"ok": True, "why": "", "branch": self.branch(),
                "ahead": self.ahead(), "remote": self.has_remote(),
                "dirty": len([x for x in out.splitlines() if x.strip()])}

    # -- reading ---------------------------------------------------------
    def blob(self, rel):
        """The hash of the file as it is on disk right now. None if absent."""
        rel = safe_rel(rel)
        if not rel:
            return None
        full = os.path.join(self.root, rel)
        if not os.path.isfile(full):
            return None
        _, out, _ = self._run(["hash-object", "--", full])
        return out.strip() or None

    def log(self, rel=None, limit=50):
        args = ["log", f"-{int(limit)}", "--no-merges",
                "--pretty=format:%H%x1f%at%x1f%an%x1f%ae%x1f%s"]
        if rel:
            r = safe_rel(rel)
            if not r:
                return []
            args += ["--", r]
        code, out, _ = self._run(args, check=False)
        if code != 0:
            return []
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            sha, ts, name, email, subject = parts
            rows.append({"sha": sha, "short": sha[:7], "ts": int(ts),
                         "author": name, "email": email, "summary": subject})
        return rows

    def show(self, sha, rel):
        """A file as it was at one commit, for the history view and for
        restoring. Restoring writes a new commit; history is never rewritten."""
        r = safe_rel(rel)
        if not r or not re.fullmatch(r"[0-9a-f]{7,40}", str(sha or "")):
            return None
        code, out, _ = self._run(["show", f"{sha}:{r}"], check=False)
        return out if code == 0 else None

    def diff(self, sha, rel=None):
        args = ["show", "--format=", "--unified=3", sha]
        if rel:
            r = safe_rel(rel)
            if not r:
                return ""
            args += ["--", r]
        code, out, _ = self._run(args, check=False)
        return out if code == 0 else ""

    # -- writing ---------------------------------------------------------
    def write(self, rel, text, author, email, summary, expected_blob=None):
        """Write, stage and commit one file as one person.

        `expected_blob` is the hash the editor loaded. If the file on disk no
        longer hashes to it, nothing is written and a Conflict is raised
        carrying both sides. Nothing is ever merged automatically: the person
        decides, and that is a decision, not a default.
        """
        r = safe_rel(rel)
        if not r:
            raise RepoError(f"refusing to write {rel!r}")
        name = _BAD_NAME.sub("", str(author or "unknown"))[:64] or "unknown"
        mail = _BAD_NAME.sub("", str(email or "")).strip()[:120] \
            or f"{name}@elastic-ops.local"
        msg = str(summary or "").strip()[:200]
        if not msg:
            raise RepoError("a commit needs a summary")

        full = os.path.join(self.root, r)
        with self.lock:
            actual = self.blob(r)
            if expected_blob is not None and actual != expected_blob:
                theirs = ""
                if actual:
                    with open(full, encoding="utf-8") as fh:
                        theirs = fh.read()
                raise Conflict(r, expected_blob, actual, theirs)

            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
            os.replace(tmp, full)       # atomic: no half-written record

            self._run(["add", "--", r])
            code, out, err = self._run(
                ["-c", f"user.name={name}", "-c", f"user.email={mail}",
                 "commit", "--no-verify", "-m", msg, "--", r], check=False)
            if code != 0:
                if "nothing to commit" in (out + err).lower():
                    return None         # saved the same bytes; not an error
                raise RepoError((err or out).strip())
            _, sha, _ = self._run(["rev-parse", "HEAD"])
            return sha.strip()

    def remove(self, rel, author, email, summary):
        r = safe_rel(rel)
        if not r:
            raise RepoError(f"refusing to remove {rel!r}")
        name = _BAD_NAME.sub("", str(author or "unknown"))[:64] or "unknown"
        mail = _BAD_NAME.sub("", str(email or "")).strip()[:120] \
            or f"{name}@elastic-ops.local"
        with self.lock:
            code, _, err = self._run(["rm", "--", r], check=False)
            if code != 0:
                raise RepoError(err.strip() or "could not remove")
            self._run(["-c", f"user.name={name}", "-c", f"user.email={mail}",
                       "commit", "--no-verify", "-m",
                       str(summary or f"Remove {r}")[:200]])
            _, sha, _ = self._run(["rev-parse", "HEAD"])
            return sha.strip()

    # -- the remote ------------------------------------------------------
    def push(self):
        """Best effort, and the caller must treat it that way. A save has
        already succeeded by the time this runs; a failure here means the
        work is committed locally and not yet off this host, which is a
        banner, not an error."""
        if not self.has_remote():
            return False, "no remote configured"
        try:
            code, out, err = self._run(["push"], timeout=LONG, check=False)
        except RepoError as e:
            return False, str(e)
        return (code == 0), (err or out).strip()

    def pull(self):
        """Fast-forward only. A refused fast-forward means the history has
        diverged, and that is a human decision — never an automatic merge."""
        if not self.has_remote():
            return False, "no remote configured"
        try:
            code, out, err = self._run(["pull", "--ff-only"], timeout=LONG,
                                       check=False)
        except RepoError as e:
            return False, str(e)
        return (code == 0), (err or out).strip()

    # -- setup -----------------------------------------------------------
    def init(self):
        """Create the tree and an empty repository. Used once, by hand, and
        by the tests. Never called automatically: silently initialising a
        repository where someone expected a clone is how you end up with two
        histories and no remote."""
        os.makedirs(self.root, exist_ok=True)
        ok, _ = self.available()
        if ok:
            return False
        self._run(["init", "-q", "-b", "main"], check=False)
        return True
