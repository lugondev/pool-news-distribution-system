#!/bin/sh
# ─── Redis restore from S3 ───────────────────────────────────────────────────
# Pulls the latest RDB snapshot from S3 into the local redis volume.
# Run BEFORE first `docker compose up` on a new host.
#
# Usage:
#   ./scripts/restore-redis.sh                # restore LATEST
#   ./scripts/restore-redis.sh dump-20260101T000000Z.rdb   # restore specific
set -eu

# Load .env if present
if [ -f .env ]; then set -a; . ./.env; set +a; fi

: "${LITESTREAM_BUCKET:?must be set}"
PREFIX="${REDIS_BACKUP_PREFIX:-news-aggregator/redis}"
KEY="${1:-dump-LATEST.rdb}"
VOLUME_NAME="${COMPOSE_PROJECT_NAME:-news-aggregator}_redis-data"

S3_ARGS=""
if [ -n "${LITESTREAM_ENDPOINT:-}" ]; then
    S3_ARGS="--endpoint-url ${LITESTREAM_ENDPOINT}"
fi

echo "→ Downloading s3://${LITESTREAM_BUCKET}/${PREFIX}/${KEY}"
TMP=$(mktemp -d)
docker run --rm \
    -e AWS_ACCESS_KEY_ID="${LITESTREAM_ACCESS_KEY_ID}" \
    -e AWS_SECRET_ACCESS_KEY="${LITESTREAM_SECRET_ACCESS_KEY}" \
    -e AWS_DEFAULT_REGION="${LITESTREAM_REGION:-auto}" \
    -v "${TMP}:/out" \
    amazon/aws-cli:2.15.30 \
    ${S3_ARGS} s3 cp "s3://${LITESTREAM_BUCKET}/${PREFIX}/${KEY}" /out/dump.rdb

echo "→ Copying into volume ${VOLUME_NAME}"
docker volume create "${VOLUME_NAME}" >/dev/null
docker run --rm \
    -v "${TMP}:/in:ro" \
    -v "${VOLUME_NAME}:/data" \
    busybox:1.36 \
    sh -c "cp /in/dump.rdb /data/dump.rdb && chown 999:999 /data/dump.rdb"

rm -rf "${TMP}"
echo "✓ Restore complete. Start the stack: docker compose up -d"
