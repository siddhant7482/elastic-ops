"""
Printers, over the Printer MIB (RFC 3805).

Brother, Canon and Konica Minolta all implement it, so one reader covers the
estate instead of three vendor integrations. Everything here is a read; this
module cannot write to a device even in principle, because snmp.py has no SET.

Two quirks that look like bugs and are not:

  * A level of -3 means "some remaining" — the device only knows OK or low.
    Brother mono lasers report their toner this way, so a percentage for them
    is not available at any price, and showing 0% would be a lie.
    -1 and -2 mean other/unknown.
  * A max capacity of -2 means "unknown", which rules out a percentage even
    when the current level is a real number.
"""

import snmp

# Printer MIB and Host Resources MIB
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
HR_DEVICE = "1.3.6.1.2.1.25.3.2.1.3.1"
PRT_NAME = "1.3.6.1.2.1.43.5.1.1.16.1"
PRT_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"
PRT_ERRORS = "1.3.6.1.2.1.25.3.5.1.2.1"
PAGE_COUNT = "1.3.6.1.2.1.43.10.2.1.4.1.1"
SUP_DESC = "1.3.6.1.2.1.43.11.1.1.6"
SUP_TYPE = "1.3.6.1.2.1.43.11.1.1.5"
SUP_MAX = "1.3.6.1.2.1.43.11.1.1.8"
SUP_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
SERIAL = "1.3.6.1.2.1.43.5.1.1.17.1"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
CONSOLE = "1.3.6.1.2.1.43.16.5.1.2"
COVER_DESC = "1.3.6.1.2.1.43.6.1.1.2"
COVER_STATUS = "1.3.6.1.2.1.43.6.1.1.3"
COLORANTS = "1.3.6.1.2.1.43.12.1.1.4"
ALERT_SEVERITY = "1.3.6.1.2.1.43.18.1.1.2"
ALERT_DESC = "1.3.6.1.2.1.43.18.1.1.8"
TRAY_CAPACITY = "1.3.6.1.2.1.43.8.2.1.9"
TRAY_DIM_UNIT = "1.3.6.1.2.1.43.8.2.1.3"
TRAY_DIM_FEED = "1.3.6.1.2.1.43.8.2.1.4"
TRAY_DIM_CROSS = "1.3.6.1.2.1.43.8.2.1.5"
TRAY_MEDIA = "1.3.6.1.2.1.43.8.2.1.12"
TRAY_DESC = "1.3.6.1.2.1.43.8.2.1.18"
TRAY_LEVEL = "1.3.6.1.2.1.43.8.2.1.10"
TRAY_MAX = "1.3.6.1.2.1.43.8.2.1.9"

STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warming up"}

COVER = {1: "other", 2: "unknown", 3: "open", 4: "closed",
         5: "interlock open", 6: "interlock closed"}
ALERT_LEVEL = {1: "other", 3: "critical", 4: "warning", 5: "warning"}

# Paper sizes in mm, matched to whichever is within a few mm. The MIB gives
# two dimensions and no name, so "420 x 297" has to become "A3" somewhere.
PAPER = [("A3", 297, 420), ("A4", 210, 297), ("A5", 148, 210), ("A6", 105, 148),
         ("B4", 250, 353), ("B5", 176, 250), ("Letter", 216, 279),
         ("Legal", 216, 356), ("Executive", 184, 267)]


def paper_name(feed, cross, unit):
    """prtInputDimUnit: 3 = ten-thousandths of an inch, 4 = micrometres."""
    if not isinstance(feed, int) or not isinstance(cross, int):
        return None
    if feed <= 0 or cross <= 0:
        return None
    if unit == 3:
        mm = (feed / 10000 * 25.4, cross / 10000 * 25.4)
    else:
        mm = (feed / 1000, cross / 1000)
    short, long_ = min(mm), max(mm)
    for name, s, l in PAPER:
        if abs(short - s) <= 4 and abs(long_ - l) <= 4:
            return name
    return f"{round(short)}x{round(long_)}mm"

SUPPLY_TYPE = {
    1: "other", 2: "unknown", 3: "toner", 4: "waste toner", 5: "ink",
    6: "ink cartridge", 7: "ink ribbon", 8: "waste ink", 9: "drum",
    10: "developer", 11: "fuser oil", 12: "solid wax", 13: "ribbon wax",
    14: "waste wax", 15: "fuser", 16: "corona wire", 17: "fuser oil wick",
    18: "cleaner", 19: "fuser cleaning pad", 20: "transfer unit",
    21: "toner cartridge", 22: "fuser oiler", 23: "water", 24: "waste water",
    25: "glue water additive", 26: "waste transfer unit", 27: "waste paper",
    28: "print cleaner", 29: "staples",
}

# hrPrinterDetectedErrorState, bit 0 is the top bit of the first byte
ERROR_BITS = [
    "low paper", "no paper", "low toner", "no toner", "door open", "jammed",
    "offline", "service requested", "input tray missing", "output tray missing",
    "marker supply missing", "output near full", "output full",
    "input tray empty", "overdue maintenance",
]

# Consumables that fill up rather than run down: a HIGH reading is the problem.
WASTE = {"waste toner", "waste ink", "waste wax", "waste water",
         "waste transfer unit", "waste paper"}


def decode_errors(raw):
    if not raw:
        return []
    data = raw.encode("latin-1", "replace") if isinstance(raw, str) else raw
    out = []
    for i, label in enumerate(ERROR_BITS):
        byte, bit = divmod(i, 8)
        if byte < len(data) and data[byte] & (0x80 >> bit):
            out.append(label)
    return out


def _pct(level, maximum, kind):
    """Percentage remaining, or None when the device will not say.

    A waste container reports how FULL it is, so the useful number for a
    human is inverted — "waste box 90%" should read as nearly full, not
    nearly fine.
    """
    if not isinstance(level, int) or not isinstance(maximum, int):
        return None
    if level < 0 or maximum <= 0:
        return None
    pct = round(100.0 * level / maximum, 1)
    return round(100.0 - pct, 1) if kind in WASTE else pct


def _index(oid, base):
    return oid[len(base) + 1:]


def read(host, community="public", timeout=3.0):
    """Everything worth knowing about one printer. Returns (data, error)."""
    try:
        base = snmp.get(host, [SYS_DESCR, SYS_NAME, HR_DEVICE, PRT_NAME,
                              PRT_STATUS, PRT_ERRORS, PAGE_COUNT, SYS_UPTIME,
                              SERIAL, SYS_LOCATION, SYS_CONTACT],
                        community, timeout)
    except Exception as e:
        return None, f"no SNMP answer: {e}"
    if not base.get(SYS_DESCR) and not base.get(HR_DEVICE):
        return None, "answered, but reported no device description"

    descs = snmp.walk(host, SUP_DESC, community, timeout)
    levels = dict(snmp.walk(host, SUP_LEVEL, community, timeout))
    maxes = dict(snmp.walk(host, SUP_MAX, community, timeout))
    types = dict(snmp.walk(host, SUP_TYPE, community, timeout))

    supplies = []
    for oid, name in descs:
        idx = _index(oid, SUP_DESC)
        kind = SUPPLY_TYPE.get(types.get(f"{SUP_TYPE}.{idx}"), "other")
        level = levels.get(f"{SUP_LEVEL}.{idx}")
        maximum = maxes.get(f"{SUP_MAX}.{idx}")
        supplies.append({
            "name": name or kind.title(),
            "kind": kind,
            "level": level,
            "max": maximum,
            "percent": _pct(level, maximum, kind),
            # -3 is the device saying "enough, but I will not count it"
            "unmeasured": level == -3,
        })

    # The console text is what the panel is showing right now, which saves
    # walking across the office to find out what it is complaining about.
    console = " ".join(str(v).strip() for _, v in
                       snmp.walk(host, CONSOLE, community, timeout)
                       if str(v).strip())

    cover_state = dict(snmp.walk(host, COVER_STATUS, community, timeout))
    covers = []
    for oid, name in snmp.walk(host, COVER_DESC, community, timeout):
        idx = _index(oid, COVER_DESC)
        code = cover_state.get(f"{COVER_STATUS}.{idx}")
        covers.append({"name": name or f"Cover {idx}",
                       "state": COVER.get(code, "unknown"),
                       "open": code in (3, 5)})

    sev = dict(snmp.walk(host, ALERT_SEVERITY, community, timeout))
    alerts = []
    for oid, text in snmp.walk(host, ALERT_DESC, community, timeout):
        idx = _index(oid, ALERT_DESC)
        if not str(text).strip():
            continue
        alerts.append({"text": str(text).strip(),
                       "level": ALERT_LEVEL.get(sev.get(f"{ALERT_SEVERITY}.{idx}"),
                                                "other")})

    colorants = [str(v) for _, v in snmp.walk(host, COLORANTS, community, timeout)
                 if str(v).strip()]

    # Walk each column once, as with supplies. Walking a leaf OID per tray
    # returns nothing, because the leaf has no children to step into.
    tray_levels = dict(snmp.walk(host, TRAY_LEVEL, community, timeout))
    tray_maxes = dict(snmp.walk(host, TRAY_MAX, community, timeout))
    tray_caps = dict(snmp.walk(host, TRAY_CAPACITY, community, timeout))
    tray_units = dict(snmp.walk(host, TRAY_DIM_UNIT, community, timeout))
    tray_feed = dict(snmp.walk(host, TRAY_DIM_FEED, community, timeout))
    tray_cross = dict(snmp.walk(host, TRAY_DIM_CROSS, community, timeout))
    tray_media = dict(snmp.walk(host, TRAY_MEDIA, community, timeout))
    trays = []
    for oid, name in snmp.walk(host, TRAY_DESC, community, timeout):
        idx = _index(oid, TRAY_DESC)
        level = tray_levels.get(f"{TRAY_LEVEL}.{idx}")
        maximum = tray_maxes.get(f"{TRAY_MAX}.{idx}")
        media = str(tray_media.get(f"{TRAY_MEDIA}.{idx}") or "").strip()
        trays.append({
            "name": name or f"Tray {idx}",
            "level": level,
            "max": maximum,
            "percent": _pct(level, maximum, "tray"),
            # -3 again: "has paper, not counting sheets"
            "unmeasured": level == -3,
            "capacity": tray_caps.get(f"{TRAY_CAPACITY}.{idx}"),
            "paper": paper_name(tray_feed.get(f"{TRAY_DIM_FEED}.{idx}"),
                                tray_cross.get(f"{TRAY_DIM_CROSS}.{idx}"),
                                tray_units.get(f"{TRAY_DIM_UNIT}.{idx}")),
            "media": media if media and media != "-" else None,
        })

    return {
        "host": host,
        "name": base.get(PRT_NAME) or base.get(SYS_NAME) or host,
        "model": base.get(HR_DEVICE) or base.get(SYS_DESCR) or "",
        "descr": base.get(SYS_DESCR) or "",
        "status": STATUS.get(base.get(PRT_STATUS), "unknown"),
        "errors": decode_errors(base.get(PRT_ERRORS)),
        "pages": base.get(PAGE_COUNT),
        "uptime_s": (base.get(SYS_UPTIME) or 0) // 100,
        "serial": base.get(SERIAL) or "",
        # The device often knows where it is; better than asking a human to
        # retype it into a config file and keep it current.
        "location": (base.get(SYS_LOCATION) or "").strip(),
        "contact": (base.get(SYS_CONTACT) or "").strip(),
        "console": console,
        "covers": covers,
        "alerts": alerts,
        "colorants": colorants,
        "supplies": supplies,
        "trays": trays,
    }, None


def worst(data, warn=20, bad=10):
    """The state of the most depleted measurable supply.

    Only measurable ones count. A Brother that reports -3 forever must not
    sit permanently amber, or the page becomes something nobody reads.
    """
    if data.get("errors"):
        return "bad", "; ".join(data["errors"])
    shut = [c["name"] for c in data.get("covers", []) if c["open"]]
    if shut:
        return "bad", "open: " + ", ".join(shut)
    low = [s for s in data.get("supplies", []) if s["percent"] is not None]
    if not low:
        return "ok", "no measurable supplies reported"
    s = min(low, key=lambda x: x["percent"])
    if s["percent"] <= bad:
        return "bad", f"{s['name']} at {s['percent']}%"
    if s["percent"] <= warn:
        return "warn", f"{s['name']} at {s['percent']}%"
    return "ok", f"lowest {s['name']} at {s['percent']}%"
