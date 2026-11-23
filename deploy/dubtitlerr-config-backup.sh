#!/bin/sh
# Snapshot the DubTitlerr config volume -- including the P39 artifact store -- as one dated
# directory, then optionally mirror that copy to another host.
#
# Why this exists: since P39 every per-episode internal artifact (done / stages / words / conf /
# qc / cuts / repair audits / unresolved queue / markers) lives in $CONFIG_DIR/artifacts.db,
# NOT beside the media. A media-tree backup no longer contains them, so losing the config
# volume now costs a re-transcription of every episode and destroys the review-queue evidence.
# The store is the one file that must be in the config backup.
#
# Configuration comes from the environment (see dubtitlerr-config-backup.service):
#   CONFIG_DIR   config volume root            default /config
#   BACKUP_DIR   local backup root             required
#   RETAIN_DAYS  dated snapshot copies to keep default 14
#   COPY_TO      optional rsync destination    default empty (local copy only)
#
# The store is snapshotted with sqlite3's ".backup", NOT copied. A live WAL database is only
# self-consistent together with its -wal/-shm files, and those can change mid-copy; ".backup"
# reads the same consistent snapshot the pipeline's readers see. That copy needs no -wal/-shm
# pair beside it and is valid while the pipeline is running.
set -eu

: "${CONFIG_DIR:=/config}"
: "${BACKUP_DIR:?set BACKUP_DIR (local backup root)}"
: "${RETAIN_DAYS:=14}"
COPY_TO=${COPY_TO:-}

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/$STAMP"

# Never write the snapshot inside the tree being snapshotted: rsync would copy it into itself.
case "$OUT/" in
"$CONFIG_DIR"/*)
    echo "FATAL: BACKUP_DIR=$BACKUP_DIR is inside CONFIG_DIR=$CONFIG_DIR" >&2
    exit 1
    ;;
esac

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
mkdir -p "$OUT"
chmod 700 "$OUT" 2>/dev/null || true

if [ -f "$CONFIG_DIR/artifacts.db" ]; then
    if ! command -v sqlite3 >/dev/null 2>&1; then
        echo "FATAL: sqlite3 is required to snapshot $CONFIG_DIR/artifacts.db consistently" >&2
        exit 1
    fi
    sqlite3 "$CONFIG_DIR/artifacts.db" ".backup '$OUT/artifacts.db'"
    chmod 600 "$OUT/artifacts.db" 2>/dev/null || true
    echo "artifacts.db: $(sqlite3 "$OUT/artifacts.db" 'SELECT COUNT(*) FROM artifacts') row(s)"
else
    echo "warning: no $CONFIG_DIR/artifacts.db -- the store is absent, nothing to snapshot" >&2
fi

# Everything else in the volume: settings, secrets (0600), requests, glossaries, processing
# state. The store's own files are excluded -- artifacts.db came from ".backup" above, and a
# -wal/-shm pair left by the live process must not be mixed into a different snapshot.
rsync -a \
    --exclude 'artifacts.db' --exclude 'artifacts.db-wal' --exclude 'artifacts.db-shm' \
    "$CONFIG_DIR/" "$OUT/"

# Retention: only the dated directories this script writes, never anything else in BACKUP_DIR.
old_snapshots=$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-*' -mtime +"$RETAIN_DAYS")
if [ -n "$old_snapshots" ]; then
    printf '%s\n' "$old_snapshots" | while IFS= read -r dir; do
        rm -rf -- "$dir"
    done
fi
printf 'pruned: %s snapshot(s) older than %sd\n' "$(printf '%s' "$old_snapshots" | grep -c . || true)" "$RETAIN_DAYS"

if [ -n "$COPY_TO" ]; then
    rsync -a "$OUT/" "$COPY_TO/$STAMP/"
    echo "mirrored to $COPY_TO/$STAMP/"
fi

echo "backup complete: $OUT"
