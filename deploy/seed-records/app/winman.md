+++
title    = "CFP / WinMan"
kind     = "app"
shelf    = "APP.WIN"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"

[bind]
app = "CFP / WinMan"
+++

## What it is

The ERP. Invoicing, stock, purchase orders, and the ledger the business is run
from. If WinMan is down, nobody can invoice, and that is the shortest sentence
anyone needs to understand about this estate.

It is a Windows desktop client talking directly to SQL Server. There is no
application server and no web tier — every workstation holds a connection to
the database itself.

> [!danger] **This is the single highest-impact application in the estate.**
> It has no redundancy at any layer: one database instance, on one guest, on
> one host, whose production array is a RAID 0. Every mitigation is a backup,
> not a failover.

## Level 1 — who uses it, and what it touches

```flow
title: Level 1 - who uses it and what it touches
Finance / ops | six workstations
-> WinMan client, TCP 1433
CFP / WinMan | SQL Express + databases | accent
=> ArrearsMan | reads WinMan directly
=> Printed invoices | PDFs
```

ArrearsMan reads WinMan directly — see [[APP.ARM]]. That coupling is the
reason a WinMan outage is never only a WinMan outage.

## Level 2 — where it runs

```flow
title: Level 2 - where it runs
cinnamon | the host
-> carries
GUEST: CFP | Windows + SQL Express | accent
-> TCP 1433, dynamic port
six workstations | on proxmox
=> SQL Browser | UDP 1434, must be on | warn
=> PBS backup | nightly
```

## Level 3 — the parts that break

```flow
title: Level 3 - the chain that breaks
WinMan client | on a workstation
-> asks: which port?
SQL Browser | UDP 1434 | warn
-> replies: port N
SQL Server | TCP N | accent
-> opens
WinMan databases | live + archive
```

## The thing that bites: invoice numbering

`Invoice.InvRef` is **eight characters**. WinMan fills it with
`LEFT(InvNo, 8)`, so the printed reference silently wraps long before the real
invoice number does.

> [!danger] If printed invoices start repeating a number, **this is the cause —
> it is not a printer fault and not a user error.** It repeated every 100
> invoices and produced 1,895 historic duplicates before it was found.

Fixed on **2026-09-22** by resetting `DEFAULTS` and `Host_Defaults` to
`INV01600`. The next wall is around **December 2027**. The 1,895 historic
duplicates were deliberately parked rather than rewritten — rewriting issued
invoice references is worse than carrying a known, documented collision.

## Known history

| Date | What happened | Resolution |
|---|---|---|
| 2026-09-15 | Instance hung — `AUTO_CLOSE` thrash on the WinMan databases | Disabled `AUTO_CLOSE`; resized to 4 cores / 16 GB on 2026-09-17 |
| 2026-09-22 | Duplicate invoice numbers on printed PDFs | `LEFT(InvNo,8)` truncation; counters reset |
| 2026-09-16 | Clients could not connect after a restart | SQL Browser was off; named instances take a dynamic port |

> [!note] Every one of those was invisible from inside WinMan. They were found
> by looking at the database. The application will not tell you it is wrong.

## How it fails, and what to do

**One client cannot connect, others are fine.** A stale or corrupt encrypted
password in `C:\Windows\Winman32.ini`. Copy the file from a working machine
byte-for-byte — do not retype it, the value is encrypted and a hand-typed line
will look right and fail. See [[RUN.WMC]].

**Nobody can connect after a reboot.** SQL Browser. A named instance gets a
dynamic port that moves when the service restarts, so pinning the number means
the check goes red for a reason nobody can act on. Set Browser to Automatic,
or give the instance a static port and connect by `host,port`.

**The instance is up but unresponsive.** Check `AUTO_CLOSE` is still off. A
restore can bring the old database options back with it.

## If it dies

[[RUN.CFP]] is the procedure, and it is **the one runbook in this catalogue
that has been exercised against a real failure**. Restore target is cinnamon
if it has capacity, otherwise yam.

> [!warn] Step four of that runbook matters more than it looks: after a
> restore, confirm `AUTO_CLOSE` is still disabled before telling anyone the
> system is back.

## Where its credentials live

See [[REG.CRED]]. The monitoring holds none of them — the SQL check resolves
the instance through SQL Browser and opens a TCP connection, but never logs
in, so there is no WinMan credential anywhere in Elastic Ops to steal.
