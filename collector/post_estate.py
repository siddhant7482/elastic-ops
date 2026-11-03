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


def _sysread(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


# Levels that survive losing a disk. Anything else — raid0, linear, a single
# device — is one failure away from total loss, which the portal needs to say
# out loud rather than leave the reader to infer from the level string.
REDUNDANT_LEVELS = {"raid1", "raid4", "raid5", "raid6", "raid10"}


def md_arrays(root="/sys/block"):
    """Linux software RAID, read out of /sys rather than parsed from
    /proc/mdstat. The sysfs files are one value each; mdstat is a single
    human-readable blob whose layout has changed between kernel versions.

    root is a parameter so the parser can be tested against a fixture tree
    instead of whatever disks the test machine happens to have.
    """
    out = []
    try:
        blocks = sorted(os.listdir(root))
    except OSError:
        return out
    for dev in blocks:
        base = f"{root}/{dev}/md"
        if not os.path.isdir(base):
            continue
        level = _sysread(f"{base}/level")
        members = []
        try:
            for entry in sorted(os.listdir(base)):
                if entry.startswith("dev-"):
                    members.append({
                        "dev": entry[4:],
                        "state": _sysread(f"{base}/{entry}/state"),
                    })
        except OSError:
            pass
        try:
            degraded = int(_sysread(f"{base}/degraded", "0") or 0)
        except ValueError:
            degraded = 0
        try:
            disks = int(_sysread(f"{base}/raid_disks", "0") or 0)
        except ValueError:
            disks = 0
        out.append({
            "kind": "mdraid",
            "name": dev,
            "level": level,
            "state": _sysread(f"{base}/array_state"),
            "degraded": degraded,
            "disks": disks,
            "redundant": level in REDUNDANT_LEVELS,
            "sync_action": _sysread(f"{base}/sync_action"),
            "sync_completed": _sysread(f"{base}/sync_completed"),
            "members": members,
        })
    return out


ZFS_STATES = ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED")


def zfs_arrays():
    """zpool health. Deliberately separate from pools() above: pvesm reports
    how full a pool is and says nothing about whether its vdev is intact."""
    out = []
    names = [ln.split("\t")[0].strip()
             for ln in sh("zpool", "list", "-H", "-o", "name").splitlines()
             if ln.strip()]
    for name in names:
        health, members, redundant = "", [], False
        for raw in sh("zpool", "status", name).splitlines():
            line = raw.strip()
            if line.startswith("state:"):
                health = line.split(":", 1)[1].strip()
                continue
            # A mirror or raidz vdev tolerates a disk loss; a bare stripe of
            # devices does not, and zpool reports both as simply ONLINE.
            if re.match(r"^(mirror|raidz[123]?)\b", line):
                redundant = True
                continue
            m = re.match(r"^(\S+)\s+(" + "|".join(ZFS_STATES) + r")\b", line)
            if m and m.group(1) not in (name, "NAME"):
                members.append({"dev": m.group(1), "state": m.group(2)})
        out.append({
            "kind": "zfs",
            "name": name,
            "level": "zfs",
            "state": health,
            "degraded": 0 if health == "ONLINE" else 1,
            "disks": len(members),
            "redundant": redundant,
            "sync_action": "",
            "sync_completed": "",
            "members": members,
        })
    return out


_UNITS = {"KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12,
          "KIB": 2 ** 10, "MIB": 2 ** 20, "GIB": 2 ** 30, "TIB": 2 ** 40}


def _size_to_bytes(s):
    m = re.match(r"^([\d.]+)\s*([KMGT]i?B)$", (s or "").strip(), re.I)
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()]) if m else None

# Logical-drive states that mean "rebuilding", not "broken". HPE distinguishes
# these and so should we: a rebuild resolves itself, and paging someone at 2am
# for a disk that is already fixing itself is how a pager gets ignored.
SSA_REBUILD = {"recovering", "rebuilding", "ready for recovery", "expanding",
               "transforming", "queued for expansion", "queued for rebuild"}


def ssacli_arrays():
    """HPE / Adaptec Smart Array, via ssacli.

    These controllers hand Linux one block device per array and keep the real
    spindles to themselves, so /sys, zpool and SMART all see a single healthy
    "LOGICAL VOLUME" no matter what is happening behind it. On proxmox that
    hid a RAID 5 running in Interim Recovery Mode with a dead disk in bay
    2I:3:8 — the array was serving data with its redundancy already spent,
    and nothing outside the controller could tell.

    Returns (arrays, physical_drives). Parsed from `ssacli ctrl all show
    config`, whose shape is:

        HPE Smart Array P408i-a SR Gen10 in Slot 0 (Embedded)  (sn: ...)
           Array A (SAS,
              logicaldrive 1 (1.36 TB, RAID 5, Interim Recovery Mode)
              physicaldrive 1I:3:3 (port 1I:box 3:bay 3, SAS HDD, 300 GB, OK)

    Note the unclosed bracket after "Array A (SAS," — that is real output, not
    a transcription slip, so nothing here may depend on balanced brackets.
    """
    text = sh("ssacli", "ctrl", "all", "show", "config")
    if not text.strip():
        return [], []                 # not installed, or no controller
    arrays, drives = [], []
    slot, cur = "?", None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.search(r"\bin Slot (\S+)", line)
        if m and not line.startswith(("Array", "logicaldrive", "physicaldrive")):
            slot = m.group(1)
            cur = None
            continue
        m = re.match(r"^Array ([A-Za-z0-9]+)", line)
        if m:
            cur = {"kind": "smartarray", "name": f"slot{slot} Array {m.group(1)}",
                   "level": "", "state": "", "degraded": 0, "disks": 0,
                   "redundant": False, "sync_action": "", "sync_completed": "",
                   "members": []}
            arrays.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^logicaldrive\s+\S+\s+\((.*?)\)?$", line)
        if m:
            parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
            lvl = next((p for p in parts if p.upper().startswith("RAID")), "")
            cur["level"] = lvl.lower().replace(" ", "")      # "RAID 5" -> raid5
            cur["state"] = parts[-1] if parts else ""
            cur["redundant"] = cur["level"] in REDUNDANT_LEVELS
            continue
        m = re.match(r"^physicaldrive\s+(\S+)\s+\((.*?)\)?$", line)
        if m:
            bay = m.group(1)
            parts = [p.strip() for p in m.group(2).split(",") if p.strip()]
            status = parts[-1] if parts else ""
            kind = next((p for p in parts if "HDD" in p or "SSD" in p), "")
            size = next((p for p in parts
                         if re.match(r"^[\d.]+\s*[KMGT]i?B$", p, re.I)), "")
            cur["members"].append({"dev": bay, "state": status})
            drives.append({"dev": bay, "model": kind, "serial": "",
                           "size_bytes": _size_to_bytes(size),
                           "health": status, "wearout": None,
                           "type": "ssd" if "SSD" in kind.upper() else "hdd",
                           "used": cur["name"]})

    for a in arrays:
        a["disks"] = len(a["members"])
        state = (a["state"] or "").lower()
        bad = sum(1 for m in a["members"]
                  if (m["state"] or "").upper() not in ("OK", "REBUILDING"))
        if state in SSA_REBUILD:
            a["sync_action"] = state
            a["degraded"] = bad
        elif state and state != "ok":
            # The vendor's vocabulary is only meaningful here, so the
            # translation happens here: anything that is not OK and not a
            # rebuild counts as degraded even when every member still reads
            # OK, because a pulled disk stops being listed at all.
            a["degraded"] = max(1, bad)
        else:
            a["degraded"] = bad
    return arrays, drives


def disks(node):
    """Per-disk SMART health.

    Goes through pvesh rather than calling smartctl directly: on a Proxmox
    host it is already there and already root, it normalises the vendor
    differences, and it will not spin up a disk that is parked just to ask
    how it feels.
    """
    raw = sh("pvesh", "get", f"/nodes/{node}/disks/list", "--output-format", "json")
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for d in rows if isinstance(rows, list) else []:
        out.append({
            "dev": d.get("devpath") or "",
            "model": (d.get("model") or "").strip(),
            "serial": (d.get("serial") or "").strip(),
            "size_bytes": d.get("size"),
            # PASSED / OK on a healthy drive, UNKNOWN where SMART is not
            # readable (most virtual disks), anything else is a real answer.
            "health": (d.get("health") or "UNKNOWN").strip(),
            "wearout": d.get("wearout"),
            "type": (d.get("type") or "").strip(),
            "used": (d.get("used") or "").strip(),
        })
    return out


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


def guests_with_addresses(node):
    """Every guest on this node, with whatever addresses the agent will admit.

    This is what lets the portal subtract: everything that exists, minus
    everything that is checked, is everything nobody is watching. The agent
    call is per guest and only worth making for running ones, so the daily
    run does it and the ten-minute metrics run does not.
    """
    out = []
    for kind in ("qemu", "lxc"):
        raw = sh("pvesh", "get", f"/nodes/{node}/{kind}", "--output-format", "json")
        try:
            items = json.loads(raw or "[]")
        except ValueError:
            continue
        for g in items:
            vmid = str(g.get("vmid"))
            row = {"vmid": vmid, "name": g.get("name") or vmid, "kind": kind,
                   "status": g.get("status"), "addresses": []}
            if g.get("status") == "running":
                row["addresses"] = guest_addresses(node, kind, vmid)
            out.append(row)
    return out


def guest_addresses(node, kind, vmid):
    if kind == "qemu":
        raw = sh("pvesh", "get", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
                 "--output-format", "json")
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            return []                       # no guest agent, which is common
        ifaces = data.get("result", data) if isinstance(data, dict) else data
        return _addrs_from(ifaces, "ip-addresses", "ip-address")

    raw = sh("pvesh", "get", f"/nodes/{node}/lxc/{vmid}/interfaces",
             "--output-format", "json")
    try:
        return _addrs_from(json.loads(raw or "[]"), None, "inet")
    except ValueError:
        return []


def _addrs_from(ifaces, list_key, addr_key):
    found = []
    for i in ifaces if isinstance(ifaces, list) else []:
        entries = i.get(list_key, []) if list_key else [i]
        for e in entries if isinstance(entries, list) else []:
            a = (e.get(addr_key) or "").split("/")[0].strip()
            # Loopback and link-local say nothing about reachability.
            if a and not a.startswith(("127.", "::1", "fe80", "169.254")):
                found.append(a)
    return sorted(set(found))


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
    payload = {"host": host, "mem_total": total, "mem_used": used,
               "pools": pools(), "at": int(time.time())}
    # Integrity goes on the ten-minute run, not the daily one. Capacity can
    # wait a day; a degraded array cannot, because the second failure is the
    # one that costs the data.
    ssa_arrays, ssa_drives = ssacli_arrays()
    payload["arrays"] = md_arrays() + zfs_arrays() + ssa_arrays
    # Both halves go in: the controller's logical volume as pvesh sees it, and
    # the real spindles behind it. The logical volume reports UNKNOWN for SMART
    # and is ignored; the physical drives are where the truth is.
    payload["disks"] = disks(host) + ssa_drives
    # The metrics run omits 'guests' entirely rather than sending an empty
    # list, which the portal would read as "this host has none".
    if not a.metrics_only:
        payload["guests"] = guests_with_addresses(host)
    ok = post(url, token, "/api/ingest/host", payload, a.dry_run)

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
