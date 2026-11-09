+++
title    = "CFP machines cannot print"
kind     = "runbook"
shelf    = "RUN.PRN"
owner    = "siddhant"
verified = "2026-11-04"
review   = "12 months"

[bind]
records = ["DEV.KON"]
+++

## What it is not

Not a network fault. The UDM routes between the subnets, and `tracert` proves
it.

> [!note] The first diagnosis was that there was no path between the CFP
> machines and the printers. That was wrong, and it was wrong because it was
> reasoned from the addresses rather than tested. Test the path before
> concluding there isn't one.

## What it actually was

Stale print queues pointing at DHCP addresses that had moved:

- `.63` now answers as a phone
- `.196` is nothing at all
- the Brother was on a **WSD** port, and WSD is multicast, so it cannot cross
  subnets under any configuration

## Steps

1. `Get-PrinterPort` on the affected machine to see the real target
2. Compare against the device's current address
3. Delete the stale port; add a **Standard TCP/IP port on 9100**
4. Print a test page before telling anyone it is fixed

## Verification

Resolved on CFP 201 and 202 on **2026-11-04**. Machines 203 to 206 are
deliberately reactive only — they are fixed if someone reports a problem, not
pre-emptively.

## Still outstanding

DHCP reservations for `.161`, `.177` and `.203` on the UDM. Until those exist
this recurs on its own schedule.
