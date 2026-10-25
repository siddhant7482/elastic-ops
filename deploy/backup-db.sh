#!/bin/sh
# Back up the portal's own database.
#
# This exists because the tool that tracks whether everything is backed up was
# itself in no backup job. ops.db holds the accounts, the incident history, the
# change log and every check result — none of it reconstructible.
#
# vzdump backs up guests, not the hypervisor's own filesystem, so nothing on
# pepper covers /var/lib/elastic-ops. Hence this.
#
#   sh backup-db.sh
#
# Optional, and strongly recommended: set OPS_BACKUP_TARGET in
# /opt/elastic-ops/.env to an scp destination on another machine, e.g.
#
#   OPS_BACKUP_TARGET=root@10.10.1.80:/var/backups/elastic-ops
#
# Without it the copies sit on the same host as the original, which protects
# you from corruption and mistakes but not from losing the host.

set -eu

DB="${OPS_DB:-/var/lib/elastic-ops/ops.db}"
DIR="${OPS_BACKUP_DIR:-/var/lib/elastic-ops/backups}"
KEEP="${OPS_BACKUP_KEEP:-14}"
TARGET="${OPS_BACKUP_TARGET:-}"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/ops-$STAMP.db"

# sqlite3's backup API, not cp: the portal is running and writing, and copying
# a live SQLite file with its write-ahead log in flight gives you a file that
# restores to a surprise.
python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
b = sqlite3.connect(dst)
with b:
    a.backup(b)
a.close(); b.close()
PY

gzip -f "$OUT"
OUT="$OUT.gz"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Prove it restores. A backup nobody has opened is a hypothesis — the same
# argument this portal makes about the estate applies to the portal.
TMP=$(mktemp -u)
gzip -dc "$OUT" > "$TMP"
if python3 -c "
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
n=c.execute('SELECT COUNT(1) FROM users').fetchone()[0]
print(f'    verified: integrity ok, {n} account(s)')
" "$TMP"; then
    rm -f "$TMP"
else
    rm -f "$TMP"
    echo "    VERIFY FAILED — the snapshot did not open cleanly" >&2
    exit 1
fi

ls -1t "$DIR"/ops-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "    keeping the newest $KEEP"

if [ -n "$TARGET" ]; then
    if scp -q -o BatchMode=yes -o ConnectTimeout=10 "$OUT" "$TARGET/" 2>/dev/null; then
        echo "    copied off this host to $TARGET"
    else
        echo "    WARNING: could not copy to $TARGET — the only copy is on this host" >&2
    fi
else
    echo "    NOTE: OPS_BACKUP_TARGET is unset, so this copy lives on the same"
    echo "          host as the original. Losing pepper loses both."
fi
