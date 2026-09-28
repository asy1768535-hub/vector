"""Read-only count of live embedding queue states on the production host."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = b"""import asyncio
from sqlalchemy import case, func, or_, select
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob

async def main():
    async with async_session_factory() as db:
        is_pdf = or_(
            func.lower(func.coalesce(Document.source_path, '')).like('%.pdf'),
            func.lower(func.coalesce(Document.title, '')).like('%.pdf'),
        )
        rows = (await db.execute(
            select(
                EmbeddingJob.status,
                case((is_pdf, 'pdf'), else_='non_pdf').label('file_kind'),
                func.count(),
            )
            .join(Document, Document.id == EmbeddingJob.document_id)
            .group_by(EmbeddingJob.status, 'file_kind')
        )).all()
        imports = (await db.execute(
            select(DocumentImportJob.current_stage, func.count())
            .where(DocumentImportJob.status == 'processing')
            .group_by(DocumentImportJob.current_stage)
        )).all()
    print('import-processing ' + ' '.join(
        f'{stage}={count}' for stage, count in sorted(imports)
    ))
    print(' '.join(
        f'{status}:{file_kind}={count}'
        for status, file_kind, count in sorted(rows)
    ))

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
