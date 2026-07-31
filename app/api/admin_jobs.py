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
from app.db import get_db
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.user import User
from app.schemas.admin import (
    EmbeddingJobRead,
    EmbeddingJobStats,
    TaskMonitorRead,
    TaskMonitorStats,
)
from app.services import audit_log

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
    "queued": "pending",
    "processing": "processing",
    "partially_succeeded": "done",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "superseded": "superseded",
}


def _embedding_monitor_row(
    job: EmbeddingJob,
    *,
    title: Optional[str] = None,
) -> TaskMonitorRead:
    normalized = _EMBEDDING_STATUS.get(job.status, job.status)
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
        retryable=job.status in {"failed", "pending"},
    )


def _graph_monitor_row(
    job: GraphExtractionJob,
    *,
    title: Optional[str] = None,
    revision_no: Optional[int] = None,
    publication_status: Optional[str] = None,
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
    return TaskMonitorRead(
        id=job.id,
        task_type="graph",
        title=title,
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision=revision_no,
        document_revision_id=job.document_revision_id,
        status=_GRAPH_STATUS.get(job.status, job.status),
        raw_status=job.status,
        stage=job.current_stage or ("preparing" if job.status == "queued" else None),
        attempt_count=job.retry_generation,
        last_error=job.error_message,
        created_at=job.created_at,
        claimed_at=job.started_at,
        finished_at=job.finished_at,
        retryable=False,
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
            "in_flight": provider_gate.get("in_flight"),
            "throttled_count": int(provider_gate.get("throttled_count") or 0),
            "retry_count": int(provider_gate.get("retry_count") or 0),
        },
    )


def _import_monitor_row(
    job: DocumentImportJob,
    *,
    embedding_job: Optional[EmbeddingJob] = None,
    graph_job: Optional[GraphExtractionJob] = None,
    revision_no: Optional[int] = None,
) -> TaskMonitorRead:
    status_value = _IMPORT_STATUS.get(job.status, job.status)
    raw_status = job.status
    stage = job.current_stage
    worker_id = job.worker_id
    last_error = job.last_error
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
            last_error = embedding_job.last_error or "embedding failed"
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
                last_error = graph_job.error_message or "graph extraction failed"

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
        retryable=False,
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
        stmt = select(model).order_by(model.created_at.desc())
        if library_id is not None:
            stmt = stmt.where(model.library_id == library_id)
        if per_type_limit is not None:
            stmt = stmt.limit(per_type_limit)
        return stmt

    import_jobs = list((await db.execute(scoped(DocumentImportJob))).scalars().all())
    embedding_jobs = list((await db.execute(scoped(EmbeddingJob))).scalars().all())
    graph_jobs = list((await db.execute(scoped(GraphExtractionJob))).scalars().all())
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
                    .where(GraphExtractionJob.document_revision_id.in_(import_revision_ids))
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
    )


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
        .where(EmbeddingJob.status == "failed")
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
