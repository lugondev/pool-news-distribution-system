#!/bin/sh
set -e

# ── Bootstrap config files ───────────────────────────────────────────────────
# When /app/config is a fresh named volume, it shadows the baked-in defaults.
# Seed any MISSING files from /app/config-defaults so first deploy works and
# operator edits (via UI or editor) are never overwritten on subsequent boots.
if [ -d /app/config-defaults ]; then
    for src in /app/config-defaults/*; do
        [ -e "$src" ] || continue
        name=$(basename "$src")
        if [ ! -e "/app/config/$name" ]; then
            echo "[entrypoint] seeding /app/config/$name from defaults"
            cp -r "$src" "/app/config/$name"
        fi
    done
fi

# ── Config backend selection ─────────────────────────────────────────────────
# CONFIG_BACKEND=yaml  → app reads/writes config/*.yaml in volume (default)
# CONFIG_BACKEND=db    → app reads/writes Postgres tables directly
#                        (requires SUPABASE_DB_URL + tables initialized)
#
# In `db` mode we run a fast pre-boot reachability check. If Supabase is
# unreachable we exit with a clear error rather than letting the app start
# and crash on first config read. Yaml mode skips this check entirely.
CONFIG_BACKEND="${CONFIG_BACKEND:-yaml}"
case "$CONFIG_BACKEND" in
    db|postgres|pg)
        if [ -z "${SUPABASE_DB_URL:-}" ]; then
            echo "[entrypoint] FATAL: CONFIG_BACKEND=$CONFIG_BACKEND but SUPABASE_DB_URL is empty"
            exit 1
        fi
        echo "[entrypoint] CONFIG_BACKEND=$CONFIG_BACKEND — verifying Supabase reachability..."
        # Capture to tmp so we get python's real exit code (POSIX sh pipes
        # surface the LAST stage's exit code).
        if python -c "import os, psycopg
psycopg.connect(os.environ['SUPABASE_DB_URL'], connect_timeout=8, prepare_threshold=None).close()
" > /tmp/db_check.log 2>&1; then
            echo "[entrypoint] Supabase reachable — app will read/write config from DB"
        else
            sed 's/^/[entrypoint] /' /tmp/db_check.log
            echo "[entrypoint] FATAL: cannot reach Supabase. Fix SUPABASE_DB_URL or unset CONFIG_BACKEND to fall back to yaml."
            rm -f /tmp/db_check.log
            exit 1
        fi
        rm -f /tmp/db_check.log
        ;;
    yaml|"")
        : # default — no extra setup
        ;;
    *)
        echo "[entrypoint] WARN: unknown CONFIG_BACKEND='$CONFIG_BACKEND' — treating as yaml"
        ;;
esac

# ── Litestream restore ───────────────────────────────────────────────────────
# On first boot of a fresh volume, pull the latest SQLite snapshot from S3
# BEFORE the app opens the DB. This is what makes migration painless: spin up
# the container on any host, it auto-rebuilds state from R2.
#
# Behavior:
#   - LITESTREAM_BUCKET unset           → skip (single-machine / dev mode)
#   - DB file already exists locally    → skip (don't clobber live data)
#   - Restore fails because no backup   → ignored (first-ever deploy)
SQLITE_PATH="${SQLITE_PATH:-/app/data/stats.db}"

if [ -n "${LITESTREAM_BUCKET:-}" ] && [ ! -f "$SQLITE_PATH" ]; then
    echo "[entrypoint] No local SQLite found — attempting restore from s3://${LITESTREAM_BUCKET}"
    if litestream restore -if-replica-exists -config /etc/litestream.yml "$SQLITE_PATH"; then
        echo "[entrypoint] Restore complete: $SQLITE_PATH"
    else
        echo "[entrypoint] No replica found (likely first deploy) — starting with empty DB"
    fi
fi

# ── Run app under Litestream supervision (if configured) ─────────────────────
# `litestream replicate -exec` runs the app as a child process and streams
# WAL pages to S3 in the background. When the app exits, Litestream flushes
# the final WAL frames and exits — ensuring no data loss on graceful shutdown.
if [ -n "${LITESTREAM_BUCKET:-}" ]; then
    echo "[entrypoint] Starting Litestream replication for $SQLITE_PATH"
    exec litestream replicate -config /etc/litestream.yml -exec "$*"
else
    echo "[entrypoint] LITESTREAM_BUCKET not set — running without replication"
    exec "$@"
fi
