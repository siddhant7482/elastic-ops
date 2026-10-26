# Elastic Ops

An operations console for the Elastic Group estate: what is up, what has a
backup, what changed, what broke, and what is about to run out of disk.

It replaces nothing clever. It exists because on 4 October 2026 networkserver
froze for 36 hours and nothing reported it — the monitoring was running on the
host that died — and because the SQL dumps stopped on 9 July and nobody noticed
for 105 days, since nothing was *down*, the work had simply stopped.

So two ideas run through the whole thing:

- **Silence is the alert.** Scheduled jobs call in when they finish. A job that
  stops is reported, even though every endpoint it touches is still answering.
- **Something outside has to watch the watchman.** The checker pings an
  external dead-man's switch every minute. Nothing self-hosted can tell you it
  has died.

---

## Running it

Everything is Python 3.11+ standard library. No pip install, because the thing
that watches the estate should not itself be a dependency problem.

```bash
cd ops-portal
docker compose up -d
docker compose logs -f
```

Then create the first account — there is no sign-up, and no password reset:

```bash
docker compose exec ops python3 app.py /app/checks.toml --adduser siddhant --display "Siddhant"
```

Open <http://10.10.1.70:8080>. Other account commands:

```bash
... --users               # list
... --passwd <name>       # change a password
... --deluser <name>      # remove, and kill their sessions
```

### Before it is useful

Three settings start empty and each one disables something real. They are
**secrets, so they live in `.env`, not in the repository** — `checks.toml`
carries empty placeholders and the environment overrides them.

| `.env` variable | Without it |
|---|---|
| `OPS_INGEST_TOKEN` | No backup state, no host memory, no pool capacity |
| `OPS_DEADMAN_URL` | Nothing tells you when the checker itself dies |
| `OPS_WEBHOOK_URL` | Alerts are logged but never sent to WhatsApp |

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # the ingest token
chmod 600 .env
```

`.env` is gitignored, and CI fails if a real value ever appears in
`checks.toml`.

---

## The pieces

```
checker/
  app.py          entry point and CLI
  engine.py       config, maintenance windows, notifications, the check loop
  probes.py       tcp / http / https / ping
  store.py        SQLite — check state, incidents, changes, ingested metrics
  auth.py         scrypt passwords, server-side sessions
  api.py          static files, login, the JSON API, ingest
  checks.toml     what to check, which jobs must call in, settings
  inventory.toml  hosts, drives, applications, credentials, routing, blast radius
  web/            the portal itself
collector/
  post_estate.py  runs on each hypervisor, posts backup state and metrics in
tests/
  smoke.py        starts the real server and walks through everything
```

### Why the data is split three ways

**Live** — checks, jobs, history. The checker owns it; nothing else writes it.

**Records people create** — incidents, follow-ups, changes. SQLite, written
through the API by whoever is signed in.

**Inventory that changes a few times a year** — hosts, drives, applications,
credentials, alert routing, blast radius. These live in `inventory.toml`, not
in the database. They belong in version control, and screens to edit them would
be more code to maintain than the data is worth. Edit the file, restart the
container.

### Why the hosts push instead of the portal pulling

The portal has no credentials for any hypervisor and no route to any management
interface. Each host runs `post_estate.py` and posts a small report outward with
a shared token. One endpoint, one secret, nothing to steal from the portal if it
is ever compromised.

---

## Installing the collector

On each hypervisor — cinnamon, proxmox, yam, pepper, networkserver:

```bash
install -D -m 755 post_estate.py /opt/estate/post_estate.py

cat > /etc/default/estate-collector <<'EOF'
OPS_URL=http://10.10.1.70:8080
OPS_TOKEN=<the ingest_token from checks.toml>
OPS_CHECKIN=backup-collector
EOF
chmod 600 /etc/default/estate-collector

crontab -e
  5 6 * * *     /opt/estate/post_estate.py >> /var/log/estate-collector.log 2>&1
  */10 * * * *  /opt/estate/post_estate.py --metrics-only > /dev/null 2>&1
```

Check it first with `--dry-run`, which prints the payload instead of sending it.

The daily run walks every guest on the host and reports the age of its newest
backup — including the guests with **no** backup at all, which is the whole
point of the backups page. The ten-minute run only reads memory and `pvesm
status`, which is cheap.

---

## Making a job report in

Add one line to the end of any script or cron entry:

```bash
curl -fsS -m 10 http://10.10.1.70:8080/checkin/nightly-backup || true
```

The `|| true` matters: a check-in that fails must never fail the backup.

Then add the job to `checks.toml`:

```toml
[[jobs]]
name = "Nightly machine backup"
token = "nightly-backup"
grace_hours = 26          # late after this, and it alerts
severity = "page"
```

If it does not call in within `grace_hours`, it goes down and you are told.
That is the mechanism that would have caught July.

---

## Adding a check

```toml
[[checks]]
name = "Bigcapital"
group = "Business"
type = "http"             # tcp | http | https | ping
target = "http://10.10.1.102:3000/"
retries = 3               # consecutive failures before it alerts
severity = "notify"       # page | notify | quiet
```

`https` skips certificate verification, because Proxmox and PBS serve
self-signed certs on 8006/8007 and verifying them would mean every hypervisor
check failing for a reason nobody can fix.

Delete a check and its state disappears from the portal on restart, so nothing
lingers that nobody is measuring.

---

## Alerting rules

- Alerts fire on the **Nth consecutive failure**, never the first. One dropped
  packet should not wake anyone.
- **Recovery is always announced.** Being told something broke and never that it
  came back is how people stop trusting a channel.
- **Maintenance windows suppress alerts but still record results**, so the
  02:00–04:00 guest-agent timeouts during backups don't train you to ignore the
  channel.
- Keep `severity = "page"` to about five things. That list staying short is the
  only reason the channel stays worth reading.

---

## Security notes

Login is built in rather than delegated to Authelia: three people on a LAN did
not justify a second container and a Redis. What that means in practice:

- Passwords are hashed with `hashlib.scrypt`, n=2¹⁵, per-user random salt.
  (OpenSSL's default memory ceiling is exactly what those parameters need, so
  `maxmem` is set explicitly — without it every login throws.)
- Sessions are random tokens stored server-side. Deleting a user or signing out
  ends the session immediately, which a self-contained signed token could not do.
- The cookie is `HttpOnly` and `SameSite=Lax`, and every state-changing API
  route requires a JSON content type — together that closes CSRF.
- Failed logins back off per client address.
- **`cookie_secure` must stay `false` until the portal is behind HTTPS.** A
  `Secure` cookie over plain http is simply never sent, and login then fails
  with no error anywhere.

The portal stores no secrets. The credentials page records only where each one
lives and how old it is; the point of that table is the age column.

---

## Backing it up

Everything that matters is the `ops-data` volume — the database holds accounts,
sessions, incidents, follow-ups, changes and all history. The application itself
is mounted read-only and is in version control.

```bash
docker compose exec ops python3 -c \
  "import sqlite3,sys; sqlite3.connect('/data/ops.db').backup(sqlite3.connect('/data/ops-backup.db'))"
```

Then include VM 9503 on pepper in the nightly job like any other machine.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it protects |
|---|---|
| **Repository guards** | The runtime imports nothing outside the standard library, and no secret has been committed |
| **Smoke tests** | 94 assertions against a real server, on Python 3.11, 3.12 and 3.13 |
| **Compose config** | `docker-compose.yml` still parses |
| **Lint** | ruff |

The guards job is the one that earns its keep. A stray `import requests` would
not fail until the day the collector runs on a host without it, and a token
pasted into `checks.toml` would be public the moment it was pushed. Both are
caught in seconds instead.

## Continuous deployment

GitHub's hosted runners cannot reach 10.10.1.70 — it is on the LAN with no
inbound access, and opening a hole for a deploy would be a worse trade than any
convenience it buys. So **a self-hosted runner lives on pepper and connects
outward to GitHub**. Nothing is exposed.

On pepper, once:

```bash
# the deploy writes here; the runner needs to own it
sudo mkdir -p /opt/elastic-ops/backups
sudo cp .env.example /opt/elastic-ops/.env   # then fill it in
sudo chmod 600 /opt/elastic-ops/.env

# the runner itself — token from Settings → Actions → Runners → New runner
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o r.tar.gz -L https://github.com/actions/runner/releases/download/v2.319.1/actions-runner-linux-x64-2.319.1.tar.gz
tar xzf r.tar.gz
./config.sh --url https://github.com/<you>/elastic-ops --token <TOKEN> \
            --name pepper --labels pepper --unattended
sudo ./svc.sh install && sudo ./svc.sh start
```

The runner's user needs to be in the `docker` group.

`deploy.yml` then fires when CI passes on `main`, and it:

1. refuses to run if CI did not succeed,
2. refuses to run if `/opt/elastic-ops/.env` is missing,
3. **snapshots the database before touching anything**, keeping the last 14,
4. brings the stack up and polls `/health` for 30s,
5. dumps the last 60 log lines and fails loudly if it never answers.

You can also run it by hand from the Actions tab, against any branch.

The container bind-mounts the checkout rather than baking an image. For a
stdlib-only application on one host that removes a registry, its credentials
and an image pull from the loop, and a deploy becomes a checkout and a restart.

### If you would rather not run a runner

A pull-based deploy covers most of this with no daemon:

```bash
# /etc/systemd/system/elastic-ops-pull.service  (plus a .timer every 5 min)
ExecStart=/bin/sh -c 'cd /opt/elastic-ops/src && git pull --ff-only && docker compose up -d'
```

You lose the pre-deploy snapshot, the health gate and the deploy log in GitHub.

## Tests

```bash
python3 tests/smoke.py     # 94 assertions against a real server
python3 tests/hygiene.py   # stdlib-only, no committed secrets
```

Starts the real server against a temporary database and walks through 94
assertions: open routes, login and lockout, every read endpoint, creating and
resolving an incident, CSRF rejection, ingest with a good and a bad token, job
check-in, path traversal, and sign-out. Exit code 0 means all passed.

On Python 3.10 it borrows `tomli` (`pip install tomli`), which is the same
parser that became `tomllib` in 3.11. The container runs 3.12 and needs nothing.

---

## What it watches, beyond "is the port open"

**Coverage.** The collectors enumerate every guest on every host. The portal
subtracts: everything that exists, minus everything checked, is everything
nobody is watching. It is on the Services page, and it is how you find out
that the domain controller was never monitored. Guests that genuinely do not
need a check go in `unmonitored_ok` in `inventory.toml` — every entry there
being an explicit decision that nobody gets told when that machine stops.

**Named SQL instances.** `type = "mssql"` with a target of `host\INSTANCE`
asks SQL Browser on UDP 1434 which port the instance is on, then connects to
that. A named instance gets a dynamic port that moves when the service
restarts, so pinning the number produces a check that goes red for a reason
nobody can act on. That had already happened once here.

**Certificates.** Every `https` check also reads its certificate, refreshed
every six hours. Each one is judged against *its own* lifetime — 15% of it,
floored at an hour, capped at 21 days — because a fixed "warn under 14 days"
would alert twice a day about a Caddy internal cert that lives twelve hours
and renews itself, while saying nothing useful about one that runs to 3025.

**Silences.** A Silence button on each service mutes it for a stated number of
hours, with a reason. Nothing longer than a week is accepted. Silencing stops
the message and never the measurement: the check still runs, the transition is
still recorded, and it still appears on the timeline.

## Known gaps

- Restore drills are recorded by hand in `inventory.toml`. Nothing verifies a
  restore actually happened.
- There is no HTTPS. It is reachable on the LAN over plain http, which is why
  `cookie_secure` is off.
- Alert routing rows on the Alerting page describe intent and are edited in
  `inventory.toml`. What the engine enforces is the `severity` on each check
  plus the notify window — which the page now shows alongside them.
- Nothing checks UDP services, or anything needing authentication to probe.
