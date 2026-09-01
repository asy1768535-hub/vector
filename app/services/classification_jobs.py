from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings, settings
from app.db import async_session_factory
from app.models.classification_job import DocumentClassificationJob
from app.models.classification_taxonomy import ClassificationTaxonomy
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services.classification_decisions import lock_classification_scope
from app.services.classification_decision_contracts import ClassificationDecisionError
from app.services.classification_runtime_contracts import (
    ClassificationRuntimeError,
    build_classification_job,
    classification_job_identity,
    fail_classification_runtime,
)
from app.services.classification_runtime_policy import validate_classification_scope
from app.services.knowledge_artifact_source import load_artifact_source_text


log = logging.getLogger(__name__)
TERMINAL_CLASSIFICATION_JOB_STATUSES = {
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
}


@dataclass(frozen=True, slots=True)
class ClaimedClassificationJob:
    job_id: uuid.UUID
    claim_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class ClassificationJobEnqueueResult:
    job: DocumentClassificationJob
    created: bool


@dataclass(frozen=True, slots=True)
class ClassificationJobRecoveryResult:
    requeued: int
    failed: int
    failed_job_ids: tuple[uuid.UUID, ...] = ()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clear_classification_claim(job: DocumentClassificationJob) -> None:
    job.claim_token = None
    job.claimed_by = None
    job.lease_expires_at = None
    job.last_heartbeat_at = None


async def classification_job_by_idempotency_key(
    db,
    idempotency_key: str,
) -> DocumentClassificationJob | None:
    return (
        await db.execute(
            select(DocumentClassificationJob)
            .where(DocumentClassificationJob.idempotency_key == idempotency_key)
            .limit(1)
        )
    ).scalars().first()


def _require_same_job_identity(
    existing: DocumentClassificationJob,
    candidate: DocumentClassificationJob,
) -> None:
    fields = (
        "library_id",
        "document_id",
        "document_revision_id",
        "revision_content_hash",
        "taxonomy_version_id",
        "enabled_label_set_hash",
        "classifier_version",
        "model_provider",
        "model_name",
        "model_config_hash",
        "prompt_version",
        "input_fingerprint",
        "retry_generation",
    )
    if any(getattr(existing, name) != getattr(candidate, name) for name in fields):
        fail_classification_runtime(
            "classification_idempotency_conflict",
            "classification Job idempotency key has another identity",
        )


async def _classification_scope(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    auto_trigger: bool,
    config: Settings,
):
    validate_classification_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=auto_trigger,
        config=config,
    )
    source = await load_artifact_source_text(db, revision=revision)
    if not isinstance(source, str) or not source.strip():
        fail_classification_runtime(
            "classification_source_empty",
            "classification source is empty",
        )
    try:
        taxonomy, enabled, all_labels = await lock_classification_scope(
            db,
            library=library,
        )
    except ClassificationDecisionError as exc:
        raise ClassificationRuntimeError(
            exc.code,
            "classification taxonomy scope is unavailable",
        ) from exc
    return source, taxonomy, enabled, all_labels


async def _enqueue_classification_job_result(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    trigger_type: str,
    requested_by_user_id: uuid.UUID | None = None,
    auto_trigger: bool = False,
    retry_generation: int = 0,
    rerun_of_job_id: uuid.UUID | None = None,
    config: Settings = settings,
) -> ClassificationJobEnqueueResult:
    _, taxonomy, enabled, _ = await _classification_scope(
        db,
        library=library,
        document=document,
        revision=revision,
        auto_trigger=auto_trigger,
        config=config,
    )
    candidate = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=tuple(label.id for label in enabled),
        trigger_type=trigger_type,
        retry_generation=retry_generation,
        rerun_of_job_id=rerun_of_job_id,
        requested_by_user_id=requested_by_user_id,
        config=config,
    )
    existing = await classification_job_by_idempotency_key(
        db,
        candidate.idempotency_key,
    )
    if existing is not None:
        _require_same_job_identity(existing, candidate)
        return ClassificationJobEnqueueResult(existing, False)
    try:
        async with db.begin_nested():
            db.add(candidate)
            await db.flush()
    except IntegrityError:
        existing = await classification_job_by_idempotency_key(
            db,
            candidate.idempotency_key,
        )
        if existing is None:
            raise
        _require_same_job_identity(existing, candidate)
        return ClassificationJobEnqueueResult(existing, False)
    return ClassificationJobEnqueueResult(candidate, True)


async def enqueue_classification_job(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    trigger_type: str,
    requested_by_user_id: uuid.UUID | None = None,
    auto_trigger: bool = False,
    config: Settings = settings,
) -> DocumentClassificationJob:
    return (
        await _enqueue_classification_job_result(
            db,
            library=library,
            document=document,
            revision=revision,
            trigger_type=trigger_type,
            requested_by_user_id=requested_by_user_id,
            auto_trigger=auto_trigger,
            config=config,
        )
    ).job


async def retry_classification_job(
    db,
    *,
    source_job: DocumentClassificationJob,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    requested_by_user_id: uuid.UUID | None,
    config: Settings = settings,
) -> DocumentClassificationJob:
    if source_job.status not in {"failed", "cancelled"}:
        fail_classification_runtime(
            "classification_job_not_retryable",
            "only an unsuccessful terminal classification Job may be retried",
        )
    _, taxonomy, enabled, _ = await _classification_scope(
        db,
        library=library,
        document=document,
        revision=revision,
        auto_trigger=False,
        config=config,
    )
    if (
        source_job.library_id != library.id
        or source_job.document_id != document.id
        or source_job.document_revision_id != revision.id
        or source_job.taxonomy_version_id != taxonomy.id
    ):
        fail_classification_runtime(
            "classification_scope_mismatch",
            "classification retry source is outside the current scope",
        )
    retry_generation = source_job.retry_generation + 1
    identity = classification_job_identity(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=tuple(label.id for label in enabled),
        retry_generation=retry_generation,
        config=config,
    )
    if identity.input_fingerprint != source_job.input_fingerprint:
        fail_classification_runtime(
            "classification_job_identity_stale",
            "classification retry identity is stale",
        )
    job = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=tuple(label.id for label in enabled),
        trigger_type="retry",
        retry_generation=retry_generation,
        rerun_of_job_id=source_job.id,
        requested_by_user_id=requested_by_user_id,
        config=config,
    )
    existing = await classification_job_by_idempotency_key(db, identity.idempotency_key)
    if existing is not None:
        _require_same_job_identity(existing, job)
        return existing
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        existing = await classification_job_by_idempotency_key(
            db,
            identity.idempotency_key,
        )
        if existing is None:
            raise
        _require_same_job_identity(existing, job)
        return existing
    return job


async def cancel_classification_job(
    db,
    *,
    job: DocumentClassificationJob,
    now: datetime | None = None,
) -> bool:
    if job.status in TERMINAL_CLASSIFICATION_JOB_STATUSES:
        return False
    job.status = "cancelled"
    job.error_code = "cancelled_by_user"
    job.error_message = "classification Job was cancelled"
    job.finished_at = now or utcnow()
    clear_classification_claim(job)
    await db.flush()
    return True


async def claim_classification_job(
    db,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> ClaimedClassificationJob | None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 160:
        fail_classification_runtime(
            "classification_worker_id_invalid",
            "classification Worker ID is invalid",
        )
    if lease_seconds <= 0 or max_attempts <= 0:
        fail_classification_runtime(
            "classification_worker_limits_invalid",
            "classification Worker limits must be positive",
        )
    now = now or utcnow()
    await db.execute(
        update(DocumentClassificationJob)
        .where(
            DocumentClassificationJob.status == "queued",
            DocumentClassificationJob.attempt_count >= max_attempts,
        )
        .values(
            status="failed",
            error_code="attempt_limit_exceeded",
            error_message="classification Job exhausted its attempt limit",
            finished_at=now,
            updated_at=now,
        )
    )
    job = (
        await db.execute(
            select(DocumentClassificationJob)
            .where(
                DocumentClassificationJob.status == "queued",
                DocumentClassificationJob.attempt_count < max_attempts,
            )
            .order_by(
                DocumentClassificationJob.created_at.asc(),
                DocumentClassificationJob.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalars().first()
    if job is None:
        return None
    token = uuid.uuid4()
    job.status = "processing"
    job.attempt_count += 1
    job.claim_token = token
    job.claimed_by = worker_id.strip()
    job.started_at = job.started_at or now
    job.finished_at = None
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.last_heartbeat_at = now
    job.error_code = None
    job.error_message = None
    await db.flush()
    return ClaimedClassificationJob(job.id, token)


async def renew_classification_lease(
    db,
    *,
    job_id: uuid.UUID,
    claim_token: uuid.UUID,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    if lease_seconds <= 0:
        fail_classification_runtime(
            "classification_worker_limits_invalid",
            "classification lease must be positive",
        )
    now = now or utcnow()
    result = await db.execute(
        update(DocumentClassificationJob)
        .where(
            DocumentClassificationJob.id == job_id,
            DocumentClassificationJob.status == "processing",
            DocumentClassificationJob.claim_token == claim_token,
            DocumentClassificationJob.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
            updated_at=now,
        )
    )
    return bool(result.rowcount == 1)


def require_live_classification_claim(
    job: DocumentClassificationJob,
    *,
    claim_token: uuid.UUID,
    now: datetime,
) -> None:
    if (
        job.status != "processing"
        or job.claim_token != claim_token
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        fail_classification_runtime(
            "classification_claim_lost",
            "classification Job claim is no longer live",
        )


async def lock_classification_job(
    db,
    job_id: uuid.UUID,
) -> DocumentClassificationJob | None:
    return (
        await db.execute(
            select(DocumentClassificationJob)
            .where(DocumentClassificationJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


async def recover_stale_classification_jobs(
    db,
    *,
    max_attempts: int,
    limit: int = 100,
    now: datetime | None = None,
) -> ClassificationJobRecoveryResult:
    if max_attempts <= 0 or limit <= 0 or limit > 1_000:
        fail_classification_runtime(
            "classification_worker_limits_invalid",
            "classification recovery limits are invalid",
        )
    now = now or utcnow()
    jobs = (
        await db.execute(
            select(DocumentClassificationJob)
            .where(
                DocumentClassificationJob.status == "processing",
                DocumentClassificationJob.lease_expires_at <= now,
            )
            .order_by(DocumentClassificationJob.lease_expires_at.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()
    requeued = failed = 0
    failed_job_ids: list[uuid.UUID] = []
    for job in jobs:
        clear_classification_claim(job)
        if job.attempt_count >= max_attempts:
            job.status = "failed"
            job.error_code = "lease_expired_attempt_limit"
            job.error_message = "classification Job lease expired at its attempt limit"
            job.finished_at = now
            failed += 1
            failed_job_ids.append(job.id)
        else:
            job.status = "queued"
            job.error_code = "lease_expired"
            job.error_message = "classification Job lease expired and was requeued"
            requeued += 1
    await db.flush()
    return ClassificationJobRecoveryResult(requeued, failed, tuple(failed_job_ids))


async def list_unrecorded_attempt_failure_job_ids(
    db,
    *,
    limit: int = 100,
) -> tuple[uuid.UUID, ...]:
    if limit <= 0 or limit > 1_000:
        fail_classification_runtime(
            "classification_worker_limits_invalid",
            "classification failure recovery limit is invalid",
        )
    rows = (
        await db.execute(
            select(DocumentClassificationJob.id)
            .where(
                DocumentClassificationJob.status == "failed",
                DocumentClassificationJob.result_run_id.is_(None),
                DocumentClassificationJob.error_code.in_(
                    ("attempt_limit_exceeded", "lease_expired_attempt_limit")
                ),
            )
            .order_by(
                DocumentClassificationJob.finished_at.asc(),
                DocumentClassificationJob.id.asc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    return tuple(rows)


async def cancel_library_classification_jobs_for_policy_change(
    db,
    *,
    library_id: uuid.UUID,
    error_code: str,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    result = await db.execute(
        update(DocumentClassificationJob)
        .where(
            DocumentClassificationJob.library_id == library_id,
            DocumentClassificationJob.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            error_code=error_code[:64],
            error_message="classification Job was cancelled by a Library policy change",
            finished_at=now,
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def cancel_organization_classification_jobs_for_taxonomy_change(
    db,
    *,
    organization_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    library_ids = select(Library.id).where(
        Library.organization_id == organization_id
    )
    result = await db.execute(
        update(DocumentClassificationJob)
        .where(
            DocumentClassificationJob.library_id.in_(library_ids),
            DocumentClassificationJob.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            error_code="organization_taxonomy_changed",
            error_message="classification Job was cancelled by taxonomy activation",
            finished_at=now,
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def supersede_revision_classification_jobs(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    result = await db.execute(
        update(DocumentClassificationJob)
        .where(
            DocumentClassificationJob.library_id == library_id,
            DocumentClassificationJob.document_id == document_id,
            DocumentClassificationJob.document_revision_id == document_revision_id,
            DocumentClassificationJob.status.in_(("queued", "processing")),
        )
        .values(
            status="superseded",
            error_code="historical_revision",
            error_message="classification Job Revision was superseded",
            finished_at=now,
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def enqueue_ready_revision_classification(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> tuple[uuid.UUID, ...]:
    if not (
        config.classification_runtime_enabled
        and config.classification_auto_trigger_enabled
    ):
        return ()
    async with session_factory() as db:
        async with db.begin():
            library = await db.get(Library, library_id)
            document = await db.get(Document, document_id)
            revision = await db.get(DocumentRevision, revision_id)
            if library is None or document is None or revision is None:
                return ()
            try:
                result = await _enqueue_classification_job_result(
                    db,
                    library=library,
                    document=document,
                    revision=revision,
                    trigger_type="revision_ready",
                    requested_by_user_id=revision.created_by,
                    auto_trigger=True,
                    config=config,
                )
            except ClassificationRuntimeError as exc:
                log.warning(
                    "classification auto enqueue skipped: library=%s document=%s "
                    "revision=%s code=%s",
                    library_id,
                    document_id,
                    revision_id,
                    exc.code,
                )
                return ()
            return (result.job.id,) if result.created else ()


async def compensate_ready_revision_classifications(
    *,
    limit: int = 100,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> int:
    if not (
        config.classification_runtime_enabled
        and config.classification_auto_trigger_enabled
    ):
        return 0
    if limit <= 0 or limit > 1_000:
        fail_classification_runtime(
            "classification_compensation_limit_invalid",
            "classification compensation limit must be within 1..1000",
        )
    scan_limit = min(1_000, max(100, limit * 10))
    created = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while created < limit:
        statement = (
            select(
                Library.id,
                Document.id,
                DocumentRevision.id,
                Document.updated_at,
            )
            .join(Document, Document.library_id == Library.id)
            .join(
                DocumentRevision,
                DocumentRevision.id == Document.current_revision_id,
            )
            .join(
                ClassificationTaxonomy,
                ClassificationTaxonomy.organization_id == Library.organization_id,
            )
            .where(
                Library.deleted_at.is_(None),
                Library.classification_auto_enabled.is_(True),
                Library.classification_external_model_enabled.is_(True),
                Document.deleted_at.is_(None),
                Document.status == "ready",
                DocumentRevision.status == "ready",
                ClassificationTaxonomy.status == "active",
            )
            .order_by(Document.updated_at.asc(), Document.id.asc())
            .limit(scan_limit)
        )
        if cursor is not None:
            updated_at, document_id = cursor
            statement = statement.where(
                or_(
                    Document.updated_at > updated_at,
                    and_(
                        Document.updated_at == updated_at,
                        Document.id > document_id,
                    ),
                )
            )
        async with session_factory() as db:
            scopes = (await db.execute(statement)).all()
        if not scopes:
            break
        for library_id, document_id, revision_id, _ in scopes:
            created += len(
                await enqueue_ready_revision_classification(
                    library_id=library_id,
                    document_id=document_id,
                    revision_id=revision_id,
                    session_factory=session_factory,
                    config=config,
                )
            )
            if created >= limit:
                return created
        _, last_document_id, _, last_updated_at = scopes[-1]
        cursor = (last_updated_at, last_document_id)
        if len(scopes) < scan_limit:
            break
    return created
