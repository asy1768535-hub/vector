"""Publish the narrow upload file-resource linkage hotfix over SSH."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import paramiko


HOST = "10.0.10.2"
USERNAME = "wmh"
CONTAINER = "vector-kb-api-release"
IMAGE = "vector-kb-app:file-resource-link-upsert-20260920-r1"
SUFFIX = "file-resource-link-upsert-20260920-r1"
REMOTE_DIR = "/tmp/vector-kb-file-resource-link-upsert-20260920-r1"
HERE = Path(__file__).resolve().parent
REQUIRED_FILES = (
    "Dockerfile.file-resource-link-fix",
    "patch_file_resource_link.py",
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
        raise RuntimeError("remote deployment command failed")


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
            for name in REQUIRED_FILES:
                sftp.put(str(HERE / name), f"{REMOTE_DIR}/{name}")
        finally:
            sftp.close()

        command = f"""set -euo pipefail
base_image=$(docker inspect --format '{{{{.Config.Image}}}}' {CONTAINER})
test -n "$base_image"
echo "Active base image: $base_image"
tr -d '\\r' < {REMOTE_DIR}/recreate_container_from_existing.sh > {REMOTE_DIR}/recreate.sh
chmod 700 {REMOTE_DIR}/recreate.sh
docker build --pull=false --build-arg BASE_IMAGE="$base_image" --tag {IMAGE} --file {REMOTE_DIR}/Dockerfile.file-resource-link-fix {REMOTE_DIR}
docker run --rm --entrypoint python {IMAGE} -c "import app.services.import_uploads"
bash {REMOTE_DIR}/recreate.sh {CONTAINER} {IMAGE} {SUFFIX}
ready=0
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error http://127.0.0.1:8200/health/ready >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  rollback={CONTAINER}-rollback-{SUFFIX}
  docker rm -f {CONTAINER} >/dev/null 2>&1 || true
  docker rename "$rollback" {CONTAINER}
  docker start {CONTAINER}
  echo "Health check failed; restored rollback container" >&2
  exit 1
fi
docker exec {CONTAINER} python -c "from pathlib import Path; source=Path('/app/app/services/import_uploads.py').read_text(); assert 'pg_insert(FileResource)' in source; assert '.on_conflict_do_update(' in source; print('atomic file-resource upsert verified')"
docker ps --filter name='^/{CONTAINER}$' --format 'Active container: {{{{.Names}}}} {{{{.Image}}}} {{{{.Status}}}}'
"""
        run(client, command)
    finally:
        client.close()


if __name__ == "__main__":
    main()
