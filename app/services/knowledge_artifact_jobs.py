from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings, settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.library import Library
from app.services.knowledge_artifact_policy import (
    KnowledgeArtifactRuntimeError,
    fail_artifact_runtime,
    require_current_job_identity,
    select_generation_spec,
    validate_enqueue_scope,
)
from app.services.knowledge_artifact_source import load_artifact_source_text
from app.services.knowledge_artifacts import (
    ArtifactContractError,
    build_artifact_job,
    job_idempotency_key_v1,
)


log = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = {
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
}


@dataclass(frozen=True, slots=True)
class ClaimedArtifactJob:
    job_id: uuid.UUID
    claim_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class StaleJobRecoveryResult:
    requeued: int
    failed: int


@dataclass(frozen=True, slots=True)
class ArtifactJobEnqueueResult:
    job: KnowledgeArtifactJob
    created: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clear_job_claim(job: KnowledgeArtifactJob) -> None:
    job.claim_token = None
    job.claimed_by = None
    job.lease_expires_at = None
    job.last_heartbeat_at = None


async def job_by_idempotency_key(
    db, idempotency_key: str
) -> KnowledgeArtifactJob | None:
    result = await db.execute(
        select(KnowledgeArtifactJob)
        .where(KnowledgeArtifactJob.idempotency_key == idempotency_key)
        .limit(1)
    )
    return result.scalars().first()


def _require_same_job_identity(
    existing: KnowledgeArtifactJob, candidate: KnowledgeArtifactJob
) -> None:
    fields = (
        "library_id",
        "document_id",
        "document_revision_id",
        "artifact_type",
        "contract_version",
        "extractor_version",
        "generation_mode",
        "model_provider",
        "model_name",
        "model_config_hash",
        "input_fingerprint",
        "retry_generation",
    )
    if any(getattr(existing, name) != getattr(candidate, name) for name in fields):
        fail_artifact_runtime(
            "idempotency_key_conflict",
            "artifact Job idempotency key belongs to a different identity",
        )


async def _enqueue_knowledge_artifact_job_result(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    artifact_type: str,
    trigger_type: str,
    requested_by_user_id: uuid.UUID | None = None,
    auto_trigger: bool = False,
    config: Settings = settings,
) -> ArtifactJobEnqueueResult:
    validate_enqueue_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=auto_trigger,
        config=config,
    )
    source = (
        await load_artifact_source_text(db, revision=revision)
        if artifact_type == "summary"
        else ""
    )
    spec = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type=artifact_type,
        source_character_count=len(source),
        config=config,
    )
    try:
        candidate = build_artifact_job(
            library_id=library.id,
            document_id=document.id,
            document_revision_id=revision.id,
            revision_content_hash=revision.content_hash,
            artifact_type=spec.artifact_type,
            contract_version=spec.contract_version,
            extractor_version=spec.extractor_version,
            generation_mode=spec.generation_mode,
            trigger_type=trigger_type,
            model_provider=spec.model_provider,
            model_name=spec.model_name,
            model_config_hash=spec.model_config_hash,
            requested_by_user_id=requested_by_user_id,
        )
    except ArtifactContractError as exc:
        raise KnowledgeArtifactRuntimeError(
            "invalid_job_identity", "artifact Job identity is invalid"
        ) from exc
    existing = await job_by_idempotency_key(db, candidate.idempotency_key)
    if existing is not None:
        _require_same_job_identity(existing, candidate)
        return ArtifactJobEnqueueResult(job=existing, created=False)
    try:
        async with db.begin_nested():
            db.add(candidate)
            await db.flush()
    except IntegrityError:
        existing = await job_by_idempotency_key(db, candidate.idempotency_key)
        if existing is None:
            raise
        _require_same_job_identity(existing, candidate)
        return ArtifactJobEnqueueResult(job=existing, created=False)
    return ArtifactJobEnqueueResult(job=candidate, created=True)


async def enqueue_knowledge_artifact_job(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    artifact_type: str,
    trigger_type: str,
    requested_by_user_id: uuid.UUID | None = None,
    auto_trigger: bool = False,
    config: Settings = settings,
) -> KnowledgeArtifactJob:
    result = await _enqueue_knowledge_artifact_job_result(
        db,
        library=library,
        document=document,
        revision=revision,
        artifact_type=artifact_type,
        trigger_type=trigger_type,
        requested_by_user_id=requested_by_user_id,
        auto_trigger=auto_trigger,
        config=config,
    )
    return result.job


async def retry_knowledge_artifact_job(
    db,
    *,
    source_job: KnowledgeArtifactJob,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    requested_by_user_id: uuid.UUID | None,
    config: Settings = settings,
) -> KnowledgeArtifactJob:
    if source_job.status not in {"failed", "cancelled"}:
        fail_artifact_runtime(
            "job_not_retryable", "only a terminal unsuccessful Job may be retried"
        )
    validate_enqueue_scope(
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
    ):
        fail_artifact_runtime(
            "scope_mismatch", "retry source Job is outside the requested scope"
        )
    source = (
        await load_artifact_source_text(db, revision=revision)
        if source_job.artifact_type == "summary"
        else ""
    )
    spec = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type=source_job.artifact_type,
        source_character_count=len(source),
        config=config,
    )
    require_current_job_identity(source_job, revision=revision, spec=spec)
    retry_generation = source_job.retry_generation + 1
    idempotency_key = job_idempotency_key_v1(
        source_job.input_fingerprint, retry_generation
    )
    existing = await job_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return existing
    job = KnowledgeArtifactJob(
        id=uuid.uuid4(),
        library_id=source_job.library_id,
        document_id=source_job.document_id,
        document_revision_id=source_job.document_revision_id,
        artifact_type=source_job.artifact_type,
        contract_version=source_job.contract_version,
        extractor_version=source_job.extractor_version,
        generation_mode=source_job.generation_mode,
        model_provider=source_job.model_provider,
        model_name=source_job.model_name,
        model_config_hash=source_job.model_config_hash,
        input_fingerprint=source_job.input_fingerprint,
        idempotency_key=idempotency_key,
        retry_generation=retry_generation,
        attempt_count=0,
        trigger_type="retry",
        status="queued",
        rerun_of_job_id=source_job.id,
        requested_by_user_id=requested_by_user_id,
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        existing = await job_by_idempotency_key(db, idempotency_key)
        if existing is None:
            raise
        return existing
    return job


async def cancel_knowledge_artifact_job(
    db,
    *,
    job: KnowledgeArtifactJob,
    now: datetime | None = None,
) -> bool:
    if job.status in TERMINAL_JOB_STATUSES:
        return False
    job.status = "cancelled"
    job.error_code = "cancelled_by_user"
    job.error_message = "knowledge artifact generation was cancelled"
    job.finished_at = now or utcnow()
    clear_job_claim(job)
    await db.flush()
    return True


async def claim_knowledge_artifact_job(
    db,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> ClaimedArtifactJob | None:
    if not worker_id.strip() or len(worker_id) > 160:
        fail_artifact_runtime(
            "invalid_worker_id", "worker ID must be nonblank and bounded"
        )
    if lease_seconds <= 0 or max_attempts <= 0:
        fail_artifact_runtime(
            "invalid_worker_limits", "worker limits must be positive"
        )
    now = now or utcnow()
    await db.execute(
        update(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.status == "queued",
            KnowledgeArtifactJob.attempt_count >= max_attempts,
        )
        .values(
            status="failed",
            error_code="attempt_limit_exceeded",
            error_message="knowledge artifact Job exhausted its attempt limit",
            finished_at=now,
            updated_at=now,
        )
    )
    result = await db.execute(
        select(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.status == "queued",
            KnowledgeArtifactJob.attempt_count < max_attempts,
        )
        .order_by(KnowledgeArtifactJob.created_at.asc(), KnowledgeArtifactJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = result.scalars().first()
    if job is None:
        return None
    token = uuid.uuid4()
    job.status = "processing"
    job.attempt_count += 1
    job.claim_token = token
    job.claimed_by = worker_id
    job.started_at = job.started_at or now
    job.finished_at = None
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.last_heartbeat_at = now
    job.error_code = None
    job.error_message = None
    await db.flush()
    return ClaimedArtifactJob(job_id=job.id, claim_token=token)


async def renew_knowledge_artifact_lease(
    db,
    *,
    job_id: uuid.UUID,
    claim_token: uuid.UUID,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    if lease_seconds <= 0:
        fail_artifact_runtime(
            "invalid_worker_limits", "worker lease must be positive"
        )
    now = now or utcnow()
    result = await db.execute(
        update(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.id == job_id,
            KnowledgeArtifactJob.status == "processing",
            KnowledgeArtifactJob.claim_token == claim_token,
            KnowledgeArtifactJob.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
            updated_at=now,
        )
    )
    return bool(result.rowcount == 1)


async def recover_stale_knowledge_artifact_jobs(
    db,
    *,
    max_attempts: int,
    limit: int = 100,
    now: datetime | None = None,
) -> StaleJobRecoveryResult:
    if max_attempts <= 0 or limit <= 0 or limit > 1_000:
        fail_artifact_runtime(
            "invalid_worker_limits", "recovery limits are invalid"
        )
    now = now or utcnow()
    result = await db.execute(
        select(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.status == "processing",
            KnowledgeArtifactJob.lease_expires_at <= now,
        )
        .order_by(KnowledgeArtifactJob.lease_expires_at.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    requeued = failed = 0
    for job in result.scalars().all():
        clear_job_claim(job)
        if job.attempt_count >= max_attempts:
            job.status = "failed"
            job.error_code = "lease_expired_attempt_limit"
            job.error_message = (
                "knowledge artifact Job lease expired at its attempt limit"
            )
            job.finished_at = now
            failed += 1
        else:
            job.status = "queued"
            job.error_code = "lease_expired"
            job.error_message = "knowledge artifact Job lease expired and was requeued"
            requeued += 1
    await db.flush()
    return StaleJobRecoveryResult(requeued=requeued, failed=failed)


def require_live_claim(
    job: KnowledgeArtifactJob,
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
        fail_artifact_runtime(
            "claim_lost", "knowledge artifact Job claim is no longer live"
        )


async def lock_artifact_job(db, job_id: uuid.UUID) -> KnowledgeArtifactJob | None:
    result = await db.execute(
        select(KnowledgeArtifactJob)
        .where(KnowledgeArtifactJob.id == job_id)
        .with_for_update()
    )
    return result.scalars().first()


async def cancel_library_artifact_jobs_for_policy_change(
    db,
    *,
    library_id: uuid.UUID,
    error_code: str,
    artifact_types: tuple[str, ...] = (),
    include_model_jobs: bool = False,
    now: datetime | None = None,
) -> int:
    if not artifact_types and not include_model_jobs:
        return 0
    now = now or utcnow()
    type_filter = KnowledgeArtifactJob.artifact_type.in_(artifact_types)
    model_filter = KnowledgeArtifactJob.generation_mode == "model"
    target_filter = (
        type_filter | model_filter
        if artifact_types and include_model_jobs
        else type_filter
        if artifact_types
        else model_filter
    )
    result = await db.execute(
        update(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.library_id == library_id,
            KnowledgeArtifactJob.status.in_(("queued", "processing")),
            target_filter,
        )
        .values(
            status="cancelled",
            error_code=error_code[:64],
            error_message="knowledge artifact Job was cancelled by a Library policy change",
            finished_at=now,
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def enqueue_ready_revision_artifacts(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> tuple[uuid.UUID, ...]:
    if not (
        config.knowledge_artifact_runtime_enabled
        and config.knowledge_artifact_auto_trigger_enabled
    ):
        return ()
    created: list[uuid.UUID] = []
    async with session_factory() as db:
        async with db.begin():
            library = await db.get(Library, library_id)
            document = await db.get(Document, document_id)
            revision = await db.get(DocumentRevision, revision_id)
            if library is None or document is None or revision is None:
                return ()
            validate_enqueue_scope(
                library=library,
                document=document,
                revision=revision,
                auto_trigger=True,
                config=config,
            )
            for artifact_type, enabled in (
                ("summary", library.summary_artifact_enabled),
                ("outline", library.outline_artifact_enabled),
            ):
                if not enabled:
                    continue
                try:
                    result = await _enqueue_knowledge_artifact_job_result(
                        db,
                        library=library,
                        document=document,
                        revision=revision,
                        artifact_type=artifact_type,
                        trigger_type="revision_ready",
                        requested_by_user_id=revision.created_by,
                        auto_trigger=True,
                        config=config,
                    )
                except KnowledgeArtifactRuntimeError as exc:
                    log.warning(
                        "knowledge artifact auto enqueue skipped: library=%s document=%s "
                        "revision=%s type=%s code=%s",
                        library_id,
                        document_id,
                        revision_id,
                        artifact_type,
                        exc.code,
                    )
                    continue
                if result.created:
                    created.append(result.job.id)
    return tuple(created)


async def compensate_ready_revision_artifacts(
    *,
    limit: int = 100,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> int:
    if not (
        config.knowledge_artifact_runtime_enabled
        and config.knowledge_artifact_auto_trigger_enabled
    ):
        return 0
    if limit <= 0 or limit > 1_000:
        fail_artifact_runtime(
            "invalid_compensation_limit", "compensation limit must be within 1..1000"
        )
    summary_job_exists = (
        select(KnowledgeArtifactJob.id)
        .where(
            KnowledgeArtifactJob.library_id == Library.id,
            KnowledgeArtifactJob.document_id == Document.id,
            KnowledgeArtifactJob.document_revision_id == DocumentRevision.id,
            KnowledgeArtifactJob.artifact_type == "summary",
        )
        .exists()
    )
    outline_job_exists = (
        select(KnowledgeArtifactJob.id)
        .where(
            KnowledgeArtifactJob.library_id == Library.id,
            KnowledgeArtifactJob.document_id == Document.id,
            KnowledgeArtifactJob.document_revision_id == DocumentRevision.id,
            KnowledgeArtifactJob.artifact_type == "outline",
        )
        .exists()
    )
    async with session_factory() as db:
        result = await db.execute(
            select(Library.id, Document.id, DocumentRevision.id)
            .join(Document, Document.library_id == Library.id)
            .join(
                DocumentRevision,
                DocumentRevision.id == Document.current_revision_id,
            )
            .where(
                Library.deleted_at.is_(None),
                Library.knowledge_artifact_auto_enabled.is_(True),
                Document.deleted_at.is_(None),
                Document.status == "ready",
                DocumentRevision.status == "ready",
                or_(
                    and_(
                        Library.summary_artifact_enabled.is_(True),
                        ~summary_job_exists,
                    ),
                    and_(
                        Library.outline_artifact_enabled.is_(True),
                        ~outline_job_exists,
                    ),
                ),
            )
            .order_by(Document.updated_at.asc(), Document.id.asc())
            .limit(limit)
        )
        scopes = result.all()
    ensured = 0
    for library_id, document_id, revision_id in scopes:
        ensured += len(
            await enqueue_ready_revision_artifacts(
                library_id=library_id,
                document_id=document_id,
                revision_id=revision_id,
                session_factory=session_factory,
                config=config,
            )
        )
    return ensured
