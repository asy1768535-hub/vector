from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.knowledge_artifact import KnowledgeArtifact
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.library import Library
from app.services.knowledge_artifact_jobs import (
    clear_job_claim,
    lock_artifact_job,
    require_live_claim,
    utcnow,
)
from app.services.knowledge_artifact_policy import (
    fail_artifact_runtime,
    require_current_job_identity,
    select_generation_spec,
    validate_enqueue_scope,
)
from app.services.knowledge_artifacts import build_artifact


@dataclass(frozen=True, slots=True)
class PreparedArtifactJob:
    job_id: uuid.UUID
    claim_token: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    artifact_type: str
    contract_version: str
    extractor_version: str
    generation_mode: str
    input_fingerprint: str
    revision_content_hash: str
    source_text: str = field(repr=False)
    source_character_count: int
    source_truncated: bool
    title_paths: tuple[list[Any], ...] = field(repr=False)
    document_title: str | None = field(repr=False)
    model_provider: str | None
    model_name: str | None
    model_config_hash: str | None


async def publish_knowledge_artifact(
    db,
    *,
    prepared: PreparedArtifactJob,
    payload,
    now: datetime | None = None,
    config: Settings = settings,
) -> KnowledgeArtifact:
    now = now or utcnow()
    document_result = await db.execute(
        select(Document).where(Document.id == prepared.document_id).with_for_update()
    )
    document = document_result.scalars().first()
    library_result = await db.execute(
        select(Library).where(Library.id == prepared.library_id).with_for_update()
    )
    library = library_result.scalars().first()
    job = await lock_artifact_job(db, prepared.job_id)
    if job is None:
        fail_artifact_runtime(
            "job_not_found", "knowledge artifact Job was not found"
        )
    require_live_claim(job, claim_token=prepared.claim_token, now=now)
    revision_result = await db.execute(
        select(DocumentRevision)
        .where(DocumentRevision.id == job.document_revision_id)
        .with_for_update()
    )
    revision = revision_result.scalars().first()
    if library is None or document is None or revision is None:
        fail_artifact_runtime(
            "runtime_scope_missing", "knowledge artifact Job scope is missing"
        )
    validate_enqueue_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=job.trigger_type == "revision_ready",
        config=config,
    )
    if revision.content_hash != prepared.revision_content_hash:
        fail_artifact_runtime(
            "revision_content_changed", "Revision content identity changed"
        )
    spec = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type=job.artifact_type,
        source_character_count=prepared.source_character_count,
        config=config,
    )
    require_current_job_identity(job, revision=revision, spec=spec)
    current_result = await db.execute(
        select(KnowledgeArtifact)
        .where(
            KnowledgeArtifact.document_revision_id == job.document_revision_id,
            KnowledgeArtifact.artifact_type == job.artifact_type,
            KnowledgeArtifact.lifecycle_state == "current",
        )
        .with_for_update()
    )
    current = current_result.scalars().all()
    artifact = build_artifact(
        job=job,
        library_id=prepared.library_id,
        document_id=prepared.document_id,
        document_revision_id=prepared.document_revision_id,
        artifact_type=prepared.artifact_type,
        contract_version=prepared.contract_version,
        extractor_version=prepared.extractor_version,
        input_fingerprint=prepared.input_fingerprint,
        payload=payload,
    )
    for previous in current:
        previous.lifecycle_state = "stale"
        previous.stale_at = now
    db.add(artifact)
    job.status = "succeeded"
    job.error_code = None
    job.error_message = None
    job.finished_at = now
    clear_job_claim(job)
    await db.flush()
    return artifact


async def supersede_revision_artifacts(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[int, int]:
    now = now or utcnow()
    artifact_result = await db.execute(
        update(KnowledgeArtifact)
        .where(
            KnowledgeArtifact.library_id == library_id,
            KnowledgeArtifact.document_id == document_id,
            KnowledgeArtifact.document_revision_id == document_revision_id,
            KnowledgeArtifact.lifecycle_state == "current",
        )
        .values(lifecycle_state="stale", stale_at=now)
    )
    job_result = await db.execute(
        update(KnowledgeArtifactJob)
        .where(
            KnowledgeArtifactJob.library_id == library_id,
            KnowledgeArtifactJob.document_id == document_id,
            KnowledgeArtifactJob.document_revision_id == document_revision_id,
            KnowledgeArtifactJob.status.in_(("queued", "processing")),
        )
        .values(
            status="superseded",
            error_code="revision_superseded",
            error_message="knowledge artifact Job Revision was superseded",
            finished_at=now,
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
            updated_at=now,
        )
    )
    return artifact_result.rowcount or 0, job_result.rowcount or 0


async def list_current_document_artifacts(
    db,
    *,
    library_id: uuid.UUID,
    document: Document,
) -> tuple[KnowledgeArtifact, ...]:
    if document.library_id != library_id or document.deleted_at is not None:
        fail_artifact_runtime(
            "document_not_found", "document was not found in this Library"
        )
    if document.current_revision_id is None:
        return ()
    result = await db.execute(
        select(KnowledgeArtifact)
        .where(
            KnowledgeArtifact.library_id == library_id,
            KnowledgeArtifact.document_id == document.id,
            KnowledgeArtifact.document_revision_id == document.current_revision_id,
            KnowledgeArtifact.lifecycle_state == "current",
        )
        .order_by(KnowledgeArtifact.artifact_type.asc(), KnowledgeArtifact.id.asc())
    )
    return tuple(result.scalars().all())
