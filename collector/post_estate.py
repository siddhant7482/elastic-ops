#!/usr/bin/env python3
"""
Estate collector — runs ON a Proxmox host, posts to the portal.

The portal deliberately does not reach into five hypervisors: that would mean
giving it root credentials for the whole estate and a network path to every
management interface. Instead each host pushes a small report outward, with a
shared token, to one endpoint. If this stops running the portal notices,
because the collector also checks in as a job.

Install on each hypervisor (cinnamon, proxmox, yam, pepper, networkserver):

    install -m 755 post_estate.py /opt/estate/post_estate.py
    crontab -e
      5 6 * * *  /opt/estate/post_estate.py >> /var/log/estate-collector.log 2>&1
      */10 * * * * /opt/estate/post_estate.py --metrics-only >> /dev/null 2>&1

Configure by environment, so the token is not in the script:

    /etc/default/estate-collector
      OPS_URL=http://10.10.1.70:8080
      OPS_TOKEN=<the ingest_token from checks.toml>

Standard library only — Proxmox ships Python 3.11, and a monitoring collector
that needs pip is a collector that breaks on upgrade day.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def env_file(path="/etc/default/estate-collector"):
    """Read KEY=value lines without needing the shell to have sourced them."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------------ gather


def memory():
    """Total and used in bytes, from /proc/meminfo. 'Used' means used — it
    excludes buffers and cache, which is what you actually want to see."""
    vals = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                vals[k] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None, None
    total = vals.get("MemTotal")
    avail = vals.get("MemAvailable")
    if total is None or avail is None:
        return total, None
    return total, total - avail


def pools():
    """Proxmox storage, via pvesm. Falls back to nothing on a non-PVE host."""
    out = []
    text = sh("pvesm", "status")
    for line in text.splitlines()[1:]:
        f = line.split()
        # Name Type Status Total Used Available %
        if len(f) >= 6 and f[2] == "active":
            try:
                out.append({"name": f[0],
                            "total_bytes": int(f[3]) * 1024,
                            "used_bytes": int(f[4]) * 1024})
            except ValueError:
                continue
    return out


def backups():
    """One row per guest on this host, with the age of its newest backup.

    Read from `pvesh get /nodes/<node>/storage/<store>/content`, so a guest
    with no backup at all shows up as last_ts=None rather than being silently
    absent — the machines with nothing are the whole point of the page.
    """
    node = socket.gethostname().split(".")[0]
    guests = []
    for kind in ("qemu", "lxc"):
        raw = sh("pvesh", "get", f"/nodes/{node}/{kind}", "--output-format", "json")
        try:
            for g in json.loads(raw or "[]"):
                guests.append({"vmid": str(g.get("vmid")),
                               "name": g.get("name") or str(g.get("vmid"))})
        except ValueError:
            pass
    if not guests:
        return []

    newest = {}
    stores = re.findall(r"^(\S+)", sh("pvesm", "status"), re.M)[1:]
    for store in stores:
        raw = sh("pvesh", "get", f"/nodes/{node}/storage/{store}/content",
                 "--content", "backup", "--output-format", "json")
        try:
            items = json.loads(raw or "[]")
        except ValueError:
            continue
        for it in items:
            vmid = str(it.get("vmid"))
            ctime, size = it.get("ctime"), it.get("size")
            if not vmid or not ctime:
                continue
            if vmid not in newest or ctime > newest[vmid]["last_ts"]:
                newest[vmid] = {"last_ts": int(ctime), "size_bytes": size}

    rows = []
    for g in guests:
        hit = newest.get(g["vmid"])
        age_h = (time.time() - hit["last_ts"]) / 3600 if hit else None
        if hit is None:
            state, detail = "bad", "no backup has ever been taken"
        elif age_h > 48:
            state, detail = "warn", f"newest backup is {int(age_h / 24)} days old"
        else:
            state, detail = "ok", "verified"
        rows.append({"vmid": f"{node}/{g['vmid']}", "name": f"{g['vmid']} {g['name']}",
                     "host": node, "last_ts": hit["last_ts"] if hit else None,
                     "size_bytes": hit["size_bytes"] if hit else None,
                     "state": state, "detail": detail})
    return rows


# ------------------------------------------------------------------ post


def post(url, token, path, payload, dry=False):
    body = json.dumps(payload).encode()
    if dry:
        print(f"--- {path}\n{json.dumps(payload, indent=2)[:4000]}")
        return True
    req = urllib.request.Request(url.rstrip("/") + path, data=body, headers={
        "Content-Type": "application/json", "X-Ingest-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"{path}: {r.status} {r.read().decode()[:200]}")
        return True
    except urllib.error.HTTPError as e:
        print(f"{path}: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"{path}: {e}", file=sys.stderr)
    return False


def checkin(url, token_path):
    if not token_path:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + "/checkin/" + token_path, timeout=10).read()
    except Exception as e:
        print(f"check-in failed: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Post host metrics and backup state to Elastic Ops")
    ap.add_argument("--url", help="portal base URL")
    ap.add_argument("--token", help="ingest token")
    ap.add_argument("--checkin", help="job token to check in as, e.g. backup-collector")
    ap.add_argument("--metrics-only", action="store_true",
                    help="memory and pools only; skip the slower backup scan")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting")
    a = ap.parse_args()

    cfg = env_file()
    url = a.url or os.environ.get("OPS_URL") or cfg.get("OPS_URL")
    token = a.token or os.environ.get("OPS_TOKEN") or cfg.get("OPS_TOKEN")
    if not url or (not token and not a.dry_run):
        sys.exit("set OPS_URL and OPS_TOKEN in /etc/default/estate-collector, "
                 "or pass --url and --token")

    host = socket.gethostname().split(".")[0]
    total, used = memory()
    ok = post(url, token, "/api/ingest/host",
              {"host": host, "mem_total": total, "mem_used": used,
               "pools": pools(), "at": int(time.time())}, a.dry_run)

    if not a.metrics_only:
        rows = backups()
        if rows:
            ok = post(url, token, "/api/ingest/backups", {"backups": rows}, a.dry_run) and ok
        else:
            print("no guests found — not a Proxmox host, or pvesh is unavailable")

    if ok and not a.dry_run:
        checkin(url, a.checkin or os.environ.get("OPS_CHECKIN") or cfg.get("OPS_CHECKIN"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
