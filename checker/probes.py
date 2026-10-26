"""
Probes — the four ways we ask "is it there?"

Each returns (ok, milliseconds_or_None, human_readable_detail). The detail
string is what lands in the alert and in the portal, so it is written for
someone reading it at 3am, not for a log parser.
"""

import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request


def probe_tcp(target: str, timeout: int):
    host, _, port = target.rpartition(":")
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return True, int((time.monotonic() - t0) * 1000), "connected"
    except OSError as e:
        return False, None, str(e)


def probe_http(target: str, timeout: int, insecure: bool):
    # Proxmox and PBS serve self-signed certs on 8006/8007. Verifying them
    # would mean every hypervisor check failing for a reason nobody can fix,
    # so https checks skip verification and tcp proves the port separately.
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(target, headers={"User-Agent": "elastic-ops/1"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            code = r.status
        return code < 400, int((time.monotonic() - t0) * 1000), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        # 401/403 still proves something is listening and answering.
        return e.code in (401, 403), int((time.monotonic() - t0) * 1000), f"HTTP {e.code}"
    except Exception as e:
        return False, None, str(e)


def probe_ping(target: str, timeout: int):
    host = target.split(":")[0]
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 2,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return r.returncode == 0, ms, "reply" if r.returncode == 0 else "no reply"
    except Exception as e:
        return False, None, str(e)


def browser_port(host: str, instance: str, timeout: int):
    """Ask SQL Server Browser (UDP 1434) which TCP port an instance is on.

    Send one 0x02 byte and it replies with every instance and its port. This
    is what a client does to resolve "host\\INSTANCE", and it needs no
    credentials and never touches the engine.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"\x02", (host, 1434))
        data, _ = s.recvfrom(65535)
    except OSError:
        return None, "SQL Browser did not answer on UDP 1434"
    finally:
        s.close()
    for chunk in data[3:].decode("ascii", "replace").split(";;"):
        f = chunk.split(";")
        # strict=False on purpose: a truncated or odd reply should be ignored,
        # not raise. We only care whether the instance we want is in there.
        d = dict(zip(f[0::2], f[1::2], strict=False))
        if d.get("InstanceName", "").upper() == instance.upper():
            port = d.get("tcp")
            if port and port.isdigit():
                return int(port), f"instance {instance} on {port}"
            return None, f"instance {instance} advertises no TCP port"
    return None, f"SQL Browser does not know an instance called '{instance}'"


def probe_mssql(target: str, timeout: int):
    """host\\INSTANCE, resolved through SQL Browser before connecting.

    A named instance gets a dynamic port that moves when the service
    restarts, so pinning the number means the check goes red for a reason
    nobody can act on. Resolving first is the difference between a check that
    survives a reboot and one that cries wolf after every patch Tuesday.
    """
    host, sep, instance = target.partition("\\")
    if not sep:
        return probe_tcp(target, timeout)
    port, why = browser_port(host, instance, timeout)
    if not port:
        return False, None, why
    ok, ms, detail = probe_tcp(f"{host}:{port}", timeout)
    return ok, ms, (f"{detail}, {why}" if ok else f"{detail} (port {port})")


def run(check: dict, timeout: int):
    kind = check["type"]
    if kind == "tcp":
        return probe_tcp(check["target"], timeout)
    if kind in ("http", "https"):
        return probe_http(check["target"], timeout, insecure=(kind == "https"))
    if kind == "mssql":
        return probe_mssql(check["target"], timeout)
    if kind == "ping":
        return probe_ping(check["target"], timeout)
    return False, None, f"unknown check type '{kind}'"
