+++
title    = "Konica Minolta bizhub C368"
kind     = "device"
shelf    = "DEV.KON"
owner    = "siddhant"
verified = "2026-11-04"
review   = "12 months"

[bind]
printer = "192.168.42.177"
+++

## What it is

The ground-floor Konica Minolta bizhub C368. The only Konica in the office, so
"the Konica" is unambiguous.

## Why it disappears in the evening

ErP deep sleep powers the network card down. The device then answers nothing at
all, including ARP.

> [!danger] A queued print job **cannot wake it**. ARP never resolves, so the
> job never leaves the spooler and the user sees a queue that does nothing.
> This is not a fault to chase — it is the setting working as designed.

If it has to stay reachable overnight, turn ErP off or enable Network Wake-up on
the device itself.

## Outstanding

No DHCP reservation. A lease change silently breaks every queue pointing at it —
see [[RUN.PRN]].
