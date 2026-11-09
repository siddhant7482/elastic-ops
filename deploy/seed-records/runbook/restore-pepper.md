+++
title    = "Restore pepper"
kind     = "runbook"
shelf    = "RUN.PEP"
owner    = "siddhant"
verified = "2026-11-09"
review   = "6 months"

[bind]
records = ["HST.PEP", "APP.OPS", "APP.VWD"]
+++

## When to use this

pepper is unreachable or has been restored from backup. It carries the
knowledge base, Vaultwarden, Messenger and [[APP.OPS]] — so when pepper is
down, **the monitoring is down and this catalogue is down with it**.

> [!danger] You will not be able to read this page while you need it. The
> offline copy is `estate-docs/dist/estate-manual.html`, which is committed to
> the document repository and opens on a phone with no network. If you are
> reading this during an incident, you are reading the wrong copy.

## Order matters

```flow
title: Restore order - this is not negotiable
proxmox | ALWAYS FIRST - the business | warn
-> then
cinnamon | CFP / WinMan, invoicing
-> then
pepper | console, vault, this catalogue | accent
-> finally
confirm collectors | five hosts posting again
```

> [!warn] **Do not restore pepper first**, however tempting it is to get the
> dashboard back. Bringing the console up before the estate means the
> collectors post into an empty database and the first sweep reports
> everything as missing — you will spend the next hour reading alerts about
> an outage you already know about.

## Steps

1. Confirm proxmox and cinnamon are up and the business can work. Nothing
   below is more urgent than that.
2. Restore the pepper guest from the most recent **verified** PBS snapshot —
   verified, not merely present. See [[RUN.PBS]].
3. Start it and confirm `elastic-ops.service` is active:
   `systemctl status elastic-ops`
4. Check the build actually came back: `curl -s http://10.10.1.70:8080/health`
   and confirm the `build` stamp is the one you expect.
5. Confirm the collectors are posting — five hosts should check in within ten
   minutes. Anything still silent after that has a token problem, not a
   network problem.
6. Confirm the document tree is present and is still a git repository:
   `git -C /var/lib/elastic-ops/estate-docs status`

## What breaks after a restore, specifically

**The document tree ownership.** If the restore or any manual step runs as
root, `.git` ends up owned by root while the service runs as `elasticops`, and
git refuses on ownership. The symptom is a read-only catalogue with
"dubious ownership" in the log. Fix:

```
chown -R elasticops:elasticops /var/lib/elastic-ops/estate-docs
```

**git's home directory.** `ProtectHome=true` leaves git nowhere to read config
from. The drop-in at
`/etc/systemd/system/elastic-ops.service.d/home.conf` sets
`HOME=/var/lib/elastic-ops` and must survive the restore.

**Check-in tokens.** If `checks.toml` is restored from an older deploy, the
job tokens may not match what the collector hosts hold. Every job will go late
on its own schedule — the nightly backup will not complain until the following
morning. Run `deploy/set-checkin-tokens.py` rather than waiting to find out.

## Verification

Not "the page loads". The restore is done when:

- `/health` reports the expected build
- all five collectors have checked in
- the catalogue is writable, not read-only
- a test incident can be created and deleted

## Last exercised

**Never.** This procedure has not been run against a real failure or as a
drill.

> [!danger] A restore procedure nobody has executed is a hypothesis. This
> record exists so that the gap is visible rather than assumed away — and it
> is why Elastic Ops raises an unverified runbook as a paging condition.
