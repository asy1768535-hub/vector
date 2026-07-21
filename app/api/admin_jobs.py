"""admin embedding_jobs 监控：列表 / 重试。"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.db import get_db
from app.models.embedding_job import EmbeddingJob
from app.models.user import User
from app.schemas.admin import EmbeddingJobRead, EmbeddingJobStats
from app.services import audit_log

router = APIRouter(prefix="/admin/jobs", tags=["admin"])


@router.get("", response_model=list[EmbeddingJobRead])
async def list_jobs(
    status_filter: Optional[str] = Query(default=None, alias="status",
                                         pattern="^(pending|processing|done|failed)$"),
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
    rows = await db.execute(
        select(EmbeddingJob.status, func.count()).group_by(EmbeddingJob.status)
    )
    counts = {row_status: cnt for row_status, cnt in rows.all()}
    return EmbeddingJobStats(
        pending=counts.get("pending", 0),
        processing=counts.get("processing", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        total=sum(counts.values()),
    )


@router.post("/reset-failed")
async def reset_failed_jobs(
    library_id: Optional[uuid.UUID] = Query(default=None,
                                            description="只重置该库的 failed 任务；不传=全局重置"),
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
        .values(status="pending", attempt_count=0, last_error=None,
                worker_id=None, finished_at=None, claimed_at=None)
    )
    if library_id is not None:
        stmt = stmt.where(EmbeddingJob.library_id == library_id)
    result = await db.execute(stmt)
    count = result.rowcount or 0
    await audit_log.record(
        db, actor.id, "job.reset_failed",
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
        .values(status="pending", attempt_count=0, worker_id=None, last_error=None,
                finished_at=None, claimed_at=None)
    )
    await audit_log.record(db, actor.id, "job.retry", {"job_id": str(job_id)})
    await db.commit()
    await db.refresh(job)
    return job
