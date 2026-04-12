#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Args ───────────────────────────────────────────────────────────────────────
DEV_MODE=0
for arg in "$@"; do
    case "$arg" in
        --dev) DEV_MODE=1 ;;
    esac
done

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

# Check for croniter (added in webhook scheduling feature)
if ! python -c "import croniter" 2>/dev/null; then
    info "Installing missing dependencies (croniter)..."
    pip install -q -r requirements.txt
fi

# ── .env ──────────────────────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    warn ".env not found — copying from .env.example"
    cp .env.example .env
fi

# ── Check Weaviate (optional — soft warn only) ─────────────────────────────────
WEAVIATE_HOST="${WEAVIATE_HOST:-localhost}"
WEAVIATE_PORT="${WEAVIATE_PORT:-8080}"

# Only check if weaviate is enabled in settings (requires yaml installed above)
_WEAVIATE_ENABLED=$(python - <<'EOF' 2>/dev/null
import yaml, sys
try:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    print("yes" if cfg.get("weaviate", {}).get("enabled", True) else "no")
except Exception:
    print("yes")
EOF
)

if [ "${_WEAVIATE_ENABLED:-yes}" = "yes" ]; then
    info "Checking Weaviate at $WEAVIATE_HOST:$WEAVIATE_PORT..."
    if python -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://$WEAVIATE_HOST:$WEAVIATE_PORT/v1/.well-known/ready', timeout=3)
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        info "Weaviate OK"
    else
        warn "Weaviate not reachable at $WEAVIATE_HOST:$WEAVIATE_PORT"
        warn "Vector/RAG features will be disabled. Start Weaviate:"
        warn "  docker run -d -p 8080:8080 -p 50051:50051 semitechnologies/weaviate:1.24.6"
        warn "  (or: docker compose up -d weaviate)"
    fi
fi

# ── Start ─────────────────────────────────────────────────────────────────────
if [ "$DEV_MODE" = "1" ]; then
    warn "DEV MODE — crawler/AI scheduler disabled. Dashboard only (port 8001)."
    export DEV_MODE=1
    export DEV_PORT=8001
fi
info "Starting News Aggregator on http://0.0.0.0:8000 ..."
exec python main.py
