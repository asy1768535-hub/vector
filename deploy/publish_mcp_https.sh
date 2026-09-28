#!/usr/bin/env bash
# Run from a staging directory containing Dockerfile.mcp-https and config.py.
set -euo pipefail

source_container=vector-kb-mcp-release
target_container=vector-kb-mcp-https-release
image=vector-kb-app:mcp-https-20260923-r3

if docker inspect "$target_container" >/dev/null 2>&1; then
    echo "target container already exists: $target_container" >&2
    exit 1
fi

source_image=$(docker inspect --format '{{.Config.Image}}' "$source_container")
docker image inspect "$source_image" >/dev/null
docker build --build-arg "BASE_IMAGE=$source_image" -t "$image" -f Dockerfile.mcp-https .

mapfile -d '' -t source_env < <(
    docker inspect "$source_container" | python3 -c '
import json
import sys

excluded = {
    "MCP_ADAPTER_BASE_URL", "MCP_ADAPTER_HTTP_HOST", "MCP_ADAPTER_PRIVATE_NETWORK"
}
for item in json.load(sys.stdin)[0]["Config"].get("Env") or []:
    if item.split("=", 1)[0] not in excluded:
        sys.stdout.buffer.write(b"--env\0" + item.encode() + b"\0")
'
)

docker run -d \
    --name "$target_container" \
    --restart unless-stopped \
    --user vector-kb \
    --workdir /app \
    --network vector-kb-internal \
    "${source_env[@]}" \
    --env MCP_ADAPTER_BASE_URL=http://vector-kb-api-release:8200 \
    --env MCP_ADAPTER_HTTP_HOST=0.0.0.0 \
    --env MCP_ADAPTER_PRIVATE_NETWORK=true \
    --env MCP_ADAPTER_PUBLIC_HOST=ashark.icu \
    --label traefik.enable=true \
    --label traefik.docker.network=apps_traefik-network \
    --label 'traefik.http.routers.vector-kb-mcp.rule=Host(`ashark.icu`) && PathPrefix(`/mcp`)' \
    --label traefik.http.routers.vector-kb-mcp.entrypoints=websecure \
    --label traefik.http.routers.vector-kb-mcp.priority=100 \
    --label traefik.http.routers.vector-kb-mcp.middlewares=vector-kb-mcp-lan \
    --label traefik.http.routers.vector-kb-mcp.tls=true \
    --label traefik.http.routers.vector-kb-mcp.tls.certresolver=letsencrypt \
    --label traefik.http.middlewares.vector-kb-mcp-lan.ipallowlist.sourcerange=10.0.10.0/24 \
    --label traefik.http.services.vector-kb-mcp.loadbalancer.server.port=8301 \
    "$image" python -m app.mcp_adapter

docker network connect apps_traefik-network "$target_container"
echo "MCP HTTPS candidate started; verify authentication and TLS before sharing the URL."
