"""
Minimal SNMP v2c — GET and WALK, enough for the Printer MIB.

There is no SNMP in the standard library and this project does not take
dependencies, so this encodes the handful of ASN.1/BER types SNMP actually
uses. That is less heroic than it sounds: a GetRequest is four integers, a
string and a list of OIDs, and the wire format has not changed since 1996.

Deliberately NOT implemented: SET (nothing here should ever write to a
device), v3 (needs crypto), and traps. Read-only by construction.

    get("10.10.1.50", ["1.3.6.1.2.1.1.1.0"])      -> {oid: value}
    walk("10.10.1.50", "1.3.6.1.2.1.43.11.1.1.6") -> [(oid, value), ...]
"""

import random
import socket
import time
import struct

# ASN.1 / BER tags
SEQUENCE = 0x30
INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OID = 0x06
GET_REQUEST = 0xA0
GET_NEXT = 0xA1
GET_RESPONSE = 0xA2

# SNMP application types, all of which decode as unsigned integers
COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x41, 0x42, 0x43, 0x44, 0x46
IPADDRESS = 0x40
# "this object does not exist here", returned instead of a value
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB = 0x80, 0x81, 0x82


class SnmpError(Exception):
    pass


# ----------------------------------------------------------------- encoding


def _len(n):
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(n):
    if n == 0:
        return _tlv(INTEGER, b"\x00")
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return _tlv(INTEGER, raw)


def encode_oid(oid):
    parts = [int(p) for p in oid.strip(".").split(".")]
    if len(parts) < 2:
        raise SnmpError(f"not an OID: {oid}")
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            out.append(p)
            continue
        chunk = bytearray()
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        chunk[-1] &= 0x7F
        out += chunk
    return _tlv(OID, bytes(out))


# ----------------------------------------------------------------- decoding


def _read_len(data, i):
    n = data[i]
    i += 1
    if n < 0x80:
        return n, i
    k = n & 0x7F
    return int.from_bytes(data[i:i + k], "big"), i + k


def _read_tlv(data, i):
    tag = data[i]
    size, i = _read_len(data, i + 1)
    return tag, data[i:i + size], i + size


def decode_oid(raw):
    if not raw:
        return ""
    first = raw[0]
    parts = [first // 40, first % 40]
    val = 0
    for b in raw[1:]:
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(val)
            val = 0
    return ".".join(str(p) for p in parts)


def _decode_value(tag, raw):
    if tag in (INTEGER, COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(raw, "big", signed=(tag == INTEGER)) if raw else 0
    if tag == OCTET_STRING:
        # Printers return a mix of ASCII and, occasionally, raw bytes.
        return raw.decode("utf-8", "replace").strip("\x00").strip()
    if tag == OID:
        return decode_oid(raw)
    if tag == IPADDRESS:
        return ".".join(str(b) for b in raw)
    if tag == NULL:
        return None
    if tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB):
        return None
    return raw


# ----------------------------------------------------------------- transport


def _request(host, port, community, oids, pdu_type, timeout, retries=2):
    """SNMP is UDP, which does not retransmit. A single lost packet on a
    routed link would otherwise read as "device is dead" — which is exactly
    how a healthy printer across a router got marked unreachable."""
    last = None
    for attempt in range(retries + 1):
        try:
            return _request_once(host, port, community, oids, pdu_type, timeout)
        except (OSError, SnmpError, IndexError, struct.error) as e:
            last = e
            if attempt < retries:
                time.sleep(0.25 * (attempt + 1))
    raise last if last else SnmpError("no response")


def _request_once(host, port, community, oids, pdu_type, timeout):
    rid = random.randint(1, 0x7FFFFFFF)                      # noqa: S311
    binds = b"".join(_tlv(SEQUENCE, encode_oid(o) + _tlv(NULL, b"")) for o in oids)
    pdu = _tlv(pdu_type, _int(rid) + _int(0) + _int(0) + _tlv(SEQUENCE, binds))
    msg = _tlv(SEQUENCE, _int(1) + _tlv(OCTET_STRING, community.encode()) + pdu)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (host, port))
        data, _ = s.recvfrom(65535)
    finally:
        s.close()

    _, body, _ = _read_tlv(data, 0)                 # outer SEQUENCE
    i = 0
    _, _, i = _read_tlv(body, i)                    # version
    _, _, i = _read_tlv(body, i)                    # community
    tag, pdu_body, _ = _read_tlv(body, i)
    if tag != GET_RESPONSE:
        raise SnmpError(f"unexpected PDU 0x{tag:02x}")

    j = 0
    _, _, j = _read_tlv(pdu_body, j)                # request id
    _, err_raw, j = _read_tlv(pdu_body, j)          # error-status
    _, _, j = _read_tlv(pdu_body, j)                # error-index
    err = int.from_bytes(err_raw, "big") if err_raw else 0
    if err:
        raise SnmpError(f"agent returned error-status {err}")

    _, binds_raw, _ = _read_tlv(pdu_body, j)
    out, k = [], 0
    while k < len(binds_raw):
        _, bind, k = _read_tlv(binds_raw, k)
        m = 0
        _, oid_raw, m = _read_tlv(bind, m)
        vtag, vraw, _ = _read_tlv(bind, m)
        out.append((decode_oid(oid_raw), _decode_value(vtag, vraw)))
    return out


def get(host, oids, community="public", timeout=2.0, port=161):
    """One GET for up to a few dozen OIDs. Missing ones come back as None."""
    if isinstance(oids, str):
        oids = [oids]
    return dict(_request(host, port, community, oids, GET_REQUEST, timeout))


def walk(host, base, community="public", timeout=2.0, port=161, limit=256):
    """GETNEXT until we leave the subtree. Printers have small tables, so
    there is no need for GETBULK and its extra complexity."""
    out, cur = [], base
    for _ in range(limit):
        try:
            pairs = _request(host, port, community, [cur], GET_NEXT, timeout)
        except (SnmpError, OSError):
            break
        if not pairs:
            break
        oid, val = pairs[0]
        if not oid.startswith(base.strip(".") + ".") or val is None and oid == cur:
            break
        out.append((oid, val))
        cur = oid
    return out


def alive(host, community="public", timeout=1.5, port=161):
    """Does anything answer SNMP here? Returns sysDescr, or None."""
    try:
        r = get(host, ["1.3.6.1.2.1.1.1.0"], community, timeout, port)
    except (SnmpError, OSError, struct.error, IndexError):
        return None
    return r.get("1.3.6.1.2.1.1.1.0")
