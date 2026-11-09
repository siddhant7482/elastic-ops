#!/bin/sh
# Turn a small always-on box into a remote-site router for the estate.
#
# Run it HERE, on your desk, before the box is shipped. The instruction at the
# far end is then "plug this into power and the network", which is the only
# instruction that survives contact with an office that has nobody technical
# in it.
#
#   TS_AUTHKEY=tskey-auth-...        \
#   SITE=israel                      \
#   OPS_URL=http://pepper:8080       \
#   OPS_CHECKIN=<token from checks.toml> \
#   sh provision-site-router.sh
#
# What it sets up:
#
#   * Tailscale, pre-authenticated, so it joins the tailnet on first boot with
#     nobody touching it.
#   * IP forwarding, so it can route for the printer and anything else there.
#   * A boot-time job that works out the subnet it is actually on and
#     advertises that. This is the part that matters: the box is configured on
#     your LAN and then plugged in on a different one, and a hard-coded route
#     would advertise your own subnet into the tailnet from Israel. It
#     re-checks every ten minutes, so a DHCP change fixes itself.
#   * A check-in to Elastic Ops, so the box is monitored like any other job.
#     Silence is the alert: if the box dies, you are told. That is the whole
#     difference between this and leaving a laptop switched on.
#
# Deliberately NOT set up: ssh exposed to the internet, a VPN concentrator, or
# anything else the far office would have to maintain. It has one job.

set -eu

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root"
[ -n "${TS_AUTHKEY:-}" ] || die "set TS_AUTHKEY to a reusable, pre-approved auth key"
SITE="${SITE:-site}"
OPS_URL="${OPS_URL:-}"
OPS_CHECKIN="${OPS_CHECKIN:-}"

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates unattended-upgrades >/dev/null

if ! command -v tailscale >/dev/null 2>&1; then
    echo "==> tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "==> ip forwarding"
cat > /etc/sysctl.d/99-tailscale-router.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sysctl -p /etc/sysctl.d/99-tailscale-router.conf >/dev/null

echo "==> subnet advertiser"
cat > /usr/local/sbin/site-advertise <<'EOF'
#!/bin/sh
# Advertise whatever subnet this box is actually on.
#
# It is configured on one LAN and plugged in on another. Hard-coding the route
# would mean advertising the wrong network from the wrong country, and the
# symptom would be a printer that is unreachable for reasons nobody can see.
set -eu
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
[ -n "${IFACE:-}" ] || exit 0
CIDR=$(ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null \
       | awk '{print $4; exit}')
[ -n "${CIDR:-}" ] || exit 0

# 192.168.7.23/24 -> 192.168.7.0/24
NET=$(python3 - "$CIDR" <<'PY'
import ipaddress, sys
print(ipaddress.ip_interface(sys.argv[1]).network)
PY
)
[ -n "${NET:-}" ] || exit 0

CUR=$(tailscale debug prefs 2>/dev/null | grep -o '"AdvertiseRoutes":[^]]*]' || true)
case "$CUR" in
  *"$NET"*) exit 0 ;;                      # already advertising it
esac
logger -t site-advertise "advertising $NET on $IFACE"
tailscale set --advertise-routes="$NET" 2>/dev/null \
  || tailscale up --advertise-routes="$NET" --accept-dns=false --reset=false
EOF
chmod 755 /usr/local/sbin/site-advertise

cat > /etc/systemd/system/site-advertise.service <<'EOF'
[Unit]
Description=Advertise the local subnet into the tailnet
After=tailscaled.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/site-advertise
EOF

cat > /etc/systemd/system/site-advertise.timer <<'EOF'
[Unit]
Description=Re-check the advertised subnet

[Timer]
OnBootSec=45s
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

if [ -n "$OPS_URL" ] && [ -n "$OPS_CHECKIN" ]; then
    echo "==> check-in to Elastic Ops"
    printf 'OPS_URL=%s\nOPS_CHECKIN=%s\n' "$OPS_URL" "$OPS_CHECKIN" \
        > /etc/default/site-router
    chmod 600 /etc/default/site-router

    cat > /etc/systemd/system/site-checkin.service <<'EOF'
[Unit]
Description=Tell Elastic Ops this site router is alive
After=tailscaled.service

[Service]
Type=oneshot
EnvironmentFile=/etc/default/site-router
# Only check in once the tunnel is actually up. Reporting healthy while the
# route is down would be the exact lie the job monitoring exists to catch.
ExecStart=/bin/sh -c 'tailscale status --peers=false >/dev/null 2>&1 && curl -fsS -m 15 "$OPS_URL/checkin/$OPS_CHECKIN" >/dev/null'
EOF

    cat > /etc/systemd/system/site-checkin.timer <<'EOF'
[Unit]
Description=Site router check-in

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF
else
    echo "==> check-in skipped (set OPS_URL and OPS_CHECKIN to enable it)"
fi

echo "==> unattended security updates"
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

echo "==> joining the tailnet as ${SITE}-router"
tailscale up \
    --authkey="$TS_AUTHKEY" \
    --hostname="${SITE}-router" \
    --advertise-tags=tag:site-router \
    --accept-dns=false \
    --ssh

systemctl daemon-reload
systemctl enable --now site-advertise.timer >/dev/null
[ -f /etc/systemd/system/site-checkin.timer ] \
    && systemctl enable --now site-checkin.timer >/dev/null
/usr/local/sbin/site-advertise || true

echo
echo "done. this box is ${SITE}-router on the tailnet."
tailscale status --peers=false || true
echo
echo "Before you ship it:"
echo "  1. approve its advertised route in the Tailscale admin console,"
echo "     or set an autoApprover for tag:site-router so it needs no one."
echo "  2. power it down. It will re-advertise the correct subnet on arrival."
echo "  3. the instruction at the far end is: plug in power, plug in network."
