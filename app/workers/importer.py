from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import shutil
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_file import DocumentFile
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.schemas.storage import SourceLocatorV1
from app.services import folders as folders_service
from app.services import ingest as ingest_service
from app.services import import_staging_cleanup
from app.services.import_parsing import ParsedImport, parse_import_file
from app.services.import_uploads import (
    folder_path_for_job,
    source_path_for_job,
    staging_path,
)
from app.services.object_storage import build_object_storage_adapter
from app.services.revision_files import (
    bind_prepared_file_object,
    persist_revision_file_capture,
    prepare_managed_file_path,
)
from app.services.evidence_write_path import validate_parser_segments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] importer[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
STAGING_CLEANUP_INTERVAL_SECONDS = 60.0
_last_staging_cleanup_monotonic: float | None = None


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def _reset_stale_jobs(db: AsyncSession) -> int:
    params = {
        "seconds": str(settings.import_worker_stale_seconds),
        "max_attempts": settings.import_worker_max_attempts,
    }
    failed = await db.execute(
        text(
            """
            UPDATE document_import_jobs
            SET status = 'failed', worker_id = NULL, claimed_at = NULL,
                finished_at = NOW(),
                last_error = COALESCE(last_error, 'stale import at max attempts')
            WHERE status = 'processing'
              AND current_stage IN ('validating', 'parsing', 'chunking')
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND attempt_count >= :max_attempts
            """
        ),
        params,
    )
    reset = await db.execute(
        text(
            """
            UPDATE document_import_jobs
            SET status = 'queued', current_stage = 'queued',
                worker_id = NULL, claimed_at = NULL
            WHERE status = 'processing'
              AND current_stage IN ('validating', 'parsing', 'chunking')
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND attempt_count < :max_attempts
            """
        ),
        params,
    )
    await db.commit()
    return (failed.rowcount or 0) + (reset.rowcount or 0)


async def _claim_jobs(
    db: AsyncSession, worker_id: str, limit: int
) -> list[uuid.UUID]:
    result = await db.execute(
        text(
            """
            WITH picked AS (
                SELECT id
                FROM document_import_jobs
                WHERE status = 'queued' AND attempt_count < :max_attempts
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE document_import_jobs AS jobs
            SET status = 'processing', current_stage = 'validating',
                worker_id = :worker_id, claimed_at = NOW(),
                attempt_count = jobs.attempt_count + 1, last_error = NULL
            FROM picked
            WHERE jobs.id = picked.id
            RETURNING jobs.id
            """
        ),
        {
            "max_attempts": settings.import_worker_max_attempts,
            "limit": limit,
            "worker_id": worker_id,
        },
    )
    await db.commit()
    return [row.id for row in result]


async def _set_stage(job_id: uuid.UUID, stage: str) -> None:
    async with async_session_factory() as db:
        job = await db.get(DocumentImportJob, job_id)
        if job is None or job.status != "processing":
            return
        job.current_stage = stage
        await db.commit()


async def _mark_failed(job_id: uuid.UUID, error: Exception) -> None:
    message = str(error).strip() or error.__class__.__name__
    async with async_session_factory() as db:
        job = await db.get(DocumentImportJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.worker_id = None
        job.claimed_at = None
        job.finished_at = datetime.now(timezone.utc)
        job.last_error = message[:4000]
        await db.commit()


async def _maybe_cleanup_staging(db: AsyncSession) -> tuple[int, int]:
    global _last_staging_cleanup_monotonic
    current = time.monotonic()
    if (
        _last_staging_cleanup_monotonic is not None
        and current - _last_staging_cleanup_monotonic < STAGING_CLEANUP_INTERVAL_SECONDS
    ):
        return 0, 0
    _last_staging_cleanup_monotonic = current
    try:
        transitioned, staging_keys = await import_staging_cleanup.cleanup_staging(db)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        log.exception("import staging cleanup failed")
        return 0, 0

    removed = 0
    for staging_key in staging_keys:
        try:
            unlink_result = await import_staging_cleanup._unlink_staging_key(
                staging_key,
                settings,
            )
        except Exception:  # noqa: BLE001
            log.exception("staging cleanup unlink failed key=%s", staging_key)
            continue
        if unlink_result == "removed":
            removed += 1
        elif unlink_result == "missing":
            log.info("staging cleanup file already missing key=%s", staging_key)
        else:
            log.warning(
                "staging cleanup unlink failed key=%s result=%s",
                staging_key,
                unlink_result,
            )
    if transitioned or staging_keys:
        log.info(
            "staging cleanup transitioned=%s removed=%s attempted=%s",
            transitioned,
            removed,
            len(staging_keys),
        )
    return transitioned, removed


def _text_hash(parsed: ParsedImport) -> str:
    return hashlib.sha256(parsed.normalized_text.encode("utf-8")).hexdigest()


async def _target_document(
    db: AsyncSession, job: DocumentImportJob, source_path: str | None
) -> Document | None:
    if job.replace_document_id is not None:
        stmt = select(Document).where(
            Document.id == job.replace_document_id,
            Document.library_id == job.library_id,
            Document.deleted_at.is_(None),
        )
    elif source_path is not None:
        stmt = select(Document).where(
            Document.library_id == job.library_id,
            Document.source_path == source_path,
            Document.deleted_at.is_(None),
        )
    else:
        return None
    return (await db.execute(stmt.with_for_update())).scalars().first()


def _legacy_files_root() -> Path:
    configured = Path(settings.document_files_dir)
    return configured if configured.is_absolute() else BASE_DIR / configured


async def _persist_legacy_file(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    source: Path,
) -> None:
    digest = job.sha256 or ""
    suffix = source.suffix.lower()
    destination = (
        _legacy_files_root()
        / str(job.library_id)
        / str(document.id)
        / str(document.current_revision)
        / f"{digest}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, source, destination)
    values = {
        "revision": document.current_revision,
        "file_name": job.file_name,
        "content_type": job.content_type,
        "storage_path": str(destination.relative_to(_legacy_files_root())),
        "size_bytes": job.size_bytes,
        "sha256": digest,
    }
    row = await db.get(DocumentFile, document.id)
    if row is None:
        db.add(DocumentFile(document_id=document.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


async def _persist_revision_file(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    source: Path,
    revision_id: uuid.UUID | None,
    file_id: uuid.UUID | None = None,
) -> DocumentRevisionFile | None:
    if not settings.revision_file_storage_enabled:
        await _persist_legacy_file(db, job=job, document=document, source=source)
        return None
    if revision_id is None:
        raise RuntimeError("revision file storage requires a DocumentRevision")
    prepared = await prepare_managed_file_path(
        adapter=build_object_storage_adapter(),
        library_id=job.library_id,
        file_name=job.file_name,
        content_type=job.content_type,
        source_path=source,
        expected_sha256=job.sha256,
        source_locator=SourceLocatorV1(kind="upload"),
    )
    return await persist_revision_file_capture(
        db,
        prepared=bind_prepared_file_object(
            prepared,
            document_id=document.id,
            document_revision_id=revision_id,
            file_id=file_id,
        ),
        )


async def _apply_import_revision_scope(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    revision_id: uuid.UUID | None,
) -> None:
    if job.security_level is not None:
        document.security_level = job.security_level
    if revision_id is None:
        return
    revision = await db.get(DocumentRevision, revision_id)
    if revision is None:
        return
    if job.security_level is not None:
        revision.security_level = job.security_level
    if job.graph_extraction_requested:
        revision.parser_config = {
            **(revision.parser_config or {}),
            "graph_extraction_requested": True,
        }


async def _process_claimed_job(job_id: uuid.UUID) -> None:
    await _set_stage(job_id, "parsing")
    async with async_session_factory() as read_db:
        job = await read_db.get(DocumentImportJob, job_id)
        if job is None:
            return
        library = await read_db.get(Library, job.library_id)
        if library is None or library.deleted_at is not None:
            raise RuntimeError("library is unavailable")
        source = staging_path(job.staging_key)
        if not source.is_file() or source.stat().st_size != job.size_bytes:
            raise RuntimeError("staging file is missing or incomplete")
        parsed = await asyncio.to_thread(
            parse_import_file,
            source,
            library,
            file_name=job.file_name,
        )
    validate_parser_segments(parsed.segments)

    await _set_stage(job_id, "chunking")
    remove_staging = False
    async with async_session_factory() as db:
        async with db.begin():
            job = (
                await db.execute(
                    select(DocumentImportJob)
                    .where(DocumentImportJob.id == job_id)
                    .with_for_update()
                )
            ).scalars().first()
            if job is None or job.status != "processing":
                return
            library = await db.get(Library, job.library_id)
            if library is None or library.deleted_at is not None:
                raise RuntimeError("library is unavailable")
            source_path = source_path_for_job(job)
            target = await _target_document(db, job, source_path)
            content_hash = _text_hash(parsed)
            if (
                target is not None
                and job.replace_document_id is None
                and target.content_hash == content_hash
            ):
                target.folder_id = await folders_service.ensure_folder_path(
                    db, library, folder_path_for_job(job)
                )
                job.status = "succeeded"
                job.current_stage = "completed"
                job.result_operation = "unchanged"
                job.document_id = target.id
                job.document_revision_id = (
                    target.latest_revision_id or target.current_revision_id
                )
                await _apply_import_revision_scope(
                    db,
                    job=job,
                    document=target,
                    revision_id=job.document_revision_id,
                )
                job.finished_at = datetime.now(timezone.utc)
                job.worker_id = None
                job.claimed_at = None
                remove_staging = True
                await db.flush()
                await asyncio.to_thread(source.unlink, missing_ok=True)
                return

            if target is None:
                candidate_revision_file_id = (
                    uuid.uuid4()
                    if settings.enable_evidence_write_path and settings.revision_file_storage_enabled
                    else None
                )
                document, embedding_job, _chunk_count, _existing = (
                    await ingest_service.ingest_text(
                        db=db,
                        library=library,
                        text=parsed.normalized_text,
                        title=job.file_name,
                        external_id=job.external_id,
                        metadata=None,
                        splitter=parsed.splitter_name,
                        created_by=job.requested_by_user_id,
                        security_level=job.security_level,
                        chunks=parsed.chunks,
                        segments=parsed.segments,
                        file_name=job.file_name,
                        raw_file_sha256=job.sha256 if candidate_revision_file_id else None,
                        document_revision_file_id=candidate_revision_file_id,
                        source_path=source_path,
                    )
                )
                operation = "unchanged" if _existing else "created"
                revision_file_id = candidate_revision_file_id if not _existing else None
            else:
                candidate_revision_file_id = (
                    uuid.uuid4()
                    if settings.enable_evidence_write_path and settings.revision_file_storage_enabled
                    else None
                )
                embedding_job, _chunk_count, changed = (
                    await ingest_service.reingest_document(
                        db=db,
                        library=library,
                        document=target,
                        new_text=parsed.normalized_text,
                        title=job.file_name,
                        metadata=None,
                        splitter=parsed.splitter_name,
                        force=job.replace_document_id is not None,
                        chunks=parsed.chunks,
                        segments=parsed.segments,
                        file_name=job.file_name,
                        raw_file_sha256=job.sha256 if candidate_revision_file_id else None,
                        document_revision_file_id=candidate_revision_file_id,
                    )
                )
                document = target
                operation = "updated" if changed else "unchanged"
                revision_file_id = candidate_revision_file_id if changed else None
            if source_path is not None:
                document.source_path = source_path
            document.folder_id = await folders_service.ensure_folder_path(
                db, library, folder_path_for_job(job)
            )
            revision_id = (
                getattr(embedding_job, "document_revision_id", None)
                if embedding_job is not None
                else document.latest_revision_id or document.current_revision_id
            )
            if operation != "unchanged" or not settings.revision_file_storage_enabled:
                await _persist_revision_file(
                    db,
                    job=job,
                    document=document,
                    source=source,
                    revision_id=revision_id,
                    file_id=revision_file_id,
                )
            await _apply_import_revision_scope(
                db,
                job=job,
                document=document,
                revision_id=revision_id,
            )
            job.result_operation = operation
            job.document_id = document.id
            job.document_revision_id = revision_id
            job.embedding_job_id = (
                embedding_job.id if embedding_job is not None else None
            )
            job.status = "processing" if embedding_job is not None else "succeeded"
            job.current_stage = (
                "embedding" if embedding_job is not None else "completed"
            )
            job.worker_id = None
            job.claimed_at = None
            if embedding_job is None:
                job.finished_at = datetime.now(timezone.utc)
            remove_staging = True
    if remove_staging:
        await asyncio.to_thread(source.unlink, missing_ok=True)


async def run_once() -> int:
    worker_id = _worker_id()
    async with async_session_factory() as db:
        recovered = await _reset_stale_jobs(db)
        if recovered:
            log.warning("recovered %s stale import jobs", recovered)
        await _maybe_cleanup_staging(db)
        job_ids = await _claim_jobs(
            db, worker_id, max(1, settings.import_worker_batch_size)
        )
    for job_id in job_ids:
        try:
            await _process_claimed_job(job_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("import job %s failed", job_id)
            await _mark_failed(job_id, exc)
    return len(job_ids)


async def run_watch() -> None:
    while True:
        processed = await run_once()
        if not processed:
            await asyncio.sleep(settings.import_worker_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process uploaded document imports")
    parser.add_argument("--watch", action="store_true", help="keep polling for work")
    args = parser.parse_args()
    asyncio.run(run_watch() if args.watch else run_once())


if __name__ == "__main__":
    main()
