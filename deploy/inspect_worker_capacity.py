"""Read-only worker capacity and safe runtime settings on the production host."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = b"""from app.config import settings
print(
    'import_worker_batch_size=' + str(settings.import_worker_batch_size),
    'embed_worker_batch_docs=' + str(settings.embed_worker_batch_docs),
    'embed_batch_size=' + str(settings.embed_batch_size),
    sep=' '
)
"""
COMMAND = f'''set -euo pipefail
docker stats --no-stream --format '{{{{.Name}}}} cpu={{{{.CPUPerc}}}} mem={{{{.MemUsage}}}}' vector-kb-importer-release vector-kb-embedder-release vllm-embedding vector-kb-postgres
docker inspect --format '{{{{.Name}}}} cpus={{{{.HostConfig.NanoCpus}}}} memory={{{{.HostConfig.Memory}}}} restart={{{{.HostConfig.RestartPolicy.Name}}}}' vector-kb-importer-release vector-kb-embedder-release
printf %s {base64.b64encode(PYTHON).decode('ascii')} | base64 -d | docker exec -i vector-kb-embedder-release python -
'''


def main() -> None:
    password = os.environ.get("VECTOR_KB_SSH_PASSWORD")
    if not password:
        raise SystemExit("VECTOR_KB_SSH_PASSWORD is required")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        "10.0.10.2",
        username="wmh",
        password=password,
        timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        _, stdout, stderr = client.exec_command(COMMAND, timeout=90)
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")
        if output:
            print(output, end="")
        if errors:
            print(errors, end="", file=sys.stderr)
        raise SystemExit(stdout.channel.recv_exit_status())
    finally:
        client.close()


if __name__ == "__main__":
    main()
