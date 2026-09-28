"""Read the live embedding-worker topology before pausing its consumers."""

from __future__ import annotations

import os
import sys

import paramiko


COMMAND = r'''set -euo pipefail
docker ps --format '{{.Names}}|{{.Image}}|{{.Command}}|{{.Status}}' | grep -Ei 'embed|vector|worker|import' || true
docker inspect --format 'embedding-worker: running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}}' vector-kb-embedder-release
docker exec vector-kb-importer-release python -c "import os; print('importer-exclude-pdf=' + os.environ.get('WORKER_EXCLUDE_PDF', ''))"
docker exec vector-kb-embedder-release python -c "import os; print('embedder-exclude-pdf=' + os.environ.get('WORKER_EXCLUDE_PDF', ''))"
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
