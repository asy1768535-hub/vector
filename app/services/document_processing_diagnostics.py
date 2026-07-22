from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, select

from app.config import Settings, settings
from app.models.classification_job import DocumentClassificationJob
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.library import Library
from app.schemas.document_processing import (
    DocumentProcessingRead,
    DocumentProcessingStageRead,
    GraphProcessingCountsRead,
)
from app.services import audit_log
from app.services.classification_jobs import retry_classification_job
from app.services.classification_runtime_contracts import ClassificationRuntimeError
from app.services.document_processing_contracts import (
    PROCESSING_STAGES,
    DocumentProcessingError,
    ProcessingStage,
    retry_rejection_code,
    safe_stage_error_code,
)
from app.services.graph_extraction_jobs import (
    GraphExtractionJobError,
    retry_graph_extraction_job,
)
from app.services.knowledge_artifact_jobs import retry_knowledge_artifact_job
from app.services.knowledge_artifact_policy import KnowledgeArtifactRuntimeError


_JOB_STATUSES = {
    "queued",
    "processing",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
    "partially_succeeded",
}


@dataclass(frozen=True, slots=True)
class _CurrentScope:
    document: Document
    revision: DocumentRevision


def _fail(code: str, message: str = "Document processing request failed") -> None:
    raise DocumentProcessingError(code, message)


async def _load_current_scope(
    db,
    *,
    library: Library,
    document_id: uuid.UUID,
    for_update: bool = False,
) -> _CurrentScope:
    document_statement = select(Document).where(
        Document.id == document_id,
        Document.library_id == library.id,
        Document.deleted_at.is_(None),
    )
    if for_update:
        document_statement = document_statement.with_for_update()
    document = (await db.execute(document_statement)).scalar_one_or_none()
    if document is None:
        _fail("processing_document_not_found")
    if document.current_revision_id is None:
        _fail("processing_revision_unavailable")

    revision_statement = select(DocumentRevision).where(
        DocumentRevision.id == document.current_revision_id,
        DocumentRevision.document_id == document.id,
        DocumentRevision.library_id == library.id,
    )
    if for_update:
        revision_statement = revision_statement.with_for_update()
    revision = (await db.execute(revision_statement)).scalar_one_or_none()
    if revision is None:
        _fail("processing_revision_unavailable")
    return _CurrentScope(document, revision)


async def _latest_artifact_jobs(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> dict[str, KnowledgeArtifactJob]:
    rows = (
        (
            await db.execute(
                select(KnowledgeArtifactJob)
                .where(
                    KnowledgeArtifactJob.library_id == library_id,
                    KnowledgeArtifactJob.document_id == document_id,
                    KnowledgeArtifactJob.document_revision_id == revision_id,
                )
                .distinct(KnowledgeArtifactJob.artifact_type)
                .order_by(
                    KnowledgeArtifactJob.artifact_type,
                    KnowledgeArtifactJob.created_at.desc(),
                    KnowledgeArtifactJob.retry_generation.desc(),
                    KnowledgeArtifactJob.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.artifact_type: row for row in rows}


async def _latest_classification_job(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> DocumentClassificationJob | None:
    return (
        await db.execute(
            select(DocumentClassificationJob)
            .where(
                DocumentClassificationJob.library_id == library_id,
                DocumentClassificationJob.document_id == document_id,
                DocumentClassificationJob.document_revision_id == revision_id,
            )
            .order_by(
                DocumentClassificationJob.created_at.desc(),
                DocumentClassificationJob.retry_generation.desc(),
                DocumentClassificationJob.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_graph_job(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> GraphExtractionJob | None:
    return (
        await db.execute(
            select(GraphExtractionJob)
            .where(
                GraphExtractionJob.library_id == library_id,
                GraphExtractionJob.document_id == document_id,
                GraphExtractionJob.document_revision_id == revision_id,
                GraphExtractionJob.execution_mode == "production",
            )
            .order_by(
                GraphExtractionJob.created_at.desc(),
                GraphExtractionJob.retry_generation.desc(),
                GraphExtractionJob.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _graph_counts(
    db,
    *,
    job_id: uuid.UUID,
    max_attempts: int,
) -> GraphProcessingCountsRead:
    retryable_count = func.count(GraphExtractionUnit.id).filter(
        and_(
            GraphExtractionUnit.status == "failed",
            GraphExtractionUnit.retryable.is_(True),
            GraphExtractionUnit.model_attempt_count < max_attempts,
        )
    )
    rows = (
        await db.execute(
            select(
                GraphExtractionUnit.status,
                func.count(GraphExtractionUnit.id),
                func.coalesce(func.sum(GraphExtractionUnit.model_attempt_count), 0),
                retryable_count,
            )
            .where(GraphExtractionUnit.job_id == job_id)
            .group_by(GraphExtractionUnit.status)
        )
    ).all()
    by_status = {str(status): int(count) for status, count, _attempts, _retryable in rows}
    return GraphProcessingCountsRead(
        total=sum(by_status.values()),
        queued=by_status.get("queued", 0),
        processing=by_status.get("processing", 0),
        succeeded=by_status.get("succeeded", 0),
        failed=by_status.get("failed", 0),
        cancelled=by_status.get("cancelled", 0),
        retryable_failed=sum(int(row[3] or 0) for row in rows),
        model_attempts=sum(int(row[2] or 0) for row in rows),
    )


def _stage_enabled(
    stage: ProcessingStage,
    *,
    library: Library,
    config: Settings,
) -> bool:
    if stage == "summary":
        return bool(
            config.knowledge_artifact_runtime_enabled
            and library.summary_artifact_enabled
        )
    if stage == "outline":
        return bool(
            config.knowledge_artifact_runtime_enabled
            and library.outline_artifact_enabled
        )
    if stage == "classification":
        return bool(config.classification_runtime_enabled)
    if stage == "graph":
        return bool(config.graph_extraction_enabled and library.graph_extraction_enabled)
    _fail("processing_stage_invalid")


def _stage_read(
    stage: ProcessingStage,
    *,
    enabled: bool,
    job: KnowledgeArtifactJob | DocumentClassificationJob | GraphExtractionJob | None,
    graph_counts: GraphProcessingCountsRead | None = None,
) -> DocumentProcessingStageRead:
    status = "not_started" if job is None else str(job.status)
    if status != "not_started" and status not in _JOB_STATUSES:
        _fail("processing_invariant_failed")
    retryable = False
    if enabled and job is not None:
        if stage in {"summary", "outline", "classification"}:
            retryable = status in {"failed", "cancelled"}
        else:
            retryable = bool(
                status in {"failed", "partially_succeeded"}
                and graph_counts is not None
                and graph_counts.retryable_failed > 0
            )
    return DocumentProcessingStageRead(
        stage=stage,
        availability="enabled" if enabled else "disabled",
        status=status,
        job_id=job.id if job is not None else None,
        retry_generation=int(job.retry_generation) if job is not None else 0,
        attempt_count=(
            graph_counts.model_attempts
            if stage == "graph" and graph_counts is not None
            else int(job.attempt_count)
            if job is not None and hasattr(job, "attempt_count")
            else None
        ),
        safe_error_code=(
            safe_stage_error_code(job.error_code) if job is not None else None
        ),
        retryable=retryable,
        created_at=job.created_at if job is not None else None,
        started_at=job.started_at if job is not None else None,
        updated_at=job.updated_at if job is not None else None,
        finished_at=job.finished_at if job is not None else None,
        graph_counts=graph_counts if stage == "graph" else None,
    )


async def _project_scope(
    db,
    *,
    library: Library,
    scope: _CurrentScope,
    config: Settings,
) -> DocumentProcessingRead:
    document, revision = scope.document, scope.revision
    artifact_jobs = await _latest_artifact_jobs(
        db,
        library_id=library.id,
        document_id=document.id,
        revision_id=revision.id,
    )
    classification_job = await _latest_classification_job(
        db,
        library_id=library.id,
        document_id=document.id,
        revision_id=revision.id,
    )
    graph_job = await _latest_graph_job(
        db,
        library_id=library.id,
        document_id=document.id,
        revision_id=revision.id,
    )
    counts = (
        await _graph_counts(
            db,
            job_id=graph_job.id,
            max_attempts=config.graph_extraction_worker_max_model_attempts,
        )
        if graph_job is not None
        else None
    )
    jobs = {
        "summary": artifact_jobs.get("summary"),
        "outline": artifact_jobs.get("outline"),
        "classification": classification_job,
        "graph": graph_job,
    }
    return DocumentProcessingRead(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        stages=[
            _stage_read(
                stage,
                enabled=_stage_enabled(stage, library=library, config=config),
                job=jobs[stage],
                graph_counts=counts if stage == "graph" else None,
            )
            for stage in PROCESSING_STAGES
        ],
    )


async def get_document_processing_diagnostics(
    db,
    *,
    library: Library,
    document_id: uuid.UUID,
    config: Settings = settings,
) -> DocumentProcessingRead:
    scope = await _load_current_scope(
        db,
        library=library,
        document_id=document_id,
    )
    return await _project_scope(db, library=library, scope=scope, config=config)


async def _source_job(
    db,
    *,
    library: Library,
    scope: _CurrentScope,
    stage: ProcessingStage,
    source_job_id: uuid.UUID,
):
    document, revision = scope.document, scope.revision
    if stage in {"summary", "outline"}:
        model = KnowledgeArtifactJob
        statement = select(model).where(
            model.id == source_job_id,
            model.library_id == library.id,
            model.document_id == document.id,
            model.document_revision_id == revision.id,
            model.artifact_type == stage,
        )
    elif stage == "classification":
        model = DocumentClassificationJob
        statement = select(model).where(
            model.id == source_job_id,
            model.library_id == library.id,
            model.document_id == document.id,
            model.document_revision_id == revision.id,
        )
    elif stage == "graph":
        model = GraphExtractionJob
        statement = select(model).where(
            model.id == source_job_id,
            model.library_id == library.id,
            model.document_id == document.id,
            model.document_revision_id == revision.id,
            model.execution_mode == "production",
        )
    else:
        _fail("processing_stage_invalid")
    job = (await db.execute(statement.with_for_update())).scalar_one_or_none()
    if job is None:
        _fail("processing_job_stale")
    return job


async def retry_document_processing_stage(
    db,
    *,
    library: Library,
    document_id: uuid.UUID,
    stage: ProcessingStage,
    source_job_id: uuid.UUID,
    observed_retry_generation: int,
    actor_user_id: uuid.UUID,
    config: Settings = settings,
) -> DocumentProcessingRead:
    if stage not in PROCESSING_STAGES:
        _fail("processing_stage_invalid")
    scope = await _load_current_scope(
        db,
        library=library,
        document_id=document_id,
        for_update=True,
    )
    if not _stage_enabled(stage, library=library, config=config):
        _fail("processing_stage_unavailable")
    source = await _source_job(
        db,
        library=library,
        scope=scope,
        stage=stage,
        source_job_id=source_job_id,
    )
    if source.retry_generation != observed_retry_generation:
        _fail("processing_job_stale")

    try:
        if stage in {"summary", "outline"}:
            result = await retry_knowledge_artifact_job(
                db,
                source_job=source,
                library=library,
                document=scope.document,
                revision=scope.revision,
                requested_by_user_id=actor_user_id,
                config=config,
            )
        elif stage == "classification":
            result = await retry_classification_job(
                db,
                source_job=source,
                library=library,
                document=scope.document,
                revision=scope.revision,
                requested_by_user_id=actor_user_id,
                config=config,
            )
        else:
            latest_graph = await _latest_graph_job(
                db,
                library_id=library.id,
                document_id=scope.document.id,
                revision_id=scope.revision.id,
            )
            if latest_graph is None or latest_graph.id != source.id:
                _fail("processing_job_stale")
            result = await retry_graph_extraction_job(
                db,
                library=library,
                job_id=source.id,
            )
    except (
        KnowledgeArtifactRuntimeError,
        ClassificationRuntimeError,
        GraphExtractionJobError,
    ) as exc:
        raise DocumentProcessingError(retry_rejection_code(exc.code)) from exc

    await audit_log.record(
        db,
        actor_user_id,
        "catalog.processing_retry",
        {
            "organization_id": str(library.organization_id),
            "library_id": str(library.id),
            "document_id": str(scope.document.id),
            "document_revision_id": str(scope.revision.id),
            "stage": stage,
            "source_job_id": str(source.id),
            "result_job_id": str(result.id),
            "retry_generation": int(result.retry_generation),
        },
    )
    return await _project_scope(db, library=library, scope=scope, config=config)
