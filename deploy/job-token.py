#!/usr/bin/env python3
"""
Print one job's check-in token, for piping into ssh.

    python3 deploy/job-token.py nightly | ssh root@host 'read -r T; ...'

The match is a case-insensitive substring of the job name, so it can be given
without quotes — which matters more than it should, because PowerShell strips
quotes out of arguments on their way to a native command and every attempt to
inline a regex or a quoted name has failed for that reason.

It refuses an ambiguous match rather than picking one. Writing the wrong
token into a host's config produces a job that silently never checks in, and
"silence is the alert" then reports a backup failure that did not happen.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = os.path.join(ROOT, "checker", "checks.toml")


def jobs():
    text = open(CHECKS, encoding="utf-8").read()
    if "[[jobs]]" not in text:
        return []
    out = []
    for chunk in text[text.index("[[jobs]]"):].split("[[jobs]]"):
        name = re.search(r'name\s*=\s*"([^"]+)"', chunk)
        token = re.search(r'token\s*=\s*"([^"]+)"', chunk)
        if name and token:
            out.append((name.group(1), token.group(1)))
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: job-token.py <part of the job name>", file=sys.stderr)
        print("\nknown jobs:", file=sys.stderr)
        for n, _ in jobs():
            print("  " + n, file=sys.stderr)
        return 2

    want = " ".join(sys.argv[1:]).strip().lower()
    hits = [(n, t) for n, t in jobs() if want in n.lower()]

    if not hits:
        print(f"no job matching {want!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"{want!r} matches more than one job:", file=sys.stderr)
        for n, _ in hits:
            print("  " + n, file=sys.stderr)
        print("be more specific", file=sys.stderr)
        return 1

    # stdout is the token and nothing else, so it can be piped straight in.
    # The name goes to stderr so a human watching still sees what was chosen.
    print(f"-> {hits[0][0]}", file=sys.stderr)

    # Write BYTES, not text. Python's text stdout translates \n to \r\n on
    # Windows. `read -r` on the far side strips the newline but keeps the
    # carriage return, so the token silently gains a trailing \r and every
    # URL built from it is malformed. curl reports that as "bad/illegal
    # format", which looks nothing like the token problem it actually is —
    # and the same class of bug already bit this estate once, in .env.
    sys.stdout.flush()
    sys.stdout.buffer.write(hits[0][1].encode() + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
