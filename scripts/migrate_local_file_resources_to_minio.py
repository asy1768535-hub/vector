"""Migrate available local FileResource objects to the configured MinIO bucket.

The command is read-only by default.  ``--apply`` uploads each source, verifies
size and SHA-256 by reading it back, then updates that row in PostgreSQL.  Local
files are intentionally retained so the operation is reversible at the storage
layer until a separate cleanup decision is made.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision_file import DocumentRevisionFile
from app.models.file_resource import FileResource
from app.schemas.storage import SourceLocatorV1, StorageLocatorV1
from app.services.object_storage import build_object_storage_adapter
from app.services.revision_files import (
    PreparedRevisionFileCapture,
    persist_revision_file_capture,
    verify_stored_object,
)


async def _hash_file(path: Path) -> tuple[int, str]:
    def calculate() -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()

    return await asyncio.to_thread(calculate)


def revision_capture_from_resource(
    job: DocumentImportJob, resource: FileResource
) -> PreparedRevisionFileCapture:
    return PreparedRevisionFileCapture(
        library_id=resource.library_id,
        document_id=job.document_id,
        document_revision_id=job.document_revision_id,
        file_name=resource.file_name,
        content_type=resource.content_type,
        size_bytes=resource.size_bytes,
        sha256=resource.sha256,
        locator=StorageLocatorV1(
            provider=resource.storage_provider,
            endpoint_ref=resource.endpoint_ref,
            bucket=resource.bucket,
            object_key=resource.object_key,
            object_version=resource.object_version,
            etag=resource.etag,
            immutability_mode=resource.immutability_mode,
        ),
        managed_snapshot=True,
        source_locator=SourceLocatorV1(kind="upload"),
        verified_at=resource.storage_verified_at,
    )


async def backfill_revision_files(*, apply: bool, remote) -> tuple[int, int]:
    backfilled = 0
    failed = 0
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(DocumentImportJob, FileResource)
                .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
                .join(Document, Document.id == DocumentImportJob.document_id)
                .outerjoin(
                    DocumentRevisionFile,
                    DocumentRevisionFile.document_revision_id
                    == DocumentImportJob.document_revision_id,
                )
                .where(
                    FileResource.storage_provider == "minio",
                    FileResource.storage_status == "available",
                    DocumentImportJob.status == "succeeded",
                    DocumentImportJob.document_revision_id.is_not(None),
                    Document.deleted_at.is_(None),
                    DocumentImportJob.document_revision_id.in_(
                        (Document.current_revision_id, Document.latest_revision_id)
                    ),
                    DocumentRevisionFile.id.is_(None),
                )
                .order_by(DocumentImportJob.id)
            )
        ).all()
        print(f"revision_file_candidates={len(rows)} apply={apply}")
        seen: set[object] = set()
        for job, resource in rows:
            if job.document_revision_id in seen:
                continue
            seen.add(job.document_revision_id)
            try:
                prepared = revision_capture_from_resource(job, resource)
                if apply:
                    await verify_stored_object(
                        adapter=remote,
                        locator=prepared.locator,
                        expected_sha256=prepared.sha256,
                        expected_size_bytes=prepared.size_bytes,
                    )
                    await persist_revision_file_capture(db, prepared=prepared)
                    await db.commit()
                backfilled += 1
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                failed += 1
                print(
                    "revision_file_backfill_failed "
                    f"revision={job.document_revision_id} reason={exc.__class__.__name__}"
                )
    print(f"revision_files_backfilled={backfilled} failed={failed}")
    return backfilled, failed


async def migrate(*, apply: bool, limit: int | None) -> int:
    if settings.document_storage_provider != "minio":
        raise RuntimeError("MINIO_ENDPOINT must configure document storage as minio")

    remote = build_object_storage_adapter(settings, provider="minio")
    local = build_object_storage_adapter(settings, provider="local")
    migrated = 0
    failed = 0

    async with async_session_factory() as db:
        statement = (
            select(FileResource)
            .where(
                FileResource.storage_provider == "local",
                FileResource.storage_status == "available",
            )
            .order_by(FileResource.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        resources = list((await db.execute(statement)).scalars().all())
        print(f"candidates={len(resources)} apply={apply}")

        for resource in resources:
            try:
                key = resource.object_key or resource.storage_path
                source = local.path_for_read(key)
                size, sha256 = await _hash_file(source)
                if size != resource.size_bytes or sha256 != resource.sha256:
                    raise RuntimeError("local source integrity mismatch")

                if not apply:
                    migrated += 1
                    continue

                version = await remote.put_file(key, source, resource.content_type)
                locator = StorageLocatorV1(
                    provider=remote.provider,
                    endpoint_ref=remote.endpoint_ref,
                    bucket=remote.bucket,
                    object_key=key,
                    object_version=version.object_version,
                    etag=version.etag,
                    immutability_mode=(
                        "version_id" if version.object_version else "content_hash"
                    ),
                )
                await verify_stored_object(
                    adapter=remote,
                    locator=locator,
                    expected_sha256=resource.sha256,
                    expected_size_bytes=resource.size_bytes,
                )

                resource.storage_provider = remote.provider
                resource.endpoint_ref = remote.endpoint_ref
                resource.bucket = remote.bucket
                resource.object_key = key
                resource.storage_path = key
                resource.object_version = version.object_version
                resource.etag = version.etag
                resource.immutability_mode = locator.immutability_mode
                resource.storage_verified_at = datetime.now(timezone.utc)
                resource.storage_error_code = None
                await db.commit()
                migrated += 1
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                failed += 1
                print(f"migration_failed resource={resource.id} reason={exc.__class__.__name__}")

    print(f"migrated={migrated} failed={failed}")
    _, backfill_failed = await backfill_revision_files(apply=apply, remote=remote)
    return 1 if failed or backfill_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(migrate(apply=args.apply, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
