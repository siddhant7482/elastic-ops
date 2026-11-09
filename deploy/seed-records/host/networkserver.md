+++
title    = "networkserver"
kind     = "host"
shelf    = "HST.NET"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"

[bind]
host    = "networkserver"
guests  = true
backups = true
+++

## What it is

The fifth hypervisor, and the one the written documentation had wrong. It runs
HikCentral for CCTV, FusionPBX for the phones, and Zabbix.

> [!warn] The knowledge base described this box as **down, at `10.10.1.40`,
> needing investigation**. Both halves were wrong. It is at `10.10.1.30` and it
> is running — its collector checks in on schedule. A page that says a live
> hypervisor is dead is worse than no page, because nobody goes looking at a
> machine they have been told is already off.

## How it is built

A Dell OptiPlex 3070 with 15 GB of non-ECC memory and a single NVMe drive.

> [!danger] **No redundancy of any kind.** One disk, no ECC, no second power
> supply. CCTV and the phone system both sit on it. There is no array to
> degrade — the first disk failure is the whole service.

## If it dies

CCTV management and the phones go with it. There is no documented restore for
this host yet, which is the gap worth closing before anything else here.

## Known history

**2026-10-04** — froze for roughly 36 hours. Not diagnosed to a root cause. It
has not been established whether that was heat, the disk, or memory, and on a
box with no ECC the third is not something that announces itself.

## Where its credentials live

Not written yet.
