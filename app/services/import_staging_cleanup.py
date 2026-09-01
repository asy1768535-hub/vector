from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.document_import_job import DocumentImportJob
from app.services.import_uploads import (
    EXPIRED_UPLOAD_CLEANUP_COMPLETE,
    EXPIRED_UPLOAD_CLEANUP_PENDING,
    UPLOAD_PREFLIGHT_ERROR_PREFIX,
    ImportUploadError,
    _encode_upload_preflight_error,
    decode_upload_preflight_error,
    staging_path,
)


MAX_CLEANUP_BATCH = 256
log = logging.getLogger(__name__)
UnlinkResult = Literal["removed", "missing", "failed", "invalid"]
CleanupPlan = tuple[int, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PendingPreflightCleanup:
    job_id: uuid.UUID
    staging_key: str
    pending_marker: str
    complete_marker: str


@dataclass(frozen=True, slots=True)
class PendingExpiredCleanup:
    job_id: uuid.UUID
    staging_key: str


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


async def _unlink_staging_key(
    staging_key: str,
    config: Settings,
) -> UnlinkResult:
    try:
        from app.services.doc_conversion import converted_staging_path

        path = staging_path(staging_key, config)
        converted = converted_staging_path(staging_key, config)
    except ImportUploadError:
        return "invalid"
    removed = False
    for candidate in (path, converted):
        try:
            await asyncio.to_thread(candidate.unlink)
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            return "failed"
    return "removed" if removed else "missing"


async def cleanup_staging(
    db: AsyncSession,
    *,
    config: Settings = settings,
    batch_size: int = MAX_CLEANUP_BATCH,
    now: datetime | None = None,
) -> CleanupPlan:
    """Bounded, database-driven staging retention.

    Orphan and crashed terminal files are deliberately left alone: scanning
    the staging directory is unbounded and cannot be made safe in the importer
    hot loop. Only stale uploading rows need a state transition, so they are
    selected with SKIP LOCKED and their bounded set of keys is returned to the
    worker for deletion after the transaction commits.
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
        job.last_error = EXPIRED_UPLOAD_CLEANUP_PENDING
        transitioned += 1
    if stale_uploads:
        await db.flush()

    return transitioned, tuple(job.staging_key for job in stale_uploads)


async def reconcile_pending_expired_cleanups(
    db: AsyncSession,
    *,
    config: Settings = settings,
    batch_size: int = MAX_CLEANUP_BATCH,
) -> tuple[int, int]:
    """Retry expired-upload deletion and release quota only after it is gone."""
    batch_size = _positive_limit(
        batch_size,
        "staging cleanup batch size",
        maximum=MAX_CLEANUP_BATCH,
    )
    jobs = list(
        (
            await db.execute(
                select(DocumentImportJob)
                .where(
                    DocumentImportJob.status == "cancelled",
                    DocumentImportJob.last_error == EXPIRED_UPLOAD_CLEANUP_PENDING,
                )
                .order_by(DocumentImportJob.updated_at, DocumentImportJob.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        PendingExpiredCleanup(job_id=job.id, staging_key=job.staging_key)
        for job in jobs
    ]
    await db.commit()

    ready: list[PendingExpiredCleanup] = []
    retry_later: list[PendingExpiredCleanup] = []
    removed = 0
    for candidate in candidates:
        unlink_result = await _unlink_staging_key(candidate.staging_key, config)
        if unlink_result in {"removed", "missing"}:
            ready.append(candidate)
            if unlink_result == "removed":
                removed += 1
        else:
            retry_later.append(candidate)
            log.warning(
                "expired upload staging cleanup failed key=%s result=%s",
                candidate.staging_key,
                unlink_result,
            )

    reconciled = 0
    for candidate in ready:
        result = await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == candidate.job_id,
                DocumentImportJob.status == "cancelled",
                DocumentImportJob.last_error == EXPIRED_UPLOAD_CLEANUP_PENDING,
            )
            .values(last_error=EXPIRED_UPLOAD_CLEANUP_COMPLETE)
            .returning(DocumentImportJob.id)
        )
        if result.scalar_one_or_none() is not None:
            reconciled += 1
    for candidate in retry_later:
        result = await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == candidate.job_id,
                DocumentImportJob.status == "cancelled",
                DocumentImportJob.last_error == EXPIRED_UPLOAD_CLEANUP_PENDING,
            )
            .values(updated_at=func.now())
            .returning(DocumentImportJob.id)
        )
        result.scalar_one_or_none()
    if ready or retry_later:
        await db.commit()
    return reconciled, removed


async def reconcile_pending_preflight_cleanups(
    db: AsyncSession,
    *,
    config: Settings = settings,
    batch_size: int = MAX_CLEANUP_BATCH,
) -> tuple[int, int]:
    """Finish bounded preflight cleanup without holding a transaction over file I/O."""
    batch_size = _positive_limit(
        batch_size,
        "staging cleanup batch size",
        maximum=MAX_CLEANUP_BATCH,
    )
    jobs = list(
        (
            await db.execute(
                select(DocumentImportJob)
                .where(
                    DocumentImportJob.status == "failed",
                    DocumentImportJob.current_stage == "completed",
                    DocumentImportJob.last_error.like(
                        f"{UPLOAD_PREFLIGHT_ERROR_PREFIX}cleanup_pending:%"
                    ),
                )
                .order_by(DocumentImportJob.updated_at, DocumentImportJob.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    candidates: list[PendingPreflightCleanup] = []
    for job in jobs:
        decoded = decode_upload_preflight_error(job.last_error)
        if decoded is None or decoded[0] != "cleanup_pending":
            continue
        candidates.append(
            PendingPreflightCleanup(
                job_id=job.id,
                staging_key=job.staging_key,
                pending_marker=job.last_error,
                complete_marker=_encode_upload_preflight_error(
                    decoded[1],
                    cleanup_state="cleanup_complete",
                ),
            )
        )
    await db.commit()

    ready: list[PendingPreflightCleanup] = []
    retry_later: list[PendingPreflightCleanup] = []
    removed = 0
    for candidate in candidates:
        unlink_result = await _unlink_staging_key(candidate.staging_key, config)
        if unlink_result in {"removed", "missing"}:
            ready.append(candidate)
            if unlink_result == "removed":
                removed += 1
        else:
            retry_later.append(candidate)
            log.warning(
                "upload preflight staging cleanup failed key=%s result=%s",
                candidate.staging_key,
                unlink_result,
            )

    reconciled = 0
    for candidate in ready:
        result = await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == candidate.job_id,
                DocumentImportJob.status == "failed",
                DocumentImportJob.current_stage == "completed",
                DocumentImportJob.last_error == candidate.pending_marker,
            )
            .values(last_error=candidate.complete_marker)
            .returning(DocumentImportJob.id)
        )
        if result.scalar_one_or_none() is not None:
            reconciled += 1
    for candidate in retry_later:
        result = await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == candidate.job_id,
                DocumentImportJob.status == "failed",
                DocumentImportJob.current_stage == "completed",
                DocumentImportJob.last_error == candidate.pending_marker,
            )
            .values(updated_at=func.now())
            .returning(DocumentImportJob.id)
        )
        result.scalar_one_or_none()
    if ready or retry_later:
        await db.commit()
    return reconciled, removed
