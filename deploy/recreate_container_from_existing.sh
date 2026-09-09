#!/usr/bin/env bash
# Recreate one named container on a new image without exposing its environment.
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 <existing-container> <new-image> <rollback-suffix>" >&2
    exit 2
fi

container=$1
image=$2
suffix=$3
rollback_name="${container}-rollback-${suffix}"

if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "container not found: $container" >&2
    exit 1
fi
if docker inspect "$rollback_name" >/dev/null 2>&1; then
    echo "rollback container already exists: $rollback_name" >&2
    exit 1
fi

mapfile -d '' -t run_args < <(
    docker inspect "$container" | python3 -c '
import json
import sys

source = json.load(sys.stdin)[0]
config = source["Config"]
host = source["HostConfig"]

def emit(*values):
    for value in values:
        sys.stdout.buffer.write(str(value).encode() + b"\0")

if config.get("User"):
    emit("--user", config["User"])
if config.get("WorkingDir"):
    emit("--workdir", config["WorkingDir"])
restart = (host.get("RestartPolicy") or {}).get("Name")
if restart:
    emit("--restart", restart)
for bind in host.get("Binds") or []:
    emit("--volume", bind)
for container_port, bindings in (host.get("PortBindings") or {}).items():
    for binding in bindings or []:
        host_ip = binding.get("HostIp") or ""
        host_port = binding.get("HostPort") or ""
        prefix = f"{host_ip}:" if host_ip else ""
        emit("--publish", f"{prefix}{host_port}:{container_port}")
for item in config.get("Env") or []:
    emit("--env", item)
'
)

mapfile -d '' -t command < <(
    docker inspect "$container" | python3 -c '
import json
import sys

for item in json.load(sys.stdin)[0]["Config"].get("Cmd") or []:
    sys.stdout.buffer.write(item.encode() + b"\0")
'
)

network=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")
if [[ -n "$network" && "$network" != "default" ]]; then
    run_args+=(--network "$network")
fi

docker stop "$container"
docker rename "$container" "$rollback_name"

if ! docker run -d --name "$container" "${run_args[@]}" "$image" "${command[@]}"; then
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rename "$rollback_name" "$container"
    docker start "$container"
    exit 1
fi
