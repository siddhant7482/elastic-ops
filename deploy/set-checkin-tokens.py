#!/usr/bin/env python3
"""
Push each collector host's check-in token to it, without displaying one.

The tokens in checks.toml are credentials. Reading them out to a terminal to
copy them by hand puts them in a scrollback, a clipboard and whatever was
watching the screen, which is how one set already had to be rotated. This
does the same job and prints only host names and outcomes.

How the token travels:

  * Over **stdin**, so it is never an argument to ssh and never appears in
    the local shell history or process list.
  * On the far side it is written with `printf`, a shell builtin, so it never
    becomes an argument to sed or any other process either. `ps` is not a
    privilege, and the window does not have to be long to matter.

Verification is done by **the host itself**, by running the real collector
unit, not by this script calling /checkin/ — doing that from here would mark
the job healthy without the collector having run, which is precisely the lie
the whole job-monitoring design exists to prevent.

    python3 deploy/set-checkin-tokens.py                 # all collector hosts
    python3 deploy/set-checkin-tokens.py pepper yam      # just these
    python3 deploy/set-checkin-tokens.py --dry-run

Jobs that are not collectors — the nightly backup, the store verify, the SQL
dumps — are called from cron lines this cannot see. They are listed at the
end so they are not forgotten.
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = os.path.join(ROOT, "checker", "checks.toml")
INVENTORY = os.path.join(ROOT, "checker", "inventory.toml")

# What install-collector.sh actually writes. Not /etc/elastic-ops/anything:
# the collector predates that path and keeps its own.
ENV_PATH = "/etc/default/estate-collector"
UNIT = "estate-collector-metrics"

# Read the token from stdin, rewrite the file with a builtin so the value
# never reaches another process's argv, then run the real collector once.
REMOTE = (
    'read -r T; F=' + ENV_PATH + '; '
    'touch "$F"; '
    'grep -v "^OPS_CHECKIN=" "$F" > "$F.new" 2>/dev/null || true; '
    'printf "OPS_CHECKIN=%s\\n" "$T" >> "$F.new"; '
    'mv "$F.new" "$F"; chmod 600 "$F"; chown root:root "$F"; '
    'if systemctl start ' + UNIT + '; then echo CITADEL-OK; '
    'else echo CITADEL-FAIL; '
    'journalctl -u ' + UNIT + ' -n 4 --no-pager 2>/dev/null | tail -4; fi'
)


def jobs():
    """{host: token} for the per-host collector jobs, plus the rest by name."""
    text = open(CHECKS, encoding="utf-8").read()
    out, other = {}, []
    if "[[jobs]]" not in text:
        return out, other
    for chunk in text[text.index("[[jobs]]"):].split("[[jobs]]"):
        name = re.search(r'name\s*=\s*"([^"]+)"', chunk)
        token = re.search(r'token\s*=\s*"([^"]+)"', chunk)
        if not (name and token):
            continue
        m = re.match(r"Collector\s*[—–-]\s*(\S+)", name.group(1))
        if m:
            out[m.group(1)] = token.group(1)
        else:
            other.append(name.group(1))
    return out, other


def addresses():
    text = open(INVENTORY, encoding="utf-8").read()
    out = {}
    for chunk in text.split("[[hosts]]")[1:]:
        name = re.search(r'name\s*=\s*"([^"]+)"', chunk)
        addr = re.search(r'address\s*=\s*"([^"]+)"', chunk)
        if name and addr and addr.group(1).strip():
            out[name.group(1)] = addr.group(1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hosts", nargs="*", help="limit to these hosts")
    ap.add_argument("--user", default="root")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    found, other = jobs()
    addr = addresses()
    want = args.hosts or sorted(found)

    missing = [h for h in want if h not in found]
    if missing:
        print(f"no collector job for: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"Setting OPS_CHECKIN in {ENV_PATH} and running {UNIT} once.")
    print("You will be asked for each host's password.\n")

    failures = 0
    for host in want:
        target = addr.get(host, host)
        if args.dry_run:
            print(f"  would set {host:14} ({target})")
            continue
        try:
            p = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10",
                 f"{args.user}@{target}", REMOTE],
                input=found[host] + "\n", text=True, capture_output=True,
                timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  FAIL {host:14} {e}")
            failures += 1
            continue

        out = (p.stdout or "") + (p.stderr or "")
        if "CITADEL-OK" in out:
            print(f"  ok   {host:14} set, and the collector checked in")
        else:
            failures += 1
            detail = " / ".join(
                x.strip() for x in out.splitlines()
                if x.strip() and "CITADEL-" not in x)[:160]
            print(f"  FAIL {host:14} {detail or 'no output'}")

    if other:
        print("\nCalled from cron, so not reachable from here. Update the cron")
        print("line on whichever host runs each:")
        for n in other:
            print(f"  - {n}")

    if failures:
        print(f"\n{failures} host(s) need attention", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
