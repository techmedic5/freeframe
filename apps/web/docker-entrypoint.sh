#!/bin/sh
# Refuse to start when NEXT_PUBLIC_BASE_PATH does not match the base path this
# image was built with. Next bakes the base path into the build, so a mismatch
# would not fail loudly: the server would start and every link would be wrong.
set -eu

baked=$(node -p "require('/app/.next/required-server-files.json').config.basePath||''")
runtime="${NEXT_PUBLIC_BASE_PATH:-}"

if [ -n "$runtime" ] && [ "$runtime" != "$baked" ]; then
  shown="${baked:-/}"
  echo "FreeFrame: this web image was built to be served from \"$shown\", but NEXT_PUBLIC_BASE_PATH is \"$runtime\". Published images only support the domain root. To serve FreeFrame under a sub-path, build the web image yourself: docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build web" >&2
  exit 1
fi

exec "$@"
