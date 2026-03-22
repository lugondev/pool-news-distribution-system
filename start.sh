#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Check Redis ────────────────────────────────────────────────────────────────
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
REDIS_HOST=$(echo "$REDIS_URL" | sed -E 's|redis://([^:/]+).*|\1|')
REDIS_PORT=$(echo "$REDIS_URL" | sed -E 's|redis://[^:]+:([0-9]+).*|\1|')
REDIS_PORT="${REDIS_PORT:-6379}"

info "Checking Redis at $REDIS_HOST:$REDIS_PORT..."
if ! redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping &>/dev/null; then
    error "Redis is not reachable at $REDIS_HOST:$REDIS_PORT"
    error "Start Redis first: redis-server  (or docker run -d -p 6379:6379 redis:alpine)"
    exit 1
fi
info "Redis OK"

# ── Create data dir ────────────────────────────────────────────────────────────
mkdir -p data

# ── Virtualenv / dependencies ──────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    warn "No .venv found — creating..."
    python3 -m venv .venv
fi

# Activate venv
# shellcheck disable=SC1091
source .venv/bin/activate

# Check if deps are installed (quick check via importlib)
if ! python -c "import fastapi" 2>/dev/null; then
    info "Installing dependencies..."
    pip install -q -r requirements.txt
fi

# ── .env ──────────────────────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    warn ".env not found — copying from .env.example"
    cp .env.example .env
fi

# ── Start ─────────────────────────────────────────────────────────────────────
info "Starting News Aggregator on http://0.0.0.0:8000 ..."
exec python main.py
