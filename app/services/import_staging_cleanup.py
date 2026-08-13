from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.document_import_job import DocumentImportJob
from app.services.import_uploads import ImportUploadError, staging_path


MAX_CLEANUP_BATCH = 256
# Failed imports remain retryable while their original staging file exists.
_TERMINAL_STATUSES = ("succeeded", "cancelled", "superseded")


def _positive_limit(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ImportUploadError(
            "staging_quota_unavailable",
            f"{name} is invalid",
            status_code=503,
        )
    if maximum is not None and value > maximum:
        raise ImportUploadError(
            "staging_quota_unavailable",
            f"{name} is invalid",
            status_code=503,
        )
    return value


async def _unlink_staging_key(staging_key: str, config: Settings) -> bool:
    try:
        path = staging_path(staging_key, config)
    except ImportUploadError:
        return False
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        return False
    return True


async def cleanup_staging(
    db: AsyncSession,
    *,
    config: Settings = settings,
    batch_size: int = MAX_CLEANUP_BATCH,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Bounded, database-driven staging retention.

    Orphan files are deliberately left alone: scanning the staging directory
    is unbounded and cannot be made safe in the importer hot loop. Uploading
    rows are the only candidates that need a state transition and therefore
    use SKIP LOCKED; terminal rows only provide bounded file keys.
    """
    retention_seconds = _positive_limit(
        config.import_staging_retention_seconds,
        "staging retention",
    )
    batch_size = _positive_limit(
        batch_size,
        "staging cleanup batch size",
        maximum=MAX_CLEANUP_BATCH,
    )
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=retention_seconds)

    stale_uploads = list(
        (
            await db.execute(
                select(DocumentImportJob)
                .where(
                    DocumentImportJob.status == "uploading",
                    DocumentImportJob.updated_at < cutoff,
                )
                .order_by(DocumentImportJob.updated_at, DocumentImportJob.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    transitioned = 0
    for job in stale_uploads:
        job.status = "cancelled"
        job.finished_at = now or datetime.now(timezone.utc)
        job.last_error = "upload expired before completion"
        transitioned += 1
    if stale_uploads:
        await db.flush()

    terminal_keys = list(
        (
            await db.execute(
                select(DocumentImportJob.staging_key)
                .where(
                    DocumentImportJob.status.in_(_TERMINAL_STATUSES),
                    DocumentImportJob.finished_at.is_not(None),
                    DocumentImportJob.finished_at < cutoff,
                )
                .order_by(DocumentImportJob.finished_at, DocumentImportJob.id)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    for staging_key in terminal_keys:
        if await _unlink_staging_key(staging_key, config):
            removed += 1
    return transitioned, removed
