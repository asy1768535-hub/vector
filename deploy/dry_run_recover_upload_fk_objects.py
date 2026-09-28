"""Read-only MinIO inventory for upload-FK failures, without file contents or names."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


PYTHON = """import asyncio
from collections import Counter
from sqlalchemy import select
from app.config import settings
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob
from app.services.file_resources import resource_object_key
from app.services.object_storage import build_object_storage_adapter
from app.services.object_storage_contracts import ObjectStorageError
from app.services.import_uploads import staging_path

async def main():
    async with async_session_factory() as db:
        jobs = (await db.execute(
            select(DocumentImportJob.id, DocumentImportJob.library_id, DocumentImportJob.file_name, DocumentImportJob.size_bytes, DocumentImportJob.staging_key)
            .where(DocumentImportJob.status == 'failed')
            .where(DocumentImportJob.current_stage == 'validating')
            .where(DocumentImportJob.file_resource_id.is_(None))
            .where(DocumentImportJob.last_error.ilike('%ForeignKeyViolationError%'))
            .where(DocumentImportJob.finished_at >= __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()) - __import__('datetime').timedelta(days=1))
            .where(DocumentImportJob.finished_at < __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()))
        )).all()
    adapter = build_object_storage_adapter(settings)
    outcomes = Counter()
    limit = asyncio.Semaphore(16)
    async def inspect(job):
        job_id, library_id, file_name, size_bytes, staging_key = job
        key = resource_object_key(library_id, job_id, file_name)
        path = staging_path(staging_key, settings)
        staging = 'staging_size_match' if path.is_file() and path.stat().st_size == size_bytes else ('staging_missing' if not path.is_file() else 'staging_size_mismatch')
        try:
            async with limit:
                stat = await adapter.stat(key, None)
        except ObjectStorageError as exc:
            return exc.code, staging
        return ('present_size_match' if stat.size_bytes == size_bytes else 'present_size_mismatch'), staging
    outcomes.update(await asyncio.gather(*(inspect(job) for job in jobs)))
    print(f'provider={adapter.provider} candidate_jobs={len(jobs)}')
    for (object_check, staging_check), count in sorted(outcomes.items(), key=lambda item: (-item[1], item[0])):
        print(f'count={count} object_check={object_check} staging_check={staging_check}')

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
        _, stdout, stderr = client.exec_command(COMMAND, timeout=600)
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
