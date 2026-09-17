"""admin embedding_jobs 监控：列表 / 重试。"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, literal, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

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
    TaskQueueHealth,
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

_IMPORT_STATUS_CANDIDATES = {
    "pending": ("uploading", "queued", "processing"),
    "processing": ("processing",),
    "done": ("succeeded", "processing"),
    "failed": ("failed", "processing"),
    "cancelled": ("cancelled", "processing"),
    "superseded": ("superseded", "processing"),
}
_GRAPH_STATUS_CANDIDATES = {
    "pending": ("waiting_schema", "queued"),
    "processing": ("processing",),
    "done": ("partially_succeeded", "succeeded"),
    "failed": ("failed",),
    "cancelled": ("cancelled",),
    "superseded": ("superseded",),
}


def _monitor_status_candidates(
    task_type: str,
    status_filter: Optional[str],
) -> Optional[tuple[str, ...]]:
    if status_filter is None:
        return None
    if task_type == "import":
        return _IMPORT_STATUS_CANDIDATES[status_filter]
    if task_type == "embedding":
        return (status_filter,)
    if task_type == "graph":
        return _GRAPH_STATUS_CANDIDATES[status_filter]
    return None

_QUEUE_HEALTH_WINDOW_SECONDS = 900
_IMPORT_WORKER_STAGES = ("validating", "parsing", "chunking")


def _queue_health_select(
    queue_name: str,
    model,
    *,
    pending_filter,
    processing_filter,
    succeeded_filter,
    failed_filter,
    library_id: Optional[uuid.UUID],
):
    oldest_pending_age = func.extract(
        "epoch",
        func.now() - func.min(model.created_at).filter(pending_filter),
    )
    if succeeded_filter is None or failed_filter is None:
        succeeded_count = literal(None)
        failed_count = literal(None)
    else:
        succeeded_count = func.count(model.id).filter(succeeded_filter)
        failed_count = func.count(model.id).filter(failed_filter)
    stmt = select(
        literal(queue_name),
        func.count(model.id).filter(pending_filter),
        oldest_pending_age,
        func.count(model.id).filter(processing_filter),
        succeeded_count,
        failed_count,
    )
    if library_id is not None:
        stmt = stmt.where(model.library_id == library_id)
    return stmt


async def _load_queue_health(
    db: AsyncSession,
    *,
    library_id: Optional[uuid.UUID],
) -> dict[str, TaskQueueHealth]:
    window_start = datetime.now(timezone.utc) - timedelta(seconds=_QUEUE_HEALTH_WINDOW_SECONDS)
    legacy_doc = func.lower(DocumentImportJob.file_name).like("%.doc")
    import_pending = and_(
        DocumentImportJob.status == "queued",
        or_(~legacy_doc, DocumentImportJob.conversion_sha256.is_not(None)),
    )
    doc_conversion_pending = and_(
        DocumentImportJob.status == "queued",
        legacy_doc,
        DocumentImportJob.conversion_sha256.is_(None),
    )
    statements = (
        _queue_health_select(
            "import",
            DocumentImportJob,
            pending_filter=import_pending,
            processing_filter=and_(
                DocumentImportJob.status == "processing",
                DocumentImportJob.current_stage.in_(_IMPORT_WORKER_STAGES),
            ),
            succeeded_filter=or_(
                and_(
                    DocumentImportJob.status == "succeeded",
                    DocumentImportJob.finished_at >= window_start,
                ),
                and_(
                    DocumentImportJob.status == "processing",
                    DocumentImportJob.current_stage == "embedding",
                    DocumentImportJob.worker_id.is_(None),
                    DocumentImportJob.updated_at >= window_start,
                ),
            ),
            failed_filter=and_(
                DocumentImportJob.status == "failed",
                DocumentImportJob.current_stage.in_(_IMPORT_WORKER_STAGES),
                DocumentImportJob.finished_at >= window_start,
            ),
            library_id=library_id,
        ),
        _queue_health_select(
            "doc_conversion",
            DocumentImportJob,
            pending_filter=doc_conversion_pending,
            processing_filter=and_(
                DocumentImportJob.status == "processing",
                DocumentImportJob.current_stage == "converting",
            ),
            succeeded_filter=None,
            failed_filter=None,
            library_id=library_id,
        ),
        _queue_health_select(
            "embedding",
            EmbeddingJob,
            pending_filter=EmbeddingJob.status == "pending",
            processing_filter=EmbeddingJob.status == "processing",
            succeeded_filter=and_(
                EmbeddingJob.status == "done",
                EmbeddingJob.finished_at >= window_start,
            ),
            failed_filter=and_(
                EmbeddingJob.status == "failed",
                EmbeddingJob.finished_at >= window_start,
            ),
            library_id=library_id,
        ),
        _queue_health_select(
            "graph",
            GraphExtractionJob,
            pending_filter=GraphExtractionJob.status == "queued",
            processing_filter=GraphExtractionJob.status == "processing",
            succeeded_filter=and_(
                GraphExtractionJob.status.in_(("partially_succeeded", "succeeded")),
                GraphExtractionJob.finished_at >= window_start,
            ),
            failed_filter=and_(
                GraphExtractionJob.status == "failed",
                GraphExtractionJob.finished_at >= window_start,
            ),
            library_id=library_id,
        ),
    )
    result = await db.execute(union_all(*statements))
    queues: dict[str, TaskQueueHealth] = {}
    for queue_name, pending, oldest_age, processing, succeeded, failed in result.all():
        terminal = None if succeeded is None or failed is None else int(succeeded) + int(failed)
        queues[str(queue_name)] = TaskQueueHealth(
            pending_count=int(pending),
            oldest_pending_age_seconds=(
                max(0, int(float(oldest_age))) if oldest_age is not None else None
            ),
            processing_count=int(processing),
            throughput_per_minute=(
                round(terminal / (_QUEUE_HEALTH_WINDOW_SECONDS / 60), 3)
                if terminal is not None
                else None
            ),
            failure_rate=(int(failed) / terminal if terminal else None),
            window_seconds=_QUEUE_HEALTH_WINDOW_SECONDS,
        )
    return queues


def _monitor_stats_select(*, library_id: Optional[uuid.UUID]):
    ranked_graphs = select(
        GraphExtractionJob.id.label("id"),
        GraphExtractionJob.document_revision_id.label("document_revision_id"),
        GraphExtractionJob.status.label("status"),
        func.row_number()
        .over(
            partition_by=GraphExtractionJob.document_revision_id,
            order_by=(GraphExtractionJob.created_at.desc(), GraphExtractionJob.id.desc()),
        )
        .label("rank"),
    ).where(GraphExtractionJob.execution_mode == "production")
    if library_id is not None:
        ranked_graphs = ranked_graphs.where(GraphExtractionJob.library_id == library_id)
    ranked_graphs = ranked_graphs.cte("ranked_production_graphs")
    latest_graph = (
        select(
            ranked_graphs.c.id,
            ranked_graphs.c.document_revision_id,
            ranked_graphs.c.status,
        )
        .where(ranked_graphs.c.rank == 1)
        .cte("latest_production_graph")
    )

    follows_downstream = and_(
        DocumentImportJob.status == "processing",
        DocumentImportJob.current_stage.in_(("embedding", "graph")),
        DocumentImportJob.embedding_job_id.is_not(None),
    )
    graph_requested = and_(
        DocumentImportJob.graph_extraction_requested.is_(True),
        DocumentImportJob.document_revision_id.is_not(None),
    )
    import_status = case(
        (DocumentImportJob.status.in_(("uploading", "queued")), "pending"),
        (DocumentImportJob.status == "succeeded", "done"),
        (DocumentImportJob.status == "failed", "failed"),
        (DocumentImportJob.status == "cancelled", "cancelled"),
        (DocumentImportJob.status == "superseded", "superseded"),
        (and_(follows_downstream, EmbeddingJob.id.is_(None)), "failed"),
        (and_(follows_downstream, EmbeddingJob.status == "failed"), "failed"),
        (and_(follows_downstream, EmbeddingJob.status == "pending"), "pending"),
        (and_(follows_downstream, EmbeddingJob.status == "processing"), "processing"),
        (and_(follows_downstream, EmbeddingJob.status == "superseded"), "superseded"),
        (and_(follows_downstream, EmbeddingJob.status == "done", ~graph_requested), "done"),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                graph_requested,
                latest_graph.c.id.is_(None),
            ),
            "processing",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status.in_(("queued", "processing")),
            ),
            "processing",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "waiting_schema",
            ),
            "pending",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status.in_(("partially_succeeded", "succeeded")),
            ),
            "done",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "failed",
            ),
            "failed",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "cancelled",
            ),
            "cancelled",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "superseded",
            ),
            "superseded",
        ),
        else_="processing",
    )
    import_embedding_retryable = and_(
        follows_downstream,
        EmbeddingJob.status == "failed",
        EmbeddingJob.attempt_count < settings.embed_worker_max_attempts,
    )
    import_graph_retryable = and_(
        follows_downstream,
        EmbeddingJob.status == "done",
        latest_graph.c.status == "failed",
        select(GraphExtractionUnit.id)
        .where(
            GraphExtractionUnit.job_id == latest_graph.c.id,
            GraphExtractionUnit.status == "failed",
            GraphExtractionUnit.retryable.is_(True),
            GraphExtractionUnit.model_attempt_count
            < settings.graph_extraction_worker_max_model_attempts,
        )
        .exists(),
    )
    import_retryable = or_(
        and_(
            DocumentImportJob.status == "failed",
            DocumentImportJob.attempt_count < settings.import_worker_max_attempts,
            or_(
                DocumentImportJob.last_error.is_(None),
                DocumentImportJob.last_error.not_like(
                    f"{import_uploads.UPLOAD_PREFLIGHT_ERROR_PREFIX}%"
                ),
            ),
        ),
        import_embedding_retryable,
        import_graph_retryable,
    )
    preflight_code = case(
        *(
            (
                or_(
                    *(
                        DocumentImportJob.last_error.like(
                            f"{import_uploads.UPLOAD_PREFLIGHT_ERROR_PREFIX}{cleanup_state}:{code}:%"
                        )
                        for cleanup_state in import_uploads.UPLOAD_PREFLIGHT_CLEANUP_STATES
                    )
                ),
                code,
            )
            for code in import_uploads.UPLOAD_PREFLIGHT_CODES
        ),
        else_=None,
    )
    import_projection = (
        select(
            import_status.label("status"),
            case((import_retryable, 1), else_=0).label("retryable"),
            case((import_embedding_retryable, 1), else_=0).label("retryable_embedding"),
            preflight_code.label("preflight_code"),
        )
        .select_from(DocumentImportJob)
        .outerjoin(EmbeddingJob, DocumentImportJob.embedding_job_id == EmbeddingJob.id)
        .outerjoin(
            latest_graph,
            DocumentImportJob.document_revision_id == latest_graph.c.document_revision_id,
        )
    )
    if library_id is not None:
        import_projection = import_projection.where(DocumentImportJob.library_id == library_id)

    graph_status = case(
        (GraphExtractionJob.status.in_(("waiting_schema", "queued")), "pending"),
        (GraphExtractionJob.status == "processing", "processing"),
        (GraphExtractionJob.status.in_(("partially_succeeded", "succeeded")), "done"),
        (GraphExtractionJob.status == "failed", "failed"),
        (GraphExtractionJob.status == "cancelled", "cancelled"),
        else_="superseded",
    )
    graph_retryable = and_(
        GraphExtractionJob.status == "failed",
        GraphExtractionJob.execution_mode == "production",
        GraphExtractionJob.id == latest_graph.c.id,
        select(GraphExtractionUnit.id)
        .where(
            GraphExtractionUnit.job_id == GraphExtractionJob.id,
            GraphExtractionUnit.status == "failed",
            GraphExtractionUnit.retryable.is_(True),
            GraphExtractionUnit.model_attempt_count
            < settings.graph_extraction_worker_max_model_attempts,
        )
        .exists(),
    )
    graph_projection = (
        select(
            graph_status.label("status"),
            case((graph_retryable, 1), else_=0).label("retryable"),
            literal(0).label("retryable_embedding"),
            literal(None).label("preflight_code"),
        )
        .select_from(GraphExtractionJob)
        .outerjoin(
            latest_graph,
            GraphExtractionJob.document_revision_id == latest_graph.c.document_revision_id,
        )
    )
    if library_id is not None:
        graph_projection = graph_projection.where(GraphExtractionJob.library_id == library_id)

    embedding_retryable = and_(
        EmbeddingJob.status == "failed",
        EmbeddingJob.attempt_count < settings.embed_worker_max_attempts,
    )
    embedding_projection = select(
        EmbeddingJob.status.label("status"),
        case((embedding_retryable, 1), else_=0).label("retryable"),
        case((embedding_retryable, 1), else_=0).label("retryable_embedding"),
        literal(None).label("preflight_code"),
    )
    if library_id is not None:
        embedding_projection = embedding_projection.where(EmbeddingJob.library_id == library_id)

    projection = union_all(
        import_projection,
        graph_projection,
        embedding_projection,
    ).subquery("monitor_stats_projection")
    return select(
        projection.c.status,
        func.count().label("count"),
        func.sum(projection.c.retryable).label("retryable"),
        func.sum(projection.c.retryable_embedding).label("retryable_embedding"),
        projection.c.preflight_code,
    ).group_by(projection.c.status, projection.c.preflight_code)


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
    last_error = import_uploads.project_upload_error(job.last_error)
    raw_error = last_error
    finished_at = job.finished_at
    stored_preflight = import_uploads.decode_upload_preflight_error(job.last_error)

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
    elif job.status == "failed" and (
        stored_preflight is not None or str(job.file_name).lower().endswith(".zip")
    ):
        retry_target_type = None
        retry_target_id = None
        retryable = False
        retry_capability = "unsupported"
        retry_reason = (
            "压缩包无法自动解压，请检查压缩包后重新上传"
            if str(job.file_name).lower().endswith(".zip")
            else "上传预检失败，请修正文件后重新上传"
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
    task_types: Optional[set[str]] = None,
    status_filter: Optional[str] = None,
) -> list[TaskMonitorRead]:
    selected_types = task_types or {"import", "embedding", "graph"}

    def scoped(model, task_type: str):
        stmt = select(model).order_by(model.created_at.desc(), model.id.desc())
        if library_id is not None:
            stmt = stmt.where(model.library_id == library_id)
        status_candidates = _monitor_status_candidates(task_type, status_filter)
        if status_candidates is not None:
            stmt = stmt.where(model.status.in_(status_candidates))
        if per_type_limit is not None:
            stmt = stmt.limit(per_type_limit)
        return stmt if task_type in selected_types else None

    async def load(model, task_type: str):
        stmt = scoped(model, task_type)
        if stmt is None:
            return []
        return list((await db.execute(stmt)).scalars().all())

    import_jobs = await load(DocumentImportJob, "import")
    embedding_jobs = await load(EmbeddingJob, "embedding")
    graph_jobs = await load(GraphExtractionJob, "graph")
    graph_ids = {job.id for job in graph_jobs}
    graph_revision_ids = {
        job.document_revision_id
        for job in graph_jobs
        if job.document_revision_id is not None
    }
    latest_production_graph_ids: set[uuid.UUID] = set()
    if graph_revision_ids:
        latest_ids = (
            await db.execute(
                select(GraphExtractionJob.id)
                .where(
                    GraphExtractionJob.document_revision_id.in_(graph_revision_ids),
                    GraphExtractionJob.execution_mode == "production",
                )
                .distinct(GraphExtractionJob.document_revision_id)
                .order_by(
                    GraphExtractionJob.document_revision_id,
                    GraphExtractionJob.created_at.desc(),
                    GraphExtractionJob.id.desc(),
                )
            )
        ).scalars().all()
        latest_production_graph_ids.update(latest_ids)
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
                    .options(
                        load_only(
                            GraphExtractionJob.id,
                            GraphExtractionJob.document_revision_id,
                            GraphExtractionJob.execution_mode,
                            GraphExtractionJob.status,
                            GraphExtractionJob.current_stage,
                            GraphExtractionJob.retry_generation,
                            GraphExtractionJob.error_message,
                            GraphExtractionJob.created_at,
                            GraphExtractionJob.finished_at,
                        )
                    )
                    .where(
                        GraphExtractionJob.document_revision_id.in_(import_revision_ids),
                        GraphExtractionJob.execution_mode == "production",
                    )
                    .distinct(GraphExtractionJob.document_revision_id)
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

    retryable_graph_ids: set[uuid.UUID] = set()
    retryable_graph_source_ids = graph_ids | {
        job.id for job in latest_graph_by_revision.values()
    }
    if retryable_graph_source_ids:
        retryable_units = (
            await db.execute(
                select(GraphExtractionUnit.job_id)
                .where(
                    GraphExtractionUnit.job_id.in_(retryable_graph_source_ids),
                    GraphExtractionUnit.status == "failed",
                    GraphExtractionUnit.retryable.is_(True),
                    GraphExtractionUnit.model_attempt_count
                    < settings.graph_extraction_worker_max_model_attempts,
                )
                .group_by(GraphExtractionUnit.job_id)
            )
        ).all()
        retryable_graph_ids = {row[0] for row in retryable_units}

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
    rows = await _load_monitor_tasks(
        db,
        library_id=library_id,
        per_type_limit=None if status_filter is not None else limit + offset,
        task_types={task_type} if task_type is not None else None,
        status_filter=status_filter,
    )
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
    counts = {
        key: 0
        for key in (
            "pending",
            "processing",
            "done",
            "failed",
            "cancelled",
            "superseded",
        )
    }
    aggregate_rows = (
        await db.execute(_monitor_stats_select(library_id=library_id))
    ).all()
    retryable_failed = 0
    retryable_embedding_failed = 0
    upload_preflight_failures = {
        code: 0 for code in import_uploads.UPLOAD_PREFLIGHT_CODES
    }
    total = 0
    for projected_status, count, retryable, retryable_embedding, preflight_code in aggregate_rows:
        count = int(count)
        counts[str(projected_status)] += count
        total += count
        retryable_failed += int(retryable or 0)
        retryable_embedding_failed += int(retryable_embedding or 0)
        if preflight_code is not None:
            upload_preflight_failures[str(preflight_code)] += count
    queues = await _load_queue_health(db, library_id=library_id)

    return TaskMonitorStats(
        **counts,
        retryable_failed=retryable_failed,
        total=total,
        retryable_embedding_failed=retryable_embedding_failed,
        upload_preflight_failures=upload_preflight_failures,
        queues=queues,
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
