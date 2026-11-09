+++
title    = "Zabbix"
kind     = "app"
shelf    = "APP.ZBX"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"

[bind]
app = "Zabbix"
+++

## What it is

The older monitoring system, running on [[HST.NET]].

## How it relates to Elastic Ops

```flow
title: Two monitoring systems, no decision
Zabbix | agent-based, on networkserver | warn
=> general metrics | overlapping scope
Elastic Ops | agentless + pushed, on pepper | accent
=> backups, arrays, check-ins | estate-specific
```

> [!warn] **Two monitoring systems is a decision that has not been made.**
> They overlap, neither is authoritative, and an alert from one does not
> appear in the other. Either Zabbix is retired or it is given a clearly
> separate job. Leaving both running means every incident starts with
> "which one is right?".

## Where it sits

It runs on the box the knowledge base described as dead — see [[HST.NET]].
That alone is a reason to treat anything Zabbix reports with care until the
host itself is properly documented and verified.

## If it dies

No restore procedure is written. Given the open question above, writing one
should wait until it is decided whether Zabbix has a future here.
