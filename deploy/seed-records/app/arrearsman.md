+++
title    = "ArrearsMan"
kind     = "app"
shelf    = "APP.ARM"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"

[bind]
app = "ArrearsMan"
+++

## What it is

Arrears chasing and collections. A Microsoft Access front end — a `.accdb`
file deployed **per PC**, not shared — talking to two separate SQL backends at
the same time.

That shape is the whole story of this application. Almost every "ArrearsMan is
broken" report is one of the two backends, or one machine's copy of the file,
and not the application at all.

## Level 1 — two backends, one front end

```flow
title: Level 1 - two backends, one front end
Collections staff | per-PC .accdb | accent
=> arrears database | 10.10.1.35
=> WinMan | cfp-server\cfp | warn
```

> [!warn] **ArrearsMan depends on WinMan.** A WinMan outage takes collections
> with it, and the symptom reported will be "ArrearsMan is down". Check
> [[APP.WIN]] before touching anything here.

## Level 2 — where the pieces live

```flow
title: Level 2 - where the pieces live
proxmox | RAID 0, no redundancy | warn
-> carries
arrears backend | 10.10.1.35 | accent
-> TCP 1433 + UDP 1434
cinnamon | GUEST: CFP
=> six workstations | each holds its own .accdb
```

> [!danger] The workstations and the arrears backend are all on **proxmox**,
> whose production storage is a **RAID 0**. There is no array redundancy under
> collections at all. The only thing between this and a very bad week is the
> nightly backup relay to cinnamon.

## Level 3 — why a per-PC file is the design flaw

```flow
title: Level 3 - why a per-PC file is the flaw
PC 1 | its own .accdb
-> each holds its own connection string
PC 2 | possibly different | warn
-> so a fix on one machine
PC 3 | possibly stale | warn
=> fixes exactly one machine | deploy a clean master instead
```

**Deploy a clean master to each PC.** Do not patch them in place — that is how
six machines end up with six different connection strings and a fault that
only reproduces on one desk.

## Known history

| Date | What happened | Resolution |
|---|---|---|
| 2026-09-16 | Clients could not reach the backends after a restart | SQL Browser was disabled on the instance; enabled it |
| 2026-09-16 | One machine still failing after the above | `epson1260` typo in the connection string — a printer name had been pasted into a server field |

> [!note] The `epson1260` typo is worth remembering as a class of fault: the
> connection string is inside the file, it is edited by hand, and a wrong value
> fails in a way that looks like a server problem rather than a text problem.

## How it fails, and what to do

**Nobody can connect.** Check both backends before anything else. Is
`10.10.1.35` up? Is `cfp-server\cfp` up? Is SQL Browser running on the named
instance? See [[RUN.ALF]].

**One desk fails, the rest are fine.** Its `.accdb` is stale or its connection
string is wrong. Deploy the clean master rather than editing it.

**Everything is slow.** Access over the network pulls more than people expect.
Check whether the arrears backend guest is contending on proxmox.

## If it dies

[[RUN.ARM]] has the restore. Restore target for proxmox guests is yam, roughly
two hours.

> [!warn] Restoring the arrears backend is not enough on its own. The six
> workstations each need their `.accdb` checked afterwards, because a restore
> that changes an address leaves every linked table pointing at the old one.

## Where its credentials live

See [[REG.CRED]]. The connection strings live inside each `.accdb`, which is
its own problem: they are not centrally held, not rotatable in one place, and
not recorded anywhere but the files themselves.
