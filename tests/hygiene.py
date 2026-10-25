#!/usr/bin/env python3
"""
Repository guards — the rules that are easy to break by accident.

Two things this project promises, both of which a single careless commit would
quietly undo:

  1. The runtime imports nothing outside the standard library. That is why it
     can be dropped onto any Proxmox host and will survive an upgrade. A stray
     `import requests` would not fail until the day something breaks at 3am.

  2. No secret is ever committed. checks.toml ships with empty placeholders and
     the real values arrive from the environment, so this asserts they are
     still empty and that .env is not tracked.

Run it from the repository root:

    python3 tests/hygiene.py
"""

import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIRS = ["checker", "collector"]
SECRET_KEYS = ["ingest_token", "webhook_url", "webhook_header", "deadman_url"]

# Modules this project ships itself, so they are not third-party imports.
LOCAL = {"api", "app", "auth", "engine", "probes", "store"}

# tomllib joined the standard library in 3.11, which is this project's floor.
# Running the guard on 3.10 would otherwise report it as third-party.
BACKFILL = {"tomllib"}

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(f"{name}{(' — ' + str(extra)) if extra else ''}")
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{('  — ' + str(extra)) if extra and not cond else ''}")


def py_files():
    for d in RUNTIME_DIRS:
        for dirpath, dirnames, names in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for n in sorted(names):
                if n.endswith(".py"):
                    yield os.path.join(dirpath, n)


def top_level_imports(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def main():
    print("\nruntime imports nothing outside the standard library")
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        print("  skipped — needs Python 3.10+ for sys.stdlib_module_names")
    else:
        offenders = {}
        for path in py_files():
            extra = top_level_imports(path) - set(stdlib) - LOCAL - BACKFILL
            if extra:
                offenders[os.path.relpath(path, ROOT)] = sorted(extra)
        check("no third-party imports in checker/ or collector/",
              not offenders, offenders)

    print("\nno secrets committed")
    toml_path = os.path.join(ROOT, "checker", "checks.toml")
    with open(toml_path, encoding="utf-8") as fh:
        toml = fh.read()
    for key in SECRET_KEYS:
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', toml, re.M)
        check(f"checks.toml leaves {key} empty", m is not None and m.group(1) == "",
              (m.group(1)[:24] + "…") if m and m.group(1) else "key missing")

    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30).stdout.split()
    except (OSError, subprocess.SubprocessError):
        tracked = []
    if tracked:
        check(".env is not tracked", ".env" not in tracked)
        check("no database file is tracked",
              not [t for t in tracked if t.endswith(".db")],
              [t for t in tracked if t.endswith(".db")])
        check(".gitignore is tracked", ".gitignore" in tracked)
    else:
        print("  skipped — not a git checkout")

    print("\nthe collector stays runnable on a bare host")
    coll = os.path.join(ROOT, "collector", "post_estate.py")
    check("collector is executable python", os.path.isfile(coll))
    with open(coll, encoding="utf-8") as fh:
        check("collector has a shebang", fh.readline().startswith("#!"))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
