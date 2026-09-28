"""Deploy worker-only PDF deferral overlays and resume non-PDF processing."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import paramiko


HOST = "10.0.10.2"
USERNAME = "wmh"
REMOTE_DIR = "/tmp/vector-kb-exclude-pdf-workers-20260920-r1"
HERE = Path(__file__).resolve().parent
FILES = (
    "Dockerfile.worker-exclude-pdf",
    "patch_worker_exclude_pdf.py",
    "recreate_container_from_existing.sh",
)


def run(client: paramiko.SSHClient, command: str) -> None:
    _, stdout, stderr = client.exec_command(command, timeout=900, get_pty=True)
    output = stdout.read().decode("utf-8", errors="replace")
    errors = stderr.read().decode("utf-8", errors="replace")
    if output:
        print(output, end="")
    if errors:
        print(errors, end="", file=sys.stderr)
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError("remote worker deployment failed")


def main() -> None:
    password = os.environ.get("VECTOR_KB_SSH_PASSWORD")
    if not password:
        raise SystemExit("VECTOR_KB_SSH_PASSWORD is required")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USERNAME,
        password=password,
        timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        run(client, f"mkdir -p {shlex.quote(REMOTE_DIR)}")
        sftp = client.open_sftp()
        try:
            for name in FILES:
                sftp.put(str(HERE / name), f"{REMOTE_DIR}/{name}")
        finally:
            sftp.close()
        command = f"""set -euo pipefail
import_container=vector-kb-importer-release
embed_container=vector-kb-embedder-release
import_base=$(docker inspect --format '{{{{.Config.Image}}}}' "$import_container")
embed_base=$(docker inspect --format '{{{{.Config.Image}}}}' "$embed_container")
echo 'WORKER_EXCLUDE_PDF=1' > {REMOTE_DIR}/worker-exclude-pdf.env
tr -d '\\r' < {REMOTE_DIR}/recreate_container_from_existing.sh > {REMOTE_DIR}/recreate.sh
chmod 700 {REMOTE_DIR}/recreate.sh
docker build --pull=false --build-arg BASE_IMAGE="$import_base" --build-arg WORKER=importer --tag vector-kb-app:importer-exclude-pdf-20260920-r1 --file {REMOTE_DIR}/Dockerfile.worker-exclude-pdf {REMOTE_DIR}
docker build --pull=false --build-arg BASE_IMAGE="$embed_base" --build-arg WORKER=embedder --tag vector-kb-app:embedder-exclude-pdf-20260920-r1 --file {REMOTE_DIR}/Dockerfile.worker-exclude-pdf {REMOTE_DIR}
docker run --rm --entrypoint python vector-kb-app:importer-exclude-pdf-20260920-r1 -c 'import app.workers.importer'
docker run --rm --entrypoint python vector-kb-app:embedder-exclude-pdf-20260920-r1 -c 'import app.workers.embedder'
bash {REMOTE_DIR}/recreate.sh "$import_container" vector-kb-app:importer-exclude-pdf-20260920-r1 exclude-pdf-importer-20260920-r1 {REMOTE_DIR}/worker-exclude-pdf.env
bash {REMOTE_DIR}/recreate.sh "$embed_container" vector-kb-app:embedder-exclude-pdf-20260920-r1 exclude-pdf-embedder-20260920-r1 {REMOTE_DIR}/worker-exclude-pdf.env
docker exec "$import_container" python -c "import os; from pathlib import Path; source=Path('/app/app/workers/importer.py').read_text(); assert os.environ['WORKER_EXCLUDE_PDF'] == '1'; assert 'NOT :exclude_pdf' in source; print('importer PDF exclusion verified')"
docker exec "$embed_container" python -c "import os; from pathlib import Path; source=Path('/app/app/workers/embedder.py').read_text(); assert os.environ['WORKER_EXCLUDE_PDF'] == '1'; assert 'JOIN documents d' in source; print('embedder PDF exclusion verified')"
docker ps --filter name='^/vector-kb-(importer|embedder)-release$' --format '{{{{.Names}}}} {{{{.Image}}}} {{{{.Status}}}}'
"""
        run(client, command)
    finally:
        client.close()


if __name__ == "__main__":
    main()
