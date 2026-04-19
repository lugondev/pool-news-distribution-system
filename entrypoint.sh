#!/bin/sh
# Ensure config files exist (volume mount may provide an empty directory)
for f in settings.yaml sources.yaml; do
  if [ ! -f "config/$f" ] && [ -f "config/${f}.example" ]; then
    echo "[entrypoint] config/$f missing — copying from ${f}.example"
    cp "config/${f}.example" "config/$f"
  fi
done

exec "$@"
