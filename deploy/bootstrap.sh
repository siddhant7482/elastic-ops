#!/bin/sh
# Elastic Ops — first-time install on a host.
#
# Run as root on pepper. It is safe to re-run: it never overwrites .env or the
# database, and it leaves every other container on the host alone.
#
#   sh bootstrap.sh
#
# What it does
#   /opt/elastic-ops/src       the checkout the container mounts read-only
#   /opt/elastic-ops/.env      secrets, created once, chmod 600
#   /opt/elastic-ops/backups   pre-deploy database snapshots
#
# The compose project is named elastic-ops, so `docker compose` here cannot
# touch the knowledge base, Vaultwarden or the messenger gateway.

set -eu

# The repository is private, so this clones over SSH with a read-only deploy
# key generated on this host. A deploy key is scoped to one repository and
# cannot push, which a personal access token pasted into a clone URL is not.
REPO="${REPO:-git@github.com:siddhant7482/elastic-ops.git}"
BRANCH="${BRANCH:-main}"
BASE="${BASE:-/opt/elastic-ops}"
SRC="$BASE/src"
KEY="$BASE/deploy_key"

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

say "Checking what is already here"
command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
command -v git >/dev/null 2>&1 || die "git is not installed"

if command -v ss >/dev/null 2>&1 && ss -lntH 2>/dev/null | grep -q ':8080 '; then
    die "something is already listening on 8080 — check before continuing"
fi
echo "    docker, compose and git present; port 8080 is free"

say "Checking repository access"
mkdir -p "$BASE/backups"
if [ ! -f "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N "" -f "$KEY" -C "elastic-ops deploy key ($(hostname))"
    chmod 600 "$KEY"
    echo "    generated a deploy key at $KEY"
fi
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
export GIT_SSH_COMMAND

if ! git ls-remote "$REPO" >/dev/null 2>&1; then
    cat <<EOF

This host cannot read the repository yet. Add its deploy key to GitHub:

  https://github.com/siddhant7482/elastic-ops/settings/keys/new

  Title        pepper
  Key          (the line below, copied whole)
  Allow write  leave UNTICKED — this only ever needs to read

$(cat "$KEY.pub")

Then run this script again.
EOF
    exit 1
fi
echo "    deploy key accepted"

say "Fetching the code into $SRC"
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --quiet origin "$BRANCH"
    git -C "$SRC" checkout --quiet "$BRANCH"
    git -C "$SRC" reset --hard --quiet "origin/$BRANCH"
    echo "    updated to $(git -C "$SRC" rev-parse --short HEAD)"
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$SRC"
    echo "    cloned at $(git -C "$SRC" rev-parse --short HEAD)"
fi
# Persist the key in the checkout, so a later `git pull` by hand or by a timer
# works without this script's environment.
git -C "$SRC" config core.sshCommand "$GIT_SSH_COMMAND"

say "Secrets"
if [ -f "$BASE/.env" ]; then
    echo "    $BASE/.env already exists — left untouched"
else
    TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "$BASE/.env" <<EOF
# Created by bootstrap.sh on $(date -Iseconds). Never commit this file.
OPS_INGEST_TOKEN=$TOKEN
OPS_WEBHOOK_URL=
OPS_WEBHOOK_HEADER=
OPS_DEADMAN_URL=
EOF
    chmod 600 "$BASE/.env"
    echo "    wrote $BASE/.env with a fresh ingest token"
fi
cp "$BASE/.env" "$SRC/.env"
chmod 600 "$SRC/.env"

say "Starting it"
cd "$SRC"
docker compose up -d --remove-orphans

say "Waiting for it to answer"
i=0
while [ "$i" -lt 40 ]; do
    if curl -fsS -m 3 http://127.0.0.1:8080/health >/dev/null 2>&1; then
        echo "    healthy after ${i}s"
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$i" -ge 40 ]; then
    docker compose logs --tail 40 ops
    die "it did not answer /health within 40s"
fi

cat <<EOF

------------------------------------------------------------------
Elastic Ops is up at  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080

There are no accounts yet. Create the first one:

  cd $SRC
  docker compose exec ops python3 app.py /app/checks.toml \\
      --adduser <name> --display "<Full Name>"

The ingest token for the collectors is in $BASE/.env
------------------------------------------------------------------
EOF
