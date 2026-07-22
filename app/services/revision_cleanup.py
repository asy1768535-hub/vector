from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision, REVISION_STATUS_READY
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.models.revision_retention import RevisionRetentionRecord
from app.schemas.storage import StorageLocatorV1
from app.services import audit_log
from app.services.revision_retention import project_revision_retention_impact


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RevisionCleanupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class RevisionCleanupClaim:
    record_id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    replacement_revision_id: uuid.UUID
    revision_file_id: uuid.UUID
    claim_token: uuid.UUID
    attempt_count: int
    managed_snapshot: bool
    locator: StorageLocatorV1


@dataclass(frozen=True, slots=True)
class RevisionCleanupResult:
    record_id: uuid.UUID
    status: str
    code: str


@dataclass(frozen=True, slots=True)
class LockedCleanupScope:
    library: Library
    record: RevisionRetentionRecord
    document: Document
    old_revision: DocumentRevision
    replacement_revision: DocumentRevision
    revision_file: DocumentRevisionFile


def cleanup_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RevisionCleanupError(
            "cleanup_time_invalid", "cleanup time must be timezone-aware"
        )
    return current


def cleanup_batch_limit(value: int | None, config: Settings) -> int:
    limit = config.revision_cleanup_batch_size if value is None else value
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RevisionCleanupError(
            "cleanup_batch_invalid", "cleanup batch limit is invalid"
        )
    return limit


def require_cleanup_worker_id(value: str) -> str:
    if not isinstance(value, str) or not _WORKER_ID.fullmatch(value):
        raise RevisionCleanupError(
            "cleanup_worker_invalid", "worker identity is invalid"
        )
    return value


def bounded_cleanup_error_code(value: str) -> str:
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        return "cleanup_failed"
    return value


async def _lock_library(db, library_id: uuid.UUID) -> Library | None:
    return (
        await db.execute(
            select(Library)
            .where(Library.id == library_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


async def lock_cleanup_scope(
    db,
    record_id: uuid.UUID,
) -> LockedCleanupScope | None:
    initial = (
        await db.execute(
            select(
                RevisionRetentionRecord.id,
                RevisionRetentionRecord.library_id,
                RevisionRetentionRecord.document_id,
            ).where(RevisionRetentionRecord.id == record_id)
        )
    ).first()
    if initial is None:
        return None
    library = await _lock_library(db, initial.library_id)
    if library is None:
        return None
    document = (
        await db.execute(
            select(Document)
            .where(Document.id == initial.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if document is None or document.library_id != library.id:
        return None
    record = (
        await db.execute(
            select(RevisionRetentionRecord)
            .where(RevisionRetentionRecord.id == record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if (
        record is None
        or record.library_id != library.id
        or record.document_id != document.id
    ):
        return None
    revisions = list(
        (
            await db.execute(
                select(DocumentRevision)
                .where(
                    DocumentRevision.id.in_(
                        (
                            record.document_revision_id,
                            record.replacement_revision_id,
                        )
                    )
                )
                .order_by(DocumentRevision.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    revisions_by_id = {row.id: row for row in revisions}
    revision_file = (
        await db.execute(
            select(DocumentRevisionFile)
            .where(DocumentRevisionFile.id == record.revision_file_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    old_revision = revisions_by_id.get(record.document_revision_id)
    replacement = revisions_by_id.get(record.replacement_revision_id)
    if (
        document is None
        or old_revision is None
        or replacement is None
        or revision_file is None
    ):
        return None
    return LockedCleanupScope(
        library=library,
        record=record,
        document=document,
        old_revision=old_revision,
        replacement_revision=replacement,
        revision_file=revision_file,
    )


def clear_cleanup_claim(row: RevisionRetentionRecord) -> None:
    row.worker_id = None
    row.claim_token = None
    row.claimed_at = None
    row.lease_expires_at = None


def stop_cleanup_governance(
    row: RevisionRetentionRecord,
    *,
    code: str,
    cancelled: bool = False,
) -> None:
    row.status = "cancelled" if cancelled else "blocked"
    row.block_code = code[:64]
    row.available_at = None
    row.finished_at = None
    row.last_error_code = None
    clear_cleanup_claim(row)


def cleanup_scope_error(
    scope: LockedCleanupScope,
    current_time: datetime,
) -> tuple[str, bool] | None:
    library = scope.library
    row = scope.record
    document = scope.document
    old_revision = scope.old_revision
    replacement = scope.replacement_revision
    revision_file = scope.revision_file
    if library.deleted_at is not None:
        return "library_unavailable", False
    if library.lifecycle_mode != "managed" or not library.revision_retention_enabled:
        return "library_policy_disabled", False
    if document.library_id != library.id or document.id != row.document_id:
        return "retention_scope_missing", False
    if document.deleted_at is not None:
        return "document_deleted", True
    if document.current_revision_id != row.replacement_revision_id:
        return "replacement_not_current", False
    if (
        old_revision.library_id != library.id
        or old_revision.document_id != document.id
        or old_revision.status != "superseded"
    ):
        return "revision_not_superseded", True
    if (
        replacement.library_id != library.id
        or replacement.document_id != document.id
        or replacement.status != REVISION_STATUS_READY
    ):
        return "replacement_not_ready", False
    if (
        revision_file.library_id != library.id
        or revision_file.document_id != document.id
        or revision_file.document_revision_id != old_revision.id
        or revision_file.id != row.revision_file_id
    ):
        return "revision_file_unavailable", False
    if revision_file.lifecycle_status not in {"available", "deleting"}:
        return "revision_file_lifecycle_invalid", False
    if current_time < row.cleanup_not_before:
        return "cleanup_not_due", False
    return None


async def cleanup_has_dependencies(
    db,
    scope: LockedCleanupScope,
    config: Settings,
) -> bool:
    impact = await project_revision_retention_impact(
        db,
        library_id=scope.record.library_id,
        document_revision_id=scope.record.document_revision_id,
        config=config,
    )
    return impact.has_dependencies


def cleanup_claim_from_scope(scope: LockedCleanupScope) -> RevisionCleanupClaim:
    row = scope.record
    revision_file = scope.revision_file
    if row.claim_token is None:
        raise RevisionCleanupError(
            "cleanup_claim_invalid", "cleanup claim token is unavailable"
        )
    try:
        locator = StorageLocatorV1(
            provider=revision_file.storage_provider,
            endpoint_ref=revision_file.endpoint_ref,
            bucket=revision_file.bucket,
            object_key=revision_file.object_key,
            object_version=revision_file.object_version,
            etag=revision_file.etag,
            immutability_mode=revision_file.immutability_mode,
        )
    except (TypeError, ValueError):
        raise RevisionCleanupError(
            "revision_file_locator_invalid", "revision file locator is invalid"
        ) from None
    return RevisionCleanupClaim(
        record_id=row.id,
        library_id=row.library_id,
        document_id=row.document_id,
        document_revision_id=row.document_revision_id,
        replacement_revision_id=row.replacement_revision_id,
        revision_file_id=revision_file.id,
        claim_token=row.claim_token,
        attempt_count=row.attempt_count,
        managed_snapshot=revision_file.managed_snapshot,
        locator=locator,
    )


async def queue_eligible_revision_cleanups(
    db,
    *,
    at: datetime | None = None,
    batch_limit: int | None = None,
    config: Settings = settings,
) -> tuple[uuid.UUID, ...]:
    if not config.revision_cleanup_enabled:
        return ()
    current_time = cleanup_now(at)
    limit = cleanup_batch_limit(batch_limit, config)
    candidate_ids = tuple(
        (
            await db.execute(
                select(RevisionRetentionRecord.id)
                .where(RevisionRetentionRecord.status == "eligible")
                .order_by(
                    RevisionRetentionRecord.library_id,
                    RevisionRetentionRecord.id,
                )
                .limit(limit)
            )
        ).scalars().all()
    )
    queued: list[uuid.UUID] = []
    for record_id in candidate_ids:
        scope = await lock_cleanup_scope(db, record_id)
        if scope is None or scope.record.status != "eligible":
            continue
        if reconcile_terminal_revision_file(scope):
            continue
        problem = cleanup_scope_error(scope, current_time)
        if problem is not None:
            stop_cleanup_governance(
                scope.record,
                code=problem[0],
                cancelled=problem[1],
            )
            continue
        if await cleanup_has_dependencies(db, scope, config):
            stop_cleanup_governance(scope.record, code="active_graph_dependency")
            continue
        scope.record.status = "queued"
        scope.record.block_code = None
        scope.record.available_at = current_time
        scope.record.last_error_code = None
        queued.append(scope.record.id)
    return tuple(queued)


async def claim_revision_cleanup(
    db,
    *,
    record_id: uuid.UUID,
    worker_id: str,
    at: datetime | None = None,
    config: Settings = settings,
) -> RevisionCleanupClaim | None:
    if not config.revision_cleanup_enabled:
        return None
    worker_id = require_cleanup_worker_id(worker_id)
    current_time = cleanup_now(at)
    scope = await lock_cleanup_scope(db, record_id)
    if scope is None:
        return None
    if reconcile_terminal_revision_file(scope):
        return None
    row = scope.record
    claimable = (
        row.status in {"queued", "failed"}
        and row.available_at is not None
        and row.available_at <= current_time
    ) or (
        row.status == "processing"
        and row.lease_expires_at is not None
        and row.lease_expires_at <= current_time
    )
    if not claimable or row.attempt_count >= config.revision_cleanup_max_attempts:
        return None
    problem = cleanup_scope_error(scope, current_time)
    if problem is not None:
        stop_cleanup_governance(row, code=problem[0], cancelled=problem[1])
        return None
    if await cleanup_has_dependencies(db, scope, config):
        stop_cleanup_governance(row, code="active_graph_dependency")
        return None
    row.status = "processing"
    row.block_code = None
    row.attempt_count += 1
    row.available_at = None
    row.worker_id = worker_id
    row.claim_token = uuid.uuid4()
    row.claimed_at = current_time
    row.lease_expires_at = current_time + timedelta(
        seconds=config.revision_cleanup_lease_seconds
    )
    row.finished_at = None
    row.last_error_code = None
    scope.revision_file.lifecycle_status = "deleting"
    scope.revision_file.deleted_at = None
    scope.revision_file.delete_verified_at = None
    try:
        return cleanup_claim_from_scope(scope)
    except RevisionCleanupError as exc:
        stop_cleanup_governance(row, code=exc.code)
        return None


def require_live_cleanup_claim(
    scope: LockedCleanupScope,
    claim: RevisionCleanupClaim,
) -> None:
    row = scope.record
    revision_file = scope.revision_file
    if (
        row.status != "processing"
        or row.claim_token != claim.claim_token
        or row.id != claim.record_id
        or row.library_id != claim.library_id
        or row.document_id != claim.document_id
        or row.document_revision_id != claim.document_revision_id
        or row.replacement_revision_id != claim.replacement_revision_id
        or revision_file.id != claim.revision_file_id
        or revision_file.lifecycle_status != "deleting"
        or revision_file.managed_snapshot != claim.managed_snapshot
    ):
        raise RevisionCleanupError(
            "cleanup_claim_stale", "cleanup claim is no longer authoritative"
        )
    current = cleanup_claim_from_scope(scope)
    if current.locator != claim.locator:
        raise RevisionCleanupError(
            "cleanup_claim_stale", "cleanup object identity changed"
        )


def record_revision_file_retirement(
    scope: LockedCleanupScope,
    claim: RevisionCleanupClaim,
    completed_at: datetime,
) -> None:
    scope.revision_file.lifecycle_status = (
        "deleted" if claim.managed_snapshot else "released"
    )
    scope.revision_file.deleted_at = completed_at
    scope.revision_file.delete_verified_at = (
        completed_at if claim.managed_snapshot else None
    )


def reconcile_terminal_revision_file(scope: LockedCleanupScope) -> bool:
    revision_file = scope.revision_file
    if scope.record.status not in {"eligible", "queued", "failed"} or (
        revision_file.lifecycle_status not in {"deleted", "released"}
    ):
        return False
    completed_at = revision_file.delete_verified_at or revision_file.deleted_at
    if completed_at is None:
        return False
    scope.record.status = "cleaned"
    scope.record.block_code = None
    scope.record.available_at = None
    scope.record.finished_at = completed_at
    scope.record.last_error_code = None
    clear_cleanup_claim(scope.record)
    return True


async def complete_revision_cleanup(
    db,
    *,
    claim: RevisionCleanupClaim,
    at: datetime | None = None,
    config: Settings = settings,
) -> RevisionCleanupResult:
    current_time = cleanup_now(at)
    scope = await lock_cleanup_scope(db, claim.record_id)
    if scope is None:
        raise RevisionCleanupError(
            "cleanup_claim_stale", "cleanup scope is unavailable"
        )
    require_live_cleanup_claim(scope, claim)
    problem = cleanup_scope_error(scope, current_time)
    record_revision_file_retirement(scope, claim, current_time)
    if problem is not None:
        stop_cleanup_governance(
            scope.record,
            code=problem[0],
            cancelled=problem[1],
        )
        return RevisionCleanupResult(scope.record.id, scope.record.status, problem[0])
    if await cleanup_has_dependencies(db, scope, config):
        stop_cleanup_governance(scope.record, code="active_graph_dependency")
        return RevisionCleanupResult(
            scope.record.id,
            scope.record.status,
            "active_graph_dependency",
        )
    scope.record.status = "cleaned"
    scope.record.block_code = None
    scope.record.available_at = None
    scope.record.finished_at = current_time
    scope.record.last_error_code = None
    clear_cleanup_claim(scope.record)
    await audit_log.record(
        db,
        None,
        "revision_cleanup.cleaned",
        {
            "record_id": str(scope.record.id),
            "library_id": str(scope.record.library_id),
            "document_id": str(scope.record.document_id),
            "revision_file_id": str(scope.revision_file.id),
            "attempt_count": scope.record.attempt_count,
            "lifecycle_status": scope.revision_file.lifecycle_status,
        },
    )
    return RevisionCleanupResult(
        scope.record.id,
        "cleaned",
        scope.revision_file.lifecycle_status,
    )


async def fail_revision_cleanup(
    db,
    *,
    claim: RevisionCleanupClaim,
    error_code: str,
    at: datetime | None = None,
    config: Settings = settings,
) -> RevisionCleanupResult:
    current_time = cleanup_now(at)
    code = bounded_cleanup_error_code(error_code)
    scope = await lock_cleanup_scope(db, claim.record_id)
    if scope is None:
        raise RevisionCleanupError(
            "cleanup_claim_stale", "cleanup scope is unavailable"
        )
    require_live_cleanup_claim(scope, claim)
    scope.record.status = "failed"
    scope.record.block_code = None
    scope.record.available_at = current_time + timedelta(
        seconds=min(3_600, 2 ** min(scope.record.attempt_count, 10))
    )
    scope.record.finished_at = None
    scope.record.last_error_code = code
    clear_cleanup_claim(scope.record)
    await audit_log.record(
        db,
        None,
        "revision_cleanup.failed",
        {
            "record_id": str(scope.record.id),
            "library_id": str(scope.record.library_id),
            "revision_file_id": str(scope.revision_file.id),
            "attempt_count": scope.record.attempt_count,
            "error_code": code,
        },
    )
    return RevisionCleanupResult(scope.record.id, "failed", code)


async def cleanup_candidate_record_ids(
    db,
    *,
    at: datetime,
    limit: int,
    config: Settings,
) -> tuple[uuid.UUID, ...]:
    due = and_(
        RevisionRetentionRecord.status.in_(("queued", "failed")),
        RevisionRetentionRecord.available_at <= at,
    )
    expired = and_(
        RevisionRetentionRecord.status == "processing",
        RevisionRetentionRecord.lease_expires_at <= at,
    )
    return tuple(
        (
            await db.execute(
                select(RevisionRetentionRecord.id)
                .where(
                    or_(due, expired),
                    RevisionRetentionRecord.attempt_count
                    < config.revision_cleanup_max_attempts,
                )
                .order_by(
                    RevisionRetentionRecord.library_id,
                    RevisionRetentionRecord.available_at,
                    RevisionRetentionRecord.id,
                )
                .limit(limit)
            )
        ).scalars().all()
    )
