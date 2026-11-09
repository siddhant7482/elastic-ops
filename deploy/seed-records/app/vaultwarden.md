+++
title    = "Vaultwarden"
kind     = "app"
shelf    = "APP.VWD"
owner    = "maria"
verified = "2026-11-09"
review   = "12 months"

[bind]
app = "Vaultwarden"
+++

## What it is

The password vault for the team. A self-hosted Bitwarden-compatible server, so
the official Bitwarden clients and browser extensions work against it
unchanged.

It runs on [[HST.PEP]].

## Level 1 — who depends on it

```flow
title: Who depends on it
Clients | browser, desktop, mobile
-> sync
Vaultwarden | on pepper | accent
-> stores
SQLite + attachments | backed up to PBS | warn
```

> [!danger] **This is the estate's single point of credential failure.** Lose
> it without a restore and you lose access to everything stored in it at once.
> It is backed up, and the backup has never been exercised as a drill — that
> gap is the thing worth fixing about this record.

## The thing people get wrong

A new user sees an empty vault and reports that it is broken. It is not: an
invitation only creates the account. **An existing member has to share a
collection with them** before they see anything at all.

See [[RUN.VWU]] for adding and removing people.

## Offboarding

> [!warn] Removing someone from Vaultwarden does **not** change any password
> they have already seen. Deprovisioning a person means rotating what they had
> access to, not just revoking the account. [[GOV.ONB]] covers the order.

## If it dies

Restore the guest from PBS. The vault database is small, so the restore is
fast; the risk is not duration but the fact that nobody has rehearsed it.

Emergency access while it is down: the master passwords are not recoverable
from anywhere else. That is the point of the product and also the risk.
