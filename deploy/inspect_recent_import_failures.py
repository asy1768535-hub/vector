"""Read-only diagnostics for recent import failures, without file contents."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = b"""import asyncio
from sqlalchemy import func, select
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob

async def main():
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(
                DocumentImportJob.current_stage,
                DocumentImportJob.last_error,
                func.count(),
            )
            .where(DocumentImportJob.status == 'failed')
            .where(DocumentImportJob.created_at >= func.now() - __import__('datetime').timedelta(hours=2))
            .group_by(DocumentImportJob.current_stage, DocumentImportJob.last_error)
            .order_by(func.count().desc())
            .limit(8)
        )).all()
    for stage, error, count in rows:
        safe_error = (error or '').replace('\\n', ' ')[:1200]
        print(f'count={count} stage={stage} error={safe_error}')

asyncio.run(main())
"""
COMMAND = (
    "set -euo pipefail; "
    f"printf %s {base64.b64encode(PYTHON).decode('ascii')} | base64 -d | "
    "docker exec -i vector-kb-api-release python -; "
    "docker logs --since 30m --tail 160 vector-kb-importer-release 2>&1 "
    "| grep -Ei 'error|exception|traceback|sqlalchemy|asyncpg' | tail -60 || true"
)


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
        _, stdout, stderr = client.exec_command(COMMAND, timeout=120)
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
