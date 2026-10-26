"""
Probes — the four ways we ask "is it there?"

Each returns (ok, milliseconds_or_None, human_readable_detail). The detail
string is what lands in the alert and in the portal, so it is written for
someone reading it at 3am, not for a log parser.
"""

import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


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


def _asn1_time(tag: int, raw: bytes):
    s = raw.decode("ascii").rstrip("Z")
    fmt = "%y%m%d%H%M%S" if tag == 0x17 else "%Y%m%d%H%M%S"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def _validity(der: bytes):
    """Pull notBefore/notAfter out of a DER certificate.

    The standard library has no x509 parser, and getpeercert() returns nothing
    useful once verification is disabled — which it has to be, because half
    this estate serves self-signed certs. So: scan for the Validity SEQUENCE,
    which is the first one holding two consecutive ASN.1 time values.
    """
    i = 0
    while i < len(der) - 4:
        if der[i] == 0x30:
            j, times = i + 2, []
            for _ in range(2):
                if j + 1 < len(der) and der[j] in (0x17, 0x18):
                    ln = der[j + 1]
                    times.append((der[j], der[j + 2:j + 2 + ln]))
                    j += 2 + ln
                else:
                    break
            if len(times) == 2:
                try:
                    return [_asn1_time(t, v) for t, v in times]
                except ValueError:
                    pass
        i += 1
    return None


def hostport_of(target: str):
    """Host and port from an http(s) URL, defaulting the port by scheme."""
    m = re.match(r"https?://([^/:]+)(?::(\d+))?", target)
    if not m:
        return None, None
    return m.group(1), int(m.group(2) or (443 if target.startswith("https") else 80))


def renewal_threshold(life_seconds: float) -> float:
    """How little time left counts as "renew this soon", for a cert of this
    lifetime. 15% of its own life, floored at an hour so a twelve-hour cert
    is not permanently alarming, and capped at 21 days so a ten-year one is
    not either."""
    return min(max(life_seconds * 0.15, 3600), 21 * 86400)


def cert_info(host: str, port: int, timeout: int):
    """When does this certificate expire, and is that soon *for this cert*?

    A fixed "warn under 14 days" is useless here: Caddy's internal CA issues
    twelve-HOUR certificates and renews them automatically, so a fixed
    threshold would alert twice a day about something working perfectly.
    Judge each cert against its own lifetime instead.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
    except OSError as e:
        return None, str(e)
    if not der:
        return None, "no certificate presented"
    v = _validity(der)
    if not v:
        return None, "could not read the validity dates"

    nb, na = v
    now_ = datetime.now(timezone.utc)
    left = (na - now_).total_seconds()
    life = max((na - nb).total_seconds(), 1.0)
    threshold = renewal_threshold(life)
    return {
        "not_before": int(nb.timestamp()),
        "not_after": int(na.timestamp()),
        "days_left": round(left / 86400, 2),
        "lifetime_days": round(life / 86400, 2),
        "expired": left <= 0,
        "renew_soon": 0 < left < threshold,
    }, "ok"


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
