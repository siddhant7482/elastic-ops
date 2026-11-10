#!/bin/sh
# vzdump hook: tell Elastic Ops the nightly backup job finished.
#
# Install on the Proxmox host:
#
#   install -m 755 vzdump-checkin.sh /usr/local/sbin/vzdump-checkin
#   printf 'OPS_URL=%s\nOPS_CHECKIN=%s\n' http://10.10.1.70:8080 TOKEN \
#       > /etc/default/estate-backup
#   chmod 600 /etc/default/estate-backup
#   echo 'script: /usr/local/sbin/vzdump-checkin' >> /etc/vzdump.conf
#
# Two things this deliberately does NOT do:
#
#   * It checks in on `job-end` only. `job-abort` is silence, and silence is
#     the alert. A hook that reported on every outcome would turn a failed
#     backup into a green row, which is the exact lie the job monitoring
#     exists to catch.
#   * It never fails. vzdump treats a non-zero hook as a reason to abort the
#     backup, so a monitoring problem must never be able to stop a backup
#     running. Monitoring that can break the thing it watches is worse than
#     no monitoring.
#
# vzdump calls this with the phase as $1: job-init, job-start, job-end,
# job-abort, backup-start, backup-end, backup-abort, log-end.

PHASE="$1"

# Only the whole job finishing counts. Per-guest backup-end would check in
# seven times a night and go green even if six of them had failed.
[ "$PHASE" = "job-end" ] || exit 0

[ -r /etc/default/estate-backup ] || exit 0
. /etc/default/estate-backup

# Strip any stray carriage return. A value that came from a Windows machine
# keeps its \r, the URL then ends in one, and curl rejects it as a malformed
# URL rather than as the token problem it looks like.
OPS_URL=$(printf %s "$OPS_URL" | tr -d '\r')
OPS_CHECKIN=$(printf %s "$OPS_CHECKIN" | tr -d '\r')

[ -n "$OPS_URL" ] || exit 0
[ -n "$OPS_CHECKIN" ] || exit 0

# Keep curl's own error, so a failure says why rather than just that it
# happened. It still never changes the exit status.
ERR=$(curl -fsS -m 15 "$OPS_URL/checkin/$OPS_CHECKIN" 2>&1 >/dev/null) \
    || logger -t vzdump-checkin "check-in failed: ${ERR:-unknown}; the backup itself is unaffected"

exit 0
