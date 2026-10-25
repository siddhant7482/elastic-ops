#!/bin/sh
# Install the estate collector on a hypervisor.
#
#   OPS_URL=http://10.10.1.70:8080 OPS_TOKEN=<ingest token> sh install-collector.sh
#
# Installs post_estate.py, writes its config, sets up two systemd timers, and
# posts once so the portal has data before you walk away. Safe to re-run.
#
# The check-in token is derived from the hostname — collector-cinnamon,
# collector-proxmox and so on — so each host reports separately. Sharing one
# token would let a single host still reporting mask four that had stopped,
# which is the exact failure this is here to catch.

set -eu

URL="${OPS_URL:-}"
TOKEN="${OPS_TOKEN:-}"
SRC="${SRC:-$(dirname "$0")/post_estate.py}"
DEST=/opt/estate/post_estate.py
HOST=$(hostname -s)

[ -n "$URL" ] || { echo "set OPS_URL, e.g. http://10.10.1.70:8080" >&2; exit 1; }
[ -n "$TOKEN" ] || { echo "set OPS_TOKEN to the portal's ingest token" >&2; exit 1; }
[ -f "$SRC" ] || { echo "cannot find post_estate.py at $SRC" >&2; exit 1; }

echo "==> Installing on $HOST"
install -D -m 755 "$SRC" "$DEST"

printf 'OPS_URL=%s\nOPS_TOKEN=%s\nOPS_CHECKIN=collector-%s\n' "$URL" "$TOKEN" "$HOST" \
    > /etc/default/estate-collector
chmod 600 /etc/default/estate-collector
echo "    $DEST and /etc/default/estate-collector written"

echo "==> Checking it can reach the portal"
if ! curl -fsS -m 10 "$URL/health" >/dev/null 2>&1; then
    echo "    WARNING: $URL/health did not answer from this host."
    echo "    On yam this is expected until the LAN route is sorted; the timers"
    echo "    are still installed and will start working once it is."
fi

echo "==> Timers"
cat > /etc/systemd/system/estate-collector.service <<EOF
[Unit]
Description=Post backup state and host metrics to Elastic Ops
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $DEST
EOF

cat > /etc/systemd/system/estate-collector-metrics.service <<EOF
[Unit]
Description=Post host metrics to Elastic Ops (memory and pools only)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $DEST --metrics-only
EOF

# The full scan walks every guest and every storage, so it runs once a day,
# after the backups have finished. Metrics are cheap, so they run often enough
# to draw a usable line.
cat > /etc/systemd/system/estate-collector.timer <<'EOF'
[Unit]
Description=Daily full estate report

[Timer]
OnCalendar=*-*-* 06:05:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/estate-collector-metrics.timer <<'EOF'
[Unit]
Description=Host metrics every ten minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now estate-collector.timer estate-collector-metrics.timer >/dev/null
echo "    estate-collector.timer (daily 06:05) and estate-collector-metrics.timer (10 min)"

echo "==> Posting once now"
python3 "$DEST" || echo "    the first post failed — see the message above"

echo
echo "Done on $HOST. Check-in token: collector-$HOST"
systemctl list-timers 'estate-collector*' --no-pager | head -4
