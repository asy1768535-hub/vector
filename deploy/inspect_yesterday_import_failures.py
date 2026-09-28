"""Read-only aggregation of yesterday's failed imports without file contents or names."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = """import asyncio
from collections import Counter
from sqlalchemy import select
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob
from app.models.file_resource import FileResource

def classify(error):
    value = (error or '').lower()
    if 'foreignkeyviolationerror' in value or 'fk_document_import_jobs_file_resource_id' in value:
        return 'file_resource_foreign_key'
    if 'upload_preflight' in value or 'format' in value or '格式' in value:
        return 'upload_preflight_or_format'
    if 'pdf' in value or 'ocr' in value:
        return 'pdf_or_ocr'
    if 'timeout' in value or 'timed out' in value:
        return 'timeout'
    return 'other'

async def main():
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(
                DocumentImportJob.current_stage,
                DocumentImportJob.last_error,
                DocumentImportJob.file_resource_id,
                FileResource.storage_status,
                FileResource.storage_error_code,
            )
            .outerjoin(FileResource, FileResource.id == DocumentImportJob.file_resource_id)
            .where(DocumentImportJob.status == 'failed')
            .where(DocumentImportJob.finished_at >= __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()) - __import__('datetime').timedelta(days=1))
            .where(DocumentImportJob.finished_at < __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()))
        )).all()
    grouped = Counter((stage or 'unknown', classify(error)) for stage, error, _, _, _ in rows)
    linked = sum(resource_id is not None for _, _, resource_id, _, _ in rows)
    storage = Counter((status or 'unlinked', code or 'none') for _, _, _, status, code in rows)
    print(f'yesterday_failed_total={len(rows)} file_resource_linked={linked} file_resource_missing={len(rows) - linked}')
    for (stage, kind), count in sorted(grouped.items(), key=lambda item: (-item[1], item[0])):
        print(f'count={count} stage={stage} error_class={kind}')
    for (status, code), count in sorted(storage.items(), key=lambda item: (-item[1], item[0])):
        print(f'count={count} storage_status={status} storage_error_code={code}')

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
