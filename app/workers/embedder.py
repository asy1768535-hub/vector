"""Embedding worker：从 embedding_jobs 抢锁 → 调 bge-m3 → upsert Qdrant → 标 done。

迁移自 cpwsImportData/importdata/embedding_worker.py:88-117 的 FOR UPDATE SKIP LOCKED 模式。
精简：去掉案件域逻辑（split_blocks、case_metadata），通用化为 (library, document, chunks) 三元组。

用法：
  python -m app.workers.embedder            # 处理完 pending 即退出（cron 友好）
  python -m app.workers.embedder --watch    # 长跑：空闲时 sleep poll_seconds 后再抢
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_factory
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.services import embedding, qdrant

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def _reset_stale_jobs(db: AsyncSession) -> int:
    """超时仍在 processing 的任务重置为 pending；返回被重置条数。"""
    cutoff_sql = text(
        """
        UPDATE embedding_jobs
        SET status = 'pending', worker_id = NULL, claimed_at = NULL
        WHERE status = 'processing'
          AND claimed_at IS NOT NULL
          AND claimed_at < NOW() - (:secs || ' seconds')::interval
        """
    )
    result = await db.execute(cutoff_sql, {"secs": str(settings.embed_worker_stale_seconds)})
    await db.commit()
    return result.rowcount or 0


async def _claim_jobs(db: AsyncSession, worker_id: str, limit: int) -> list[EmbeddingJob]:
    """FOR UPDATE SKIP LOCKED 抢锁 → 标记 processing。"""
    raw_sql = text(
        """
        WITH picked AS (
            SELECT id FROM embedding_jobs
            WHERE status = 'pending'
              AND attempt_count < :max_attempts
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE embedding_jobs j
        SET status = 'processing',
            worker_id = :worker_id,
            attempt_count = j.attempt_count + 1,
            claimed_at = NOW()
        FROM picked
        WHERE j.id = picked.id
        RETURNING j.id
        """
    )
    result = await db.execute(
        raw_sql,
        {"limit": limit, "worker_id": worker_id, "max_attempts": settings.embed_worker_max_attempts},
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        await db.commit()
        return []
    rows = await db.execute(select(EmbeddingJob).where(EmbeddingJob.id.in_(ids)))
    jobs = list(rows.scalars().all())
    await db.commit()
    return jobs


async def _process_job(db: AsyncSession, job: EmbeddingJob) -> None:
    """处理单个 job：拉 chunks → embed → upsert → 标 done。"""
    lib = await db.get(Library, job.library_id)
    doc = await db.get(Document, job.document_id)
    if lib is None or doc is None:
        await _mark_failed(db, job, "library or document missing")
        return
    if lib.deleted_at is not None or doc.deleted_at is not None:
        await _mark_failed(db, job, "library or document soft-deleted")
        return

    rows = await db.execute(
        select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.seq)
    )
    chunks = list(rows.scalars().all())
    if not chunks:
        await _mark_failed(db, job, "no chunks to embed")
        return

    texts = [c.text for c in chunks]
    try:
        # 分批调 embedding，避免单次请求过大
        batch = settings.embed_batch_size
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch):
            piece = await embedding.embed_texts(
                texts[i : i + batch],
                model=lib.embedding_model,
                base_url=lib.embedding_base_url,
            )
            vectors.extend(piece)
        if len(vectors) != len(chunks):
            raise RuntimeError(f"vector count mismatch: {len(vectors)} vs {len(chunks)}")
        if any(len(v) != lib.embedding_dim for v in vectors):
            raise RuntimeError(f"dim mismatch: expected {lib.embedding_dim}")

        # 准备 Qdrant points
        points = []
        for chunk, vec in zip(chunks, vectors):
            points.append(
                {
                    "id": str(chunk.id),
                    "vector": vec,
                    "payload": {
                        "library_id": str(lib.id),
                        "document_id": str(doc.id),
                        "chunk_id": str(chunk.id),
                        "seq": chunk.seq,
                        "text": chunk.text,
                        "title": doc.title,
                        "external_id": doc.external_id,
                        **(doc.doc_metadata or {}),
                    },
                }
            )
        await qdrant.upsert_points(lib.qdrant_collection, points)

        # 标 done
        now = datetime.now(timezone.utc)
        await db.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
                status="done", finished_at=now, last_error=None
            )
        )
        await db.execute(
            update(Document).where(Document.id == doc.id).values(
                status="ready", last_error=None, updated_at=now
            )
        )
        await db.commit()
        log.info("done: lib=%s doc=%s chunks=%s", lib.slug, doc.id, len(chunks))
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed: %s", job.id, exc)
        await _mark_failed(db, job, str(exc)[:1000])


async def _mark_failed(db: AsyncSession, job: EmbeddingJob, reason: str) -> None:
    now = datetime.now(timezone.utc)
    # 若已超 max_attempts → final failed；否则可回 pending 重试
    next_status = (
        "failed"
        if (job.attempt_count or 0) >= settings.embed_worker_max_attempts
        else "pending"
    )
    await db.execute(
        update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
            status=next_status,
            last_error=reason,
            finished_at=now if next_status == "failed" else None,
            worker_id=None if next_status == "pending" else job.worker_id,
            claimed_at=None if next_status == "pending" else job.claimed_at,
        )
    )
    if next_status == "failed":
        await db.execute(
            update(Document).where(Document.id == job.document_id).values(
                status="failed", last_error=reason, updated_at=now
            )
        )
    await db.commit()


async def run(watch: bool) -> None:
    worker_id = _worker_id()
    log.info("starting worker id=%s watch=%s batch_docs=%s", worker_id, watch, settings.embed_worker_batch_docs)
    while True:
        async with async_session_factory() as session:
            reset = await _reset_stale_jobs(session)
            if reset:
                log.warning("reset %s stale processing jobs", reset)
            jobs = await _claim_jobs(session, worker_id, settings.embed_worker_batch_docs)

        if not jobs:
            if not watch:
                log.info("no pending jobs; exiting")
                return
            await asyncio.sleep(settings.embed_worker_poll_seconds)
            continue

        log.info("claimed %s jobs", len(jobs))
        # 每个 job 独立 session，错误不互相影响
        for job in jobs:
            async with async_session_factory() as session:
                await _process_job(session, job)


def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding worker (DB-queue based).")
    parser.add_argument("--watch", action="store_true", help="Long-running mode; poll when idle.")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
