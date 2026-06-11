"""admin embedding_jobs 监控：列表 / 重试。"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.db import get_db
from app.models.embedding_job import EmbeddingJob
from app.models.user import User
from app.schemas.admin import EmbeddingJobRead
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


@router.post("/{job_id}/retry", response_model=EmbeddingJobRead)
async def retry_job(
    job_id: uuid.UUID,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingJob:
    job = await db.get(EmbeddingJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status not in ("failed", "processing"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cannot retry job in status={job.status}")
    await db.execute(
        update(EmbeddingJob)
        .where(EmbeddingJob.id == job_id)
        .values(status="pending", worker_id=None, last_error=None, finished_at=None, claimed_at=None)
    )
    await audit_log.record(db, actor.id, "job.retry", {"job_id": str(job_id)})
    await db.commit()
    await db.refresh(job)
    return job
