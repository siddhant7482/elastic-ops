+++
title    = "Elastic Ops"
kind     = "app"
shelf    = "APP.OPS"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"
+++

## What it is

The estate console, and the thing this catalogue is built inside. Python 3.11,
**standard library only**, one SQLite file, one process on [[HST.PEP]] at
`10.10.1.70:8080`.

It watches service checks, job check-ins, host metrics, backups, disk and
array integrity, printers and certificates, and it is where incidents and
changes are recorded.

## Level 1 — system context

```flow
title: Level 1 - system context
5 collectors | on each hypervisor
-> push metrics, 10 min
Elastic Ops | one process, pepper | accent
-> probes, SNMP
the estate | hosts, VMs, printers
=> email / matrix / whatsapp | notification channels
=> dead-man switch | external, outside the estate
```

> [!note] The collector arrow points **outward from the estate**. The portal
> holds no hypervisor credentials and has no route into any management
> interface — each host pushes a small report in with a shared token.
> Compromising the portal yields a read of the estate's shape, not a way into
> it. That direction was chosen for this reason and should not be reversed
> for convenience.

## Level 2 — containers

```flow
title: Level 2 - containers
pool.py | 8 workers, backpressure
-> dispatch
api.py | routes and the three gates | accent
-> the loop
engine.py | checks, sweeps, notifier
-> every SQL statement
store.py | ops.db, 23 tables, WAL | accent
=> estate-docs | git working tree, this catalogue
```

`ReadWritePaths=/var/lib/elastic-ops` in the unit file is the only writable
path. That is why the document tree lives there and **not** under
`/opt/elastic-ops`, which a deploy overwrites.

## Level 3 — the check loop

The main thread wakes every five seconds and does, in order:

1. Run any check whose interval has elapsed
2. Refresh certificates not looked at for six hours
3. Sweep jobs for missed check-ins
4. Sweep storage — arrays and disks
5. Sweep the catalogue — stale and undocumented records
6. Poll printers (every 300s; full read hourly, light otherwise)
7. Ping the dead-man switch (60s)
8. Prune old rows (daily)

Each step is wrapped so a raise is logged and the loop continues. A check that
throws must not take the other eleven down with it.

## The thing that bites: concurrency

| Workers | Throughput | Request p99 | Check-loop wait p99 |
|---|---|---|---|
| 4 | 874/s | 261 ms | 6.9 ms |
| **8** | **884/s** | **239 ms** | **6.6 ms** |
| 16 | 896/s | 240 ms | 18.2 ms |
| 32 | 884/s | 253 ms | 44.7 ms |

> [!danger] **Do not raise `http_workers` to make it faster.** Every query
> funnels through one SQLite connection behind one mutex, and the check loop
> takes that same mutex. Extra workers buy no throughput and make checks run
> late.

The accept loop **blocks rather than refusing**: an unaccepted connection waits
in the kernel backlog for free. Refusing eagerly turned 57% of a 200-client
burst into 503s the kernel would have absorbed; with backpressure it is 0%.

## Silence is the alert

Jobs do not report success — they report *having run*. A job that stops calling
in is reported as late.

> [!warn] That only works while the check-in tokens are unguessable. They were
> once the job names (`/checkin/sql-dumps`), which meant anyone could fake a
> healthy backup — and the portal would have gone on saying the backups were
> fine while they had stopped. Fixed 2026-11-04; rotated again 2026-11-09
> after the values were read out to a terminal.

## Why it has no dependencies

Enforced by `tests/hygiene.py`, which fails the build on any import outside
the allow-list. There is no dependency tree to audit, nothing to pin, no
post-install script. The attack surface that eats most small services — a
compromised package three levels down — is simply absent.

Two names are deliberate: `mailer.py` is not `email.py`, because that would
shadow the standard library package it needs; and `snmp.py` implements **no
SET operation at all**, so there is no code path by which the monitoring can
reconfigure a device it monitors.

## If it dies

The console and this catalogue go together — both run on pepper. The offline
manual in `estate-docs/dist/` is the fallback, which is why that file is
committed and pushed rather than only written to disk.

Restore order is in [[RUN.PEP]]: **proxmox first, always**, then pepper.
