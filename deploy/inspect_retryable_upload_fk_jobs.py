"""Read retryable upload-FK failures without reading document contents or names."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = b"""import asyncio
from sqlalchemy import select
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob

async def main():
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(
                DocumentImportJob.id,
                DocumentImportJob.library_id,
                DocumentImportJob.attempt_count,
                DocumentImportJob.current_stage,
            )
            .where(DocumentImportJob.status == 'failed')
            .where(DocumentImportJob.current_stage == 'validating')
            .where(DocumentImportJob.last_error.ilike('%ForeignKeyViolationError%'))
            .order_by(DocumentImportJob.created_at.desc())
            .limit(3)
        )).all()
    for row in rows:
        print(f'job_id={row.id} library_id={row.library_id} attempts={row.attempt_count} stage={row.current_stage}')

asyncio.run(main())
"""
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
