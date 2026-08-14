"""admin embedding_jobs 监控：列表 / 重试。"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.config import settings
from app.db import get_db
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import (
    EmbeddingJobRead,
    EmbeddingJobStats,
    TaskMonitorRetryRequest,
    TaskMonitorRetryResponse,
    TaskMonitorRetryResult,
    TaskMonitorRead,
    TaskMonitorStats,
)
from app.services import audit_log, import_uploads
from app.services.graph_extraction_jobs import GraphExtractionJobError, retry_graph_extraction_job

router = APIRouter(prefix="/admin/jobs", tags=["admin"])

_IMPORT_STATUS = {
    "uploading": "pending",
    "queued": "pending",
    "processing": "processing",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "superseded": "superseded",
}
_EMBEDDING_STATUS = {
    "pending": "pending",
    "processing": "processing",
    "done": "done",
    "failed": "failed",
    "superseded": "superseded",
}
_GRAPH_STATUS = {
    "waiting_schema": "pending",
    "queued": "pending",
    "processing": "processing",
    "partially_succeeded": "done",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "superseded": "superseded",
}


def _retry_metadata(task_type: str, status: str, retryable: bool) -> tuple[str, str]:
    if retryable:
        return "supported", "可重试"
    if status == "done":
        return "not_needed", "已成功，无需重试"
    if status == "cancelled":
        return "cancelled", "已取消"
    if status == "superseded":
        return "superseded", "已覆盖"
    if task_type != "embedding":
        return "unsupported", "当前任务类型暂不支持"
    return "unavailable", "当前不可重试"


def _embedding_retry_state(job: EmbeddingJob) -> tuple[bool, str, str]:
    if job.status == "failed" and job.attempt_count >= settings.embed_worker_max_attempts:
        return False, "exhausted", "尝试次数耗尽"
    retryable = job.status == "failed"
    capability, reason = _retry_metadata(
        "embedding", _EMBEDDING_STATUS.get(job.status, job.status), retryable
    )
    return retryable, capability, reason


def _graph_retry_state(
    job: GraphExtractionJob,
    *,
    latest_production: bool,
    has_retryable_units: bool,
) -> tuple[bool, str, str]:
    normalized = _GRAPH_STATUS.get(job.status, job.status)
    if job.status in {"cancelled", "superseded"}:
        capability, reason = _retry_metadata("graph", normalized, False)
        return False, capability, reason
    if job.execution_mode != "production" or not latest_production:
        return False, "stale", "旧版本或非 production 图谱任务不可重试"
    if job.status != "failed":
        capability, reason = _retry_metadata("graph", normalized, False)
        return False, capability, reason
    if not has_retryable_units:
        return False, "exhausted", "没有剩余预算的可重试图谱单元"
    return True, "supported", "可重试"


def _embedding_monitor_row(
    job: EmbeddingJob,
    *,
    title: Optional[str] = None,
) -> TaskMonitorRead:
    normalized = _EMBEDDING_STATUS.get(job.status, job.status)
    retryable = job.status == "pending" or (
        job.status == "failed" and job.attempt_count < settings.embed_worker_max_attempts
    )
    if job.status == "failed" and not retryable:
        retry_capability, retry_reason = "exhausted", "尝试次数耗尽"
    else:
        retry_capability, retry_reason = _retry_metadata("embedding", normalized, retryable)
    retryable, retry_capability, retry_reason = _embedding_retry_state(job)
    return TaskMonitorRead(
        id=job.id,
        task_type="embedding",
        title=title,
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision=job.document_revision,
        document_revision_id=job.document_revision_id,
        status=normalized,
        raw_status=job.status,
        stage="embedding",
        worker_id=job.worker_id,
        attempt_count=job.attempt_count,
        last_error=job.last_error,
        created_at=job.created_at,
        claimed_at=job.claimed_at,
        finished_at=job.finished_at,
        retry_target_type="embedding",
        retry_target_id=job.id,
        retry_generation=job.attempt_count,
        retryable=retryable,
        retry_capability=retry_capability,
        retry_reason=retry_reason,
        raw_error=job.last_error,
    )


def _graph_monitor_row(
    job: GraphExtractionJob,
    *,
    title: Optional[str] = None,
    revision_no: Optional[int] = None,
    publication_status: Optional[str] = None,
    latest_production: bool = False,
    has_retryable_units: bool = False,
) -> TaskMonitorRead:
    counts = dict(job.counts or {})
    statistics = dict(job.statistics or {})
    total = int(counts.get("total") or 0)
    completed = int(counts.get("succeeded") or 0) + int(counts.get("failed") or 0)
    batch_size = max(1, int((job.model_config_snapshot or {}).get("batch_size") or 1))
    planned_batches = math.ceil(total / batch_size) if total else 0
    completed_batches = min(planned_batches, math.ceil(completed / batch_size))
    eta_seconds = None
    if job.started_at is not None and completed > 0 and completed < total:
        elapsed = max(
            0.0,
            (datetime.now(timezone.utc) - job.started_at).total_seconds(),
        )
        if elapsed > 0:
            eta_seconds = round((total - completed) * elapsed / completed)
    provider_gate = statistics.get("provider_gate")
    if not isinstance(provider_gate, dict):
        provider_gate = {}
    candidate_pipeline = statistics.get("candidate_pipeline")
    candidate_pipeline = candidate_pipeline if isinstance(candidate_pipeline, dict) else {}
    routing = candidate_pipeline.get("routing")
    routing = routing if isinstance(routing, dict) else {}
    entity_routing = routing.get("entities")
    entity_routing = entity_routing if isinstance(entity_routing, dict) else {}
    relation_routing = routing.get("relations")
    relation_routing = relation_routing if isinstance(relation_routing, dict) else {}
    materialization = statistics.get("materialization")
    materialization = materialization if isinstance(materialization, dict) else {}
    publication = statistics.get("publication")
    publication = publication if isinstance(publication, dict) else {}
    normalized_status = _GRAPH_STATUS.get(job.status, job.status)
    in_flight = provider_gate.get("in_flight") if normalized_status == "processing" else None
    retryable, retry_capability, retry_reason = _graph_retry_state(
        job,
        latest_production=latest_production,
        has_retryable_units=has_retryable_units,
    )
    return TaskMonitorRead(
        id=job.id,
        task_type="graph",
        title=title,
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision=revision_no,
        document_revision_id=job.document_revision_id,
        status=normalized_status,
        raw_status=job.status,
        stage=job.current_stage or ("preparing" if job.status == "queued" else None),
        attempt_count=job.retry_generation,
        last_error=job.error_message,
        created_at=job.created_at,
        claimed_at=job.started_at,
        finished_at=job.finished_at,
        retry_target_type="graph",
        retry_target_id=job.id,
        retry_generation=job.retry_generation,
        retryable=retryable,
        retry_capability=retry_capability,
        retry_reason=retry_reason,
        raw_error=job.error_message,
        build_mode=job.build_mode,
        publication_status=publication_status,
        progress={
            "completed": completed,
            "total": total,
            "percent": round(completed * 100 / total, 1) if total else 0.0,
            "queued": int(counts.get("queued") or 0),
            "processing": int(counts.get("processing") or 0),
            "completed_batches": completed_batches,
            "planned_batches": planned_batches,
        },
        metrics={
            "eta_seconds": eta_seconds,
            "cache_hits": int(statistics.get("cache_hits") or 0),
            "configured_concurrency": provider_gate.get("configured_concurrency"),
            "effective_concurrency": provider_gate.get("effective_concurrency"),
            "in_flight": in_flight,
            "throttled_count": int(provider_gate.get("throttled_count") or 0),
            "retry_count": int(provider_gate.get("retry_count") or 0),
            "stage_counts": {
                "extraction": {
                    "entities": int(candidate_pipeline.get("entity_candidate_count") or 0),
                    "relations": int(candidate_pipeline.get("relation_candidate_count") or 0),
                },
                "validation": {
                    "entities": int(entity_routing.get("validated") or 0),
                    "relations": int(relation_routing.get("validated") or 0),
                },
                "materialization": {
                    "entities": int(
                        materialization.get("publishable_entity_candidate_count") or 0
                    ),
                    "relations": int(materialization.get("publishable_relation_count") or 0),
                },
                "publication": {
                    "entities": int(publication.get("entity_count") or 0),
                    "relations": int(publication.get("relation_count") or 0),
                },
            },
            "failure_reasons": {
                "candidates": candidate_pipeline.get("failure_reasons") or {},
                "materialization": materialization.get("failure_reasons") or {},
                "publication": publication.get("failure_reason"),
            },
            "publication_diff": publication.get("diff"),
            "current_graph_unchanged": bool(
                publication.get("current_graph_unchanged")
            ),
        },
    )


def _import_monitor_row(
    job: DocumentImportJob,
    *,
    embedding_job: Optional[EmbeddingJob] = None,
    graph_job: Optional[GraphExtractionJob] = None,
    revision_no: Optional[int] = None,
    graph_retry_state: Optional[tuple[bool, str, str]] = None,
) -> TaskMonitorRead:
    status_value = _IMPORT_STATUS.get(job.status, job.status)
    raw_status = job.status
    stage = job.current_stage
    worker_id = job.worker_id
    last_error = job.last_error
    raw_error = job.last_error
    finished_at = job.finished_at

    follows_downstream = (
        job.status == "processing"
        and job.current_stage in {"embedding", "graph"}
        and job.embedding_job_id is not None
    )
    if follows_downstream:
        if embedding_job is None:
            status_value = "failed"
            raw_status = "embedding_missing"
            last_error = "embedding job is missing"
        elif embedding_job.status == "failed":
            status_value = "failed"
            raw_status = embedding_job.status
            stage = "embedding"
            worker_id = embedding_job.worker_id
            last_error = embedding_job.last_error
            raw_error = embedding_job.last_error
            finished_at = embedding_job.finished_at
        elif embedding_job.status in {"pending", "processing"}:
            status_value = _EMBEDDING_STATUS[embedding_job.status]
            raw_status = embedding_job.status
            stage = "embedding"
            worker_id = embedding_job.worker_id
        elif embedding_job.status == "superseded":
            status_value = "superseded"
            raw_status = embedding_job.status
            stage = "completed"
        elif not job.graph_extraction_requested or job.document_revision_id is None:
            status_value = "done"
            raw_status = embedding_job.status
            stage = "completed"
            finished_at = embedding_job.finished_at
        elif graph_job is None:
            status_value = "processing"
            raw_status = "awaiting_graph"
            stage = "awaiting_graph"
            worker_id = None
        else:
            status_value = (
                "processing"
                if graph_job.status in {"queued", "processing"}
                else _GRAPH_STATUS.get(graph_job.status, graph_job.status)
            )
            raw_status = graph_job.status
            stage = graph_job.current_stage or ("preparing" if graph_job.status == "queued" else "graph")
            worker_id = None
            if status_value in {"done", "failed", "cancelled", "superseded"}:
                finished_at = graph_job.finished_at
            if status_value == "failed":
                last_error = graph_job.error_message
                raw_error = graph_job.error_message

    retry_target_type = "import"
    retry_target_id = job.id
    retry_generation = job.attempt_count
    retryable = False
    retry_capability, retry_reason = _retry_metadata("import", status_value, False)
    if follows_downstream and embedding_job is not None and embedding_job.status == "failed":
        retry_target_type = "embedding"
        retry_target_id = embedding_job.id
        retry_generation = embedding_job.attempt_count
        retryable, retry_capability, retry_reason = _embedding_retry_state(embedding_job)
    elif follows_downstream and graph_job is not None and graph_job.status == "failed":
        retry_target_type = "graph"
        retry_target_id = graph_job.id
        retry_generation = graph_job.retry_generation
        retryable, retry_capability, retry_reason = graph_retry_state or (
            False,
            "stale",
            "图谱重试状态不可用",
        )
    elif job.status == "failed":
        retryable = job.attempt_count < settings.import_worker_max_attempts
        if not retryable:
            retry_capability, retry_reason = "exhausted", "导入尝试次数耗尽"
        elif retryable:
            retry_capability, retry_reason = "supported", "可重试"
    return TaskMonitorRead(
        id=job.id,
        task_type="import",
        title=job.relative_path or job.file_name,
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision=revision_no,
        document_revision_id=job.document_revision_id,
        status=status_value,
        raw_status=raw_status,
        stage=stage,
        worker_id=worker_id,
        attempt_count=job.attempt_count,
        last_error=last_error,
        created_at=job.created_at,
        claimed_at=job.claimed_at,
        finished_at=finished_at,
        retry_target_type=retry_target_type,
        retry_target_id=retry_target_id,
        retry_generation=retry_generation,
        retryable=retryable,
        retry_capability=retry_capability,
        retry_reason=retry_reason,
        raw_error=raw_error,
    )


def _graph_publication_status(
    job: GraphExtractionJob,
    publication: GraphPublication | None,
) -> str:
    if publication is not None:
        if publication.status in {"active", "degraded", "superseded"}:
            return "available"
        if publication.status in {"planned", "activating"}:
            return "publishing"
        return "retryable"
    if job.status in {"queued", "processing"}:
        return "pending"
    materialization = (job.statistics or {}).get("materialization")
    if (
        isinstance(materialization, dict)
        and materialization.get("outcome") == "entities_only"
    ):
        return "entities_only"
    if isinstance(materialization, dict) and any(
        isinstance(value, int) and value > 0 for value in materialization.values()
    ):
        return "retryable"
    return "not_required"


async def _load_monitor_tasks(
    db: AsyncSession,
    *,
    library_id: Optional[uuid.UUID] = None,
    per_type_limit: Optional[int] = None,
) -> list[TaskMonitorRead]:
    def scoped(model):
        stmt = select(model).order_by(model.created_at.desc(), model.id.desc())
        if library_id is not None:
            stmt = stmt.where(model.library_id == library_id)
        if per_type_limit is not None:
            stmt = stmt.limit(per_type_limit)
        return stmt

    import_jobs = list((await db.execute(scoped(DocumentImportJob))).scalars().all())
    embedding_jobs = list((await db.execute(scoped(EmbeddingJob))).scalars().all())
    graph_jobs = list((await db.execute(scoped(GraphExtractionJob))).scalars().all())
    latest_production_graph_ids: set[uuid.UUID] = set()
    latest_production_by_revision: dict[uuid.UUID, uuid.UUID] = {}
    for graph_job in graph_jobs:
        if graph_job.execution_mode != "production":
            continue
        if graph_job.document_revision_id is None:
            continue
        if graph_job.document_revision_id not in latest_production_by_revision:
            latest_production_by_revision[graph_job.document_revision_id] = graph_job.id
    latest_production_graph_ids.update(latest_production_by_revision.values())
    retryable_graph_ids: set[uuid.UUID] = set()
    graph_ids = {job.id for job in graph_jobs}
    if graph_ids:
        retryable_units = (
            await db.execute(
                select(GraphExtractionUnit.job_id)
                .where(
                    GraphExtractionUnit.job_id.in_(graph_ids),
                    GraphExtractionUnit.status == "failed",
                    GraphExtractionUnit.retryable.is_(True),
                    GraphExtractionUnit.model_attempt_count
                    < settings.graph_extraction_worker_max_model_attempts,
                )
                .group_by(GraphExtractionUnit.job_id)
            )
        ).all()
        retryable_graph_ids = {row[0] for row in retryable_units}
    publication_keys = {
        f"graph-extraction:auto-publish:{job.id}:v1": job.id for job in graph_jobs
    }
    publications_by_job_id: dict[uuid.UUID, GraphPublication] = {}
    if publication_keys:
        publications = (
            (
                await db.execute(
                    select(GraphPublication)
                    .where(GraphPublication.idempotency_key.in_(publication_keys))
                    .order_by(GraphPublication.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for publication in publications:
            job_id = publication_keys.get(publication.idempotency_key)
            if job_id is not None:
                publications_by_job_id.setdefault(job_id, publication)

    embedding_ids = {job.embedding_job_id for job in import_jobs if job.embedding_job_id is not None}
    embeddings_by_id = {job.id: job for job in embedding_jobs}
    missing_embedding_ids = embedding_ids - embeddings_by_id.keys()
    if missing_embedding_ids:
        related_embeddings = (
            (await db.execute(select(EmbeddingJob).where(EmbeddingJob.id.in_(missing_embedding_ids))))
            .scalars()
            .all()
        )
        embeddings_by_id.update({job.id: job for job in related_embeddings})

    import_revision_ids = {
        job.document_revision_id for job in import_jobs if job.document_revision_id is not None
    }
    latest_graph_by_revision: dict[uuid.UUID, GraphExtractionJob] = {}
    if import_revision_ids:
        related_graph_jobs = (
            (
                await db.execute(
                    select(GraphExtractionJob)
                    .where(
                        GraphExtractionJob.document_revision_id.in_(import_revision_ids),
                        GraphExtractionJob.execution_mode == "production",
                    )
                    .order_by(
                        GraphExtractionJob.document_revision_id,
                        GraphExtractionJob.created_at.desc(),
                        GraphExtractionJob.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for graph_job in related_graph_jobs:
            latest_graph_by_revision.setdefault(graph_job.document_revision_id, graph_job)

    document_ids = {job.document_id for job in [*embedding_jobs, *graph_jobs] if job.document_id is not None}
    documents_by_id: dict[uuid.UUID, Document] = {}
    if document_ids:
        documents = (await db.execute(select(Document).where(Document.id.in_(document_ids)))).scalars().all()
        documents_by_id = {document.id: document for document in documents}

    revision_ids = {
        job.document_revision_id
        for job in [*import_jobs, *embedding_jobs, *graph_jobs]
        if job.document_revision_id is not None
    }
    revisions_by_id: dict[uuid.UUID, DocumentRevision] = {}
    if revision_ids:
        revisions = (
            (await db.execute(select(DocumentRevision).where(DocumentRevision.id.in_(revision_ids))))
            .scalars()
            .all()
        )
        revisions_by_id = {revision.id: revision for revision in revisions}

    rows: list[TaskMonitorRead] = []
    for job in import_jobs:
        revision = revisions_by_id.get(job.document_revision_id)
        rows.append(
            _import_monitor_row(
                job,
                embedding_job=embeddings_by_id.get(job.embedding_job_id),
                graph_job=latest_graph_by_revision.get(job.document_revision_id),
                revision_no=revision.revision_no if revision else None,
                graph_retry_state=(
                    _graph_retry_state(
                        latest_graph_by_revision[job.document_revision_id],
                        latest_production=True,
                        has_retryable_units=(
                            latest_graph_by_revision[job.document_revision_id].id
                            in retryable_graph_ids
                        ),
                    )
                    if job.document_revision_id in latest_graph_by_revision
                    else None
                ),
            )
        )
    for job in embedding_jobs:
        document = documents_by_id.get(job.document_id)
        rows.append(_embedding_monitor_row(job, title=document.title if document else None))
    for job in graph_jobs:
        document = documents_by_id.get(job.document_id)
        revision = revisions_by_id.get(job.document_revision_id)
        rows.append(
            _graph_monitor_row(
                job,
                title=document.title if document else None,
                revision_no=revision.revision_no if revision else None,
                publication_status=_graph_publication_status(
                    job,
                    publications_by_job_id.get(job.id),
                ),
                latest_production=job.id in latest_production_graph_ids,
                has_retryable_units=job.id in retryable_graph_ids,
            )
        )
    return sorted(rows, key=lambda row: (row.created_at, str(row.id)), reverse=True)


@router.get("", response_model=list[EmbeddingJobRead])
async def list_jobs(
    status_filter: Optional[str] = Query(
        default=None, alias="status", pattern="^(pending|processing|done|failed)$"
    ),
    library_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[EmbeddingJob]:
    stmt = select(EmbeddingJob).order_by(EmbeddingJob.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        stmt = stmt.where(EmbeddingJob.status == status_filter)
    if library_id:
        stmt = stmt.where(EmbeddingJob.library_id == library_id)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.get("/stats", response_model=EmbeddingJobStats)
async def jobs_stats(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingJobStats:
    """按状态聚合任务计数，供后台统计条 / 监控用。"""
    rows = await db.execute(select(EmbeddingJob.status, func.count()).group_by(EmbeddingJob.status))
    counts = {row_status: cnt for row_status, cnt in rows.all()}
    return EmbeddingJobStats(
        pending=counts.get("pending", 0),
        processing=counts.get("processing", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        total=sum(counts.values()),
    )


@router.get("/monitor", response_model=list[TaskMonitorRead])
async def monitor_jobs(
    task_type: Optional[str] = Query(default=None, pattern="^(import|embedding|graph)$"),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        pattern="^(pending|processing|done|failed|cancelled|superseded)$",
    ),
    library_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[TaskMonitorRead]:
    """Persistent monitor covering import, embedding, and graph extraction jobs."""
    rows = await _load_monitor_tasks(db, library_id=library_id)
    if task_type is not None:
        rows = [row for row in rows if row.task_type == task_type]
    if status_filter is not None:
        rows = [row for row in rows if row.status == status_filter]
    return rows[offset : offset + limit]


@router.get("/monitor/stats", response_model=TaskMonitorStats)
async def monitor_job_stats(
    library_id: Optional[uuid.UUID] = Query(default=None),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> TaskMonitorStats:
    rows = await _load_monitor_tasks(db, library_id=library_id)
    counts = {
        key: sum(row.status == key for row in rows)
        for key in (
            "pending",
            "processing",
            "done",
            "failed",
            "cancelled",
            "superseded",
        )
    }
    return TaskMonitorStats(
        **counts,
        retryable_failed=sum(row.retryable and row.status == "failed" for row in rows),
        total=len(rows),
        retryable_embedding_failed=sum(
            row.retryable and row.status == "failed" and row.retry_target_type == "embedding"
            for row in rows
        ),
    )


def _retry_error_message(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", None) or str(exc) or "retry_rejected"
    return str(code), str(exc)


@router.post("/monitor/retry", response_model=TaskMonitorRetryResponse)
async def retry_monitored_tasks(
    body: TaskMonitorRetryRequest,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> TaskMonitorRetryResponse:
    results: list[TaskMonitorRetryResult] = []
    seen_targets: set[tuple[str, uuid.UUID]] = set()

    async def record_result(result: TaskMonitorRetryResult, observed_generation: int) -> None:
        await audit_log.record(
            db,
            actor.id,
            "job.monitor_retry",
            {
                "task_type": result.task_type,
                "job_id": str(result.job_id),
                "status": result.status,
                "observed_generation": observed_generation,
                "retry_generation": result.retry_generation,
                "reason": result.reason,
            },
        )

    for item in body.items:
        target = (item.task_type, item.job_id)
        if target in seen_targets:
            result = TaskMonitorRetryResult(
                task_type=item.task_type,
                job_id=item.job_id,
                status="rejected",
                reason="duplicate_target",
                message="同一底层任务已在本批次中处理",
            )
            results.append(result)
            await record_result(result, item.observed_generation)
            continue
        seen_targets.add(target)

        try:
            if item.task_type == "embedding":
                job = await db.get(EmbeddingJob, item.job_id, with_for_update=True)
                if job is None:
                    raise ValueError("job_not_found")
                if job.attempt_count != item.observed_generation:
                    raise ValueError("stale_generation")
                if job.status != "failed":
                    raise ValueError("job_not_retryable")
                if job.attempt_count >= settings.embed_worker_max_attempts:
                    raise ValueError("attempt_budget_exhausted")
                job.status = "pending"
                job.attempt_count = 0
                job.worker_id = None
                job.last_error = None
                job.finished_at = None
                job.claimed_at = None
                await db.flush()
                result = TaskMonitorRetryResult(
                    task_type=item.task_type,
                    job_id=item.job_id,
                    status="succeeded",
                    retry_generation=job.attempt_count,
                    reason="retried",
                    message="向量任务已重新排队",
                )
            elif item.task_type == "import":
                job = await db.get(DocumentImportJob, item.job_id, with_for_update=True)
                if job is None:
                    raise ValueError("job_not_found")
                if job.attempt_count != item.observed_generation:
                    raise ValueError("stale_generation")
                await import_uploads.retry_job(db, job=job)
                result = TaskMonitorRetryResult(
                    task_type=item.task_type,
                    job_id=item.job_id,
                    status="succeeded",
                    retry_generation=job.attempt_count,
                    reason="retried",
                    message="导入任务已重新排队",
                )
            else:
                job = await db.get(GraphExtractionJob, item.job_id, with_for_update=True)
                if job is None:
                    raise ValueError("job_not_found")
                if job.retry_generation != item.observed_generation:
                    raise ValueError("stale_generation")
                if job.execution_mode != "production":
                    raise ValueError("stale_job")
                latest_id = (
                    await db.execute(
                        select(GraphExtractionJob.id)
                        .where(
                            GraphExtractionJob.library_id == job.library_id,
                            GraphExtractionJob.document_revision_id == job.document_revision_id,
                            GraphExtractionJob.execution_mode == "production",
                        )
                        .order_by(GraphExtractionJob.created_at.desc(), GraphExtractionJob.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if latest_id != job.id:
                    raise ValueError("stale_job")
                if job.status != "failed":
                    raise ValueError("job_not_retryable")
                library = await db.get(Library, job.library_id)
                if library is None:
                    raise ValueError("library_not_found")
                retried = await retry_graph_extraction_job(
                    db,
                    library=library,
                    job_id=job.id,
                )
                result = TaskMonitorRetryResult(
                    task_type=item.task_type,
                    job_id=item.job_id,
                    status="succeeded",
                    retry_generation=retried.retry_generation,
                    reason="retried",
                    message="图谱任务已重新排队",
                )
        except (ValueError, GraphExtractionJobError, import_uploads.ImportUploadError) as exc:
            reason, message = _retry_error_message(exc)
            result = TaskMonitorRetryResult(
                task_type=item.task_type,
                job_id=item.job_id,
                status="rejected",
                reason=reason,
                message=message,
            )
        results.append(result)
        await record_result(result, item.observed_generation)

    await db.commit()
    return TaskMonitorRetryResponse(results=results)


@router.post("/reset-failed")
async def reset_failed_jobs(
    library_id: Optional[uuid.UUID] = Query(
        default=None, description="只重置该库的 failed 任务；不传=全局重置"
    ),
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """批量把 failed 任务重置为可重跑（attempt_count 归零）。返回重置条数。

    传 library_id 则仅重置该库（多部门共用时避免误伤其它库）；不传维持全局重置。
    与单条 retry 一致：只动 job，不动 document —— worker 成功后会自己把文档置 ready。
    """
    stmt = (
        update(EmbeddingJob)
        .where(
            EmbeddingJob.status == "failed",
            EmbeddingJob.attempt_count < settings.embed_worker_max_attempts,
        )
        .values(
            status="pending",
            attempt_count=0,
            last_error=None,
            worker_id=None,
            finished_at=None,
            claimed_at=None,
        )
    )
    if library_id is not None:
        stmt = stmt.where(EmbeddingJob.library_id == library_id)
    result = await db.execute(stmt)
    count = result.rowcount or 0
    await audit_log.record(
        db,
        actor.id,
        "job.reset_failed",
        {"reset_count": count, "library_id": str(library_id) if library_id else None},
    )
    await db.commit()
    return {"reset_count": count}


@router.post("/{job_id}/retry", response_model=EmbeddingJobRead)
async def retry_job(
    job_id: uuid.UUID,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingJob:
    job = await db.get(EmbeddingJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status == "processing":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot retry active processing job")
    if job.status not in ("failed", "pending"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cannot retry job in status={job.status}")
    if job.status == "failed" and job.attempt_count >= settings.embed_worker_max_attempts:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot retry job: attempt limit exhausted")
    await db.execute(
        update(EmbeddingJob)
        .where(EmbeddingJob.id == job_id)
        # attempt_count 归零：否则失败满 max_attempts 的任务永远不被 worker 领取（领取条件 attempt_count < max）
        .values(
            status="pending",
            attempt_count=0,
            worker_id=None,
            last_error=None,
            finished_at=None,
            claimed_at=None,
        )
    )
    await audit_log.record(db, actor.id, "job.retry", {"job_id": str(job_id)})
    await db.commit()
    await db.refresh(job)
    return job
