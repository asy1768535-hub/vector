"""Read one upload job's status without reading file contents or names."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


JOB_ID = "08579c4a-0cbe-5070-966d-fc5ba10a4cfe"
PYTHON = f"""import asyncio
import uuid
from sqlalchemy import select
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob

async def main():
    async with async_session_factory() as db:
        job = (await db.execute(select(DocumentImportJob).where(DocumentImportJob.id == uuid.UUID('{JOB_ID}')))).scalar_one_or_none()
    if job is None:
        raise RuntimeError('job_not_found')
    error_kind = (job.last_error or '').split(':', 1)[0][:120]
    print(f'job_id={{job.id}} status={{job.status}} stage={{job.current_stage}} attempts={{job.attempt_count}} error_kind={{error_kind or "none"}}')

asyncio.run(main())
""".encode("utf-8")
COMMAND = (
    "set -euo pipefail; "
    f"printf %s {base64.b64encode(PYTHON).decode('ascii')} | base64 -d | "
    "docker exec -i vector-kb-api-release python -"
)


def main() -> None:
    password = os.environ.get("VECTOR_KB_SSH_PASSWORD")
    if not password:
        raise SystemExit("VECTOR_KB_SSH_PASSWORD is required")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("10.0.10.2", username="wmh", password=password, timeout=20, look_for_keys=False, allow_agent=False)
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
