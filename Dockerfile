# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ────────────────────────────────────────────────────────
# Compile Python deps in an isolated image so the final image stays slim.
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Stage 2: litestream binary ──────────────────────────────────────────────
# Pulls a single static binary — used to continuously replicate SQLite to S3.
FROM alpine:3.20 AS litestream
ARG LITESTREAM_VERSION=0.3.13
RUN apk add --no-cache curl tar \
 && ARCH="$(uname -m)" \
 && case "$ARCH" in \
        x86_64)  LS_ARCH=amd64 ;; \
        aarch64) LS_ARCH=arm64 ;; \
        *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-${LS_ARCH}.tar.gz" \
        | tar -xz -C /usr/local/bin/ litestream


# ─── Stage 3: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Runtime libs only (no compilers). lxml needs libxml2/libxslt at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 redis-tools tini \
    && rm -rf /var/lib/apt/lists/*

# Copy built Python deps and Litestream binary
COPY --from=builder   /install            /usr/local
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream

WORKDIR /app

# App source — config/ and data/ are volume-mounted at runtime
COPY . .
RUN rm -rf .venv __pycache__ data node_modules .git

# Ensure required dirs exist as directories (not files) before volume mount
RUN mkdir -p /app/data /app/config

# Stash defaults in a separate path. The runtime volume gets mounted at /app/config
# (overlaying it empty on first boot); entrypoint.sh seeds from /app/config-defaults
# so settings/sources survive volume mounts AND first deploys work out of the box.
RUN cp -r /app/config /app/config-defaults

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LITESTREAM_CONFIG=/etc/litestream.yml

EXPOSE 8000

# tini = proper PID 1, forwards signals so APScheduler/uvicorn shut down cleanly
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python", "main.py"]
