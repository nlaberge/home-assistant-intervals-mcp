#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/versions.env"

docker build \
  --platform "${PLATFORM:-linux/amd64}" \
  --build-arg "INTERVALS_VERSION=$INTERVALS_VERSION" \
  --build-arg "INTERVALS_COMMIT=$INTERVALS_COMMIT" \
  --build-arg "TUNNEL_CLIENT_VERSION=$TUNNEL_CLIENT_VERSION" \
  --build-arg "UV_VERSION=$UV_VERSION" \
  -t ha-intervals-mcp:local \
  "$ROOT/intervals-mcp"
