#!/bin/sh
# ─── Redis backup loop ───────────────────────────────────────────────────────
# Triggers BGSAVE inside Redis, waits for the RDB to flush, then uploads it
# to S3-compatible storage. Runs forever (driven by docker compose restart).
#
# Tunable via env vars:
#   BACKUP_INTERVAL_SECONDS  — how often to snapshot (default 3600 = 1h)
#   BACKUP_RETENTION_DAYS    — delete S3 objects older than this (default 7)
#   S3_BUCKET / S3_PREFIX    — destination
#   S3_ENDPOINT              — for R2/MinIO/etc. (omit for AWS)
#
# Restore: `aws s3 cp s3://$S3_BUCKET/$S3_PREFIX/dump-LATEST.rdb ./dump.rdb`
#          → drop into redis volume → `docker compose up redis`
set -eu

# Install redis-cli (image is amazon/aws-cli, doesn't ship with redis-cli)
if ! command -v redis-cli >/dev/null 2>&1; then
    echo "[backup-redis] installing redis-cli..."
    yum install -y -q redis6 >/dev/null 2>&1 || \
        (curl -fsSL https://download.redis.io/releases/redis-7.2.4.tar.gz | tar xz \
         && cd redis-7.2.4 && make redis-cli BUILD_TLS=no >/dev/null && cp src/redis-cli /usr/local/bin/)
fi

S3_ARGS=""
if [ -n "${S3_ENDPOINT:-}" ]; then
    S3_ARGS="--endpoint-url ${S3_ENDPOINT}"
fi

if [ -z "${S3_BUCKET:-}" ]; then
    echo "[backup-redis] S3_BUCKET not set — backups disabled. Idling."
    while true; do sleep 86400; done
fi

echo "[backup-redis] starting — every ${BACKUP_INTERVAL_SECONDS}s, retention ${BACKUP_RETENTION_DAYS}d"
echo "[backup-redis] target: s3://${S3_BUCKET}/${S3_PREFIX}/"

while true; do
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    KEY="${S3_PREFIX}/dump-${TS}.rdb"

    # Trigger background save and wait until LASTSAVE timestamp advances.
    # This is more reliable than `redis-cli SAVE` (which blocks Redis).
    LAST_BEFORE=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" LASTSAVE)
    redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" BGSAVE >/dev/null

    # Wait up to 60s for BGSAVE to complete
    i=0
    while [ "$i" -lt 60 ]; do
        LAST_AFTER=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" LASTSAVE)
        if [ "$LAST_AFTER" != "$LAST_BEFORE" ]; then break; fi
        sleep 1
        i=$((i + 1))
    done

    if [ -f /data/dump.rdb ]; then
        SIZE=$(stat -c %s /data/dump.rdb 2>/dev/null || stat -f %z /data/dump.rdb)
        echo "[backup-redis] $TS uploading ${SIZE} bytes → s3://${S3_BUCKET}/${KEY}"
        aws ${S3_ARGS} s3 cp /data/dump.rdb "s3://${S3_BUCKET}/${KEY}" --only-show-errors

        # Also maintain a "LATEST" pointer for one-line restore
        aws ${S3_ARGS} s3 cp /data/dump.rdb "s3://${S3_BUCKET}/${S3_PREFIX}/dump-LATEST.rdb" --only-show-errors
    else
        echo "[backup-redis] WARN: /data/dump.rdb not found after BGSAVE — skipping"
    fi

    # ── Retention sweep ──────────────────────────────────────────────────────
    # Delete RDB snapshots older than BACKUP_RETENTION_DAYS. Keeps cost bounded.
    # The LATEST pointer is overwritten in place so it's never expired.
    CUTOFF_EPOCH=$(($(date -u +%s) - BACKUP_RETENTION_DAYS * 86400))
    aws ${S3_ARGS} s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/" 2>/dev/null \
        | awk '/dump-[0-9]+T/ {print $1" "$2" "$4}' \
        | while read -r D T NAME; do
            FILE_EPOCH=$(date -u -d "$D $T" +%s 2>/dev/null || echo 0)
            if [ "$FILE_EPOCH" -gt 0 ] && [ "$FILE_EPOCH" -lt "$CUTOFF_EPOCH" ]; then
                echo "[backup-redis] pruning old snapshot: $NAME"
                aws ${S3_ARGS} s3 rm "s3://${S3_BUCKET}/${S3_PREFIX}/${NAME}" --only-show-errors
            fi
        done

    sleep "${BACKUP_INTERVAL_SECONDS}"
done
