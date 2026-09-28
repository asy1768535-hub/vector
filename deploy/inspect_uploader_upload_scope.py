"""Read-only folder and upload-count summary for one affected uploader."""

from __future__ import annotations

import base64
import os
import sys

import paramiko


EMAIL = "2933273013@qq.com"
PYTHON = f"""import asyncio
import json
from sqlalchemy import func, select
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob
from app.models.file_resource import FileResource
from app.models.user import User

async def main():
    async with async_session_factory() as db:
        user_id = (await db.execute(select(User.id).where(User.email == '{EMAIL}'))).scalar_one_or_none()
        if user_id is None:
            raise RuntimeError('user_not_found')
        total_jobs = (await db.execute(select(func.count(DocumentImportJob.id)).where(DocumentImportJob.requested_by_user_id == user_id))).scalar_one()
        total_resources = (await db.execute(select(func.count(FileResource.id)).where(FileResource.uploaded_by_user_id == user_id))).scalar_one()
        available_resources = (await db.execute(select(func.count(FileResource.id)).where(FileResource.uploaded_by_user_id == user_id, FileResource.storage_status == 'available'))).scalar_one()
        root_folder = func.nullif(func.split_part(DocumentImportJob.relative_path, '/', 1), '')
        folders = (await db.execute(
            select(root_folder, func.count(DocumentImportJob.id))
            .where(DocumentImportJob.requested_by_user_id == user_id)
            .where(DocumentImportJob.status == 'failed')
            .where(DocumentImportJob.current_stage == 'validating')
            .where(DocumentImportJob.file_resource_id.is_(None))
            .where(DocumentImportJob.last_error.ilike('%ForeignKeyViolationError%'))
            .where(DocumentImportJob.finished_at >= __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()) - __import__('datetime').timedelta(days=1))
            .where(DocumentImportJob.finished_at < __import__('sqlalchemy').func.date_trunc('day', __import__('sqlalchemy').func.now()))
            .group_by(root_folder)
            .order_by(func.count(DocumentImportJob.id).desc(), root_folder.asc().nulls_first())
            .limit(100)
        )).all()
    print(json.dumps({{
        'upload_tasks_total': total_jobs,
        'file_resources_total': total_resources,
        'file_resources_available': available_resources,
        'affected_folders': [{{'folder': folder or '/', 'count': count}} for folder, count in folders],
    }}, ensure_ascii=True))

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
