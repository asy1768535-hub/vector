"""Read-only verification for the file-resource linkage hotfix."""

from __future__ import annotations

import os
import sys

import paramiko


COMMAND = r'''set -euo pipefail
docker inspect --format '{{.Config.Image}} {{.State.Running}}' vector-kb-api-release
docker port vector-kb-api-release
docker image inspect vector-kb-app:file-resource-link-upsert-20260920-r1 --format 'HOTFIX_IMAGE={{.Id}}' 2>/dev/null || true
docker ps -a --filter name='vector-kb-api-release' --format '{{.Names}} {{.Image}} {{.Status}}'
docker ps --filter name='^/vector-kb-importer-release$' --filter name='^/vector-kb-embedder-release$' --format 'WORKER={{.Names}} {{.Image}} {{.Status}}'
curl --fail --silent --show-error http://127.0.0.1:8200/health/ready
docker exec vector-kb-api-release python -c "from pathlib import Path; source=Path('/app/app/services/import_uploads.py').read_text(); print('ATOMIC_UPSERT_PRESENT=' + str('pg_insert(FileResource)' in source and '.on_conflict_do_update(' in source))"
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
