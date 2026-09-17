from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob
from app.models.file_resource import FileResource
from app.services import file_resources
from app.services.doc_conversion import convert_doc
from app.services.import_uploads import staging_path
from app.services.object_storage import build_object_storage_adapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] doc-converter[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def _reset_stale_jobs(db: AsyncSession) -> int:
    params = {
        "seconds": str(settings.doc_converter_stale_seconds),
        "max_attempts": settings.doc_converter_max_attempts,
    }
    failed = await db.execute(
        text(
            """
            UPDATE document_import_jobs
            SET status = 'failed', worker_id = NULL, claimed_at = NULL,
                finished_at = NOW(),
                last_error = COALESCE(last_error, 'stale DOC conversion at max attempts')
            WHERE status = 'processing'
              AND current_stage = 'converting'
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND conversion_attempt_count >= :max_attempts
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
              AND current_stage = 'converting'
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND conversion_attempt_count < :max_attempts
            """
        ),
        params,
    )
    await db.commit()
    return (failed.rowcount or 0) + (reset.rowcount or 0)


async def _claim_jobs(
    db: AsyncSession,
    worker_id: str,
    limit: int,
) -> list[uuid.UUID]:
    result = await db.execute(
        text(
            """
            WITH picked AS (
                SELECT id
                FROM document_import_jobs
                WHERE status = 'queued'
                  AND LOWER(file_name) LIKE '%.doc'
                  AND conversion_sha256 IS NULL
                  AND conversion_attempt_count < :max_attempts
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE document_import_jobs AS jobs
            SET status = 'processing', current_stage = 'converting',
                worker_id = :worker_id, claimed_at = NOW(),
                conversion_attempt_count = jobs.conversion_attempt_count + 1,
                last_error = NULL
            FROM picked
            WHERE jobs.id = picked.id
            RETURNING jobs.id
            """
        ),
        {
            "max_attempts": settings.doc_converter_max_attempts,
            "limit": limit,
            "worker_id": worker_id,
        },
    )
    await db.commit()
    return [row.id for row in result]


async def _mark_failed(job_id: uuid.UUID, worker_id: str, error: Exception) -> None:
    message = str(error).strip() or error.__class__.__name__
    async with async_session_factory() as db:
        job = await db.get(DocumentImportJob, job_id)
        if (
            job is None
            or job.status != "processing"
            or job.current_stage != "converting"
            or job.worker_id != worker_id
        ):
            return
        exhausted = job.conversion_attempt_count >= settings.doc_converter_max_attempts
        job.status = "failed" if exhausted else "queued"
        job.current_stage = "converting" if exhausted else "queued"
        job.worker_id = None
        job.claimed_at = None
        job.finished_at = datetime.now(timezone.utc) if exhausted else None
        job.last_error = message[:4000]
        await db.commit()


async def _process_claimed_job(job_id: uuid.UUID, worker_id: str) -> None:
    resource_source: Path | None = None
    try:
        async with async_session_factory() as read_db:
            job = await read_db.get(DocumentImportJob, job_id)
            if (
                job is None
                or job.status != "processing"
                or job.current_stage != "converting"
                or job.worker_id != worker_id
            ):
                return
            file_resource_id = getattr(job, "file_resource_id", None)
            if file_resource_id is None:
                source = staging_path(job.staging_key)
            else:
                resource = await read_db.get(FileResource, file_resource_id)
                if (
                    resource is None
                    or resource.library_id != job.library_id
                    or resource.storage_status != "available"
                ):
                    raise RuntimeError("file resource is unavailable")
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".doc-resource-",
                    suffix=".doc",
                )
                os.close(descriptor)
                resource_source = Path(temporary_name)
                await file_resources.materialize_file_resource(
                    adapter=build_object_storage_adapter(
                        provider=resource.storage_provider
                    ),
                    resource=resource,
                    destination_path=resource_source,
                )
                source = resource_source
            staging_key = job.staging_key
        artifact = await asyncio.to_thread(convert_doc, source, staging_key)
    finally:
        if resource_source is not None:
            resource_source.unlink(missing_ok=True)

    async with async_session_factory() as db:
        job = (
            await db.execute(
                select(DocumentImportJob)
                .where(DocumentImportJob.id == job_id)
                .with_for_update()
            )
        ).scalars().first()
        owns_claim = (
            job is not None
            and job.status == "processing"
            and job.current_stage == "converting"
            and job.worker_id == worker_id
        )
        if not owns_claim:
            if job is None or job.status != "processing" or job.worker_id == worker_id:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not discard DOC artifact for inactive job %s", job_id)
            return
        job.conversion_sha256 = artifact.sha256
        job.converter_version = artifact.converter_version
        job.status = "queued"
        job.current_stage = "conversion_ready"
        job.worker_id = None
        job.claimed_at = None
        job.last_error = None
        await db.commit()


async def _process_claimed_jobs(job_ids: list[uuid.UUID], worker_id: str) -> None:
    async def process(job_id: uuid.UUID) -> None:
        try:
            await _process_claimed_job(job_id, worker_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("DOC conversion job %s failed", job_id)
            await _mark_failed(job_id, worker_id, exc)

    await asyncio.gather(*(process(job_id) for job_id in job_ids))


async def run_once() -> int:
    worker_id = _worker_id()
    async with async_session_factory() as db:
        recovered = await _reset_stale_jobs(db)
        if recovered:
            log.warning("recovered %s stale DOC conversion jobs", recovered)
        job_ids = await _claim_jobs(
            db,
            worker_id,
            settings.doc_converter_concurrency,
        )
    await _process_claimed_jobs(job_ids, worker_id)
    return len(job_ids)


async def run_watch() -> None:
    while True:
        processed = await run_once()
        if not processed:
            await asyncio.sleep(settings.doc_converter_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert legacy DOC imports")
    parser.add_argument("--watch", action="store_true", help="keep polling for work")
    args = parser.parse_args()
    asyncio.run(run_watch() if args.watch else run_once())


if __name__ == "__main__":
    main()
