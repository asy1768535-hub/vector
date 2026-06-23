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
import time
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_factory
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import embedding, qdrant

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def eligibility(job, document, library, operation) -> bool:
    """统一资格条件（#6 设计 §5.1）：claim / embedding 前 / 最终写入前三处复用同一条。

    operation 是该 job 的 rebuild_operations 行（普通 job 或非重建时为 None）。
    不满足 → 调用方应把 job 标 superseded（不调 embedding、不计失败）。
    """
    if document is None or library is None:
        return False
    if getattr(library, "deleted_at", None) is not None:   # #7：库已删 → 任何 job 不执行
        return False
    if document.deleted_at is not None:
        return False
    if job.document_revision != document.current_revision:
        return False
    if library.index_state == "ready":
        # ready 时只放行普通 job；带 rebuild_operation_id 的（含失败 operation 遗留）一律拒绝
        return job.rebuild_operation_id is None
    if library.index_state == "rebuilding":
        return (
            job.rebuild_operation_id is not None
            and job.rebuild_operation_id == library.active_rebuild_operation_id
            and operation is not None
            and operation.status == "running"
        )
    # failed 或未知状态：全拒
    return False


def _build_payload(lib: Library, doc: Document, chunk: Chunk) -> dict:
    """构造单个 Qdrant point 的 payload。

    关键：用户提供的 doc_metadata 先展开，系统保留字段**最后**写入，
    保证 document_id/chunk_id/text/title/library_id 等系统字段恒胜，
    用户无法通过 metadata 覆盖它们（否则会破坏删除/检索完整性、伪造文档归属）。
    """
    payload = dict(doc.doc_metadata or {})
    payload.update(
        {
            "library_id": str(lib.id),
            "document_id": str(doc.id),
            "chunk_id": str(chunk.id),
            "seq": chunk.seq,
            "text": chunk.text,
            "title": doc.title,
            "external_id": doc.external_id,
            "document_revision": doc.current_revision,  # #6：检索按它过滤陈旧 point
        }
    )
    return payload


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


async def _mark_superseded(db: AsyncSession, job: EmbeddingJob) -> None:
    """资格条件不满足（旧 revision / 库重建中 / 已删等）→ 正常终止，不计失败、不重试。"""
    await db.execute(
        update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
            status="superseded", finished_at=datetime.now(timezone.utc)
        )
    )
    await db.commit()


async def _process_job(db: AsyncSession, job: EmbeddingJob) -> None:
    """处理单个 job：资格检查 → 拉 chunks → embed → 按锁序复核 → upsert → 标 done（#6 §5）。"""
    lib = await db.get(Library, job.library_id)
    doc = await db.get(Document, job.document_id)
    if lib is None or doc is None:
        await _mark_failed(db, job, "library or document missing")
        return
    op = await db.get(RebuildOperation, job.rebuild_operation_id) if job.rebuild_operation_id else None

    # 资格检查 #1（embedding 前，§5.3）：不满足直接 superseded，省下 embedding 成本
    if not eligibility(job, doc, lib, op):
        await _mark_superseded(db, job)
        log.info("superseded (pre-embed): job=%s doc=%s rev=%s", job.id, doc.id, job.document_revision)
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
        # 分批调 embedding（不持行锁；READ COMMITTED 下最终再按锁序复核最新状态）
        batch = lib.embed_batch_size or settings.embed_batch_size
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

        # 资格检查 #2（写 Qdrant 前，§5.4）：按锁序 library FOR KEY SHARE → document FOR UPDATE 复核
        lib_l = (await db.execute(
            select(Library).where(Library.id == job.library_id).with_for_update(read=True, key_share=True)
        )).scalar_one_or_none()
        doc_l = (await db.execute(
            select(Document).where(Document.id == job.document_id).with_for_update()
        )).scalar_one_or_none()
        op_l = (await db.execute(
            select(RebuildOperation).where(RebuildOperation.id == job.rebuild_operation_id)
        )).scalar_one_or_none() if job.rebuild_operation_id else None
        if lib_l is None or doc_l is None or not eligibility(job, doc_l, lib_l, op_l):
            await db.rollback()  # 释放行锁
            await _mark_superseded(db, job)
            log.info("superseded (pre-write): job=%s doc=%s rev=%s", job.id, job.document_id, job.document_revision)
            return

        # 准备 Qdrant points（payload 带 document_revision，由锁定的 doc 提供）
        points = [
            {"id": str(chunk.id), "vector": vec, "payload": _build_payload(lib_l, doc_l, chunk)}
            for chunk, vec in zip(chunks, vectors)
        ]
        await qdrant.upsert_points(
            lib_l.qdrant_collection, points, timeout=settings.qdrant_upsert_timeout_seconds
        )

        now = datetime.now(timezone.utc)
        await db.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
                status="done", finished_at=now, last_error=None
            )
        )
        # 状态写守护：仍是当前 revision 才置 ready（eligibility 已在锁内确认）
        await db.execute(
            update(Document).where(Document.id == doc_l.id).values(
                status="ready", last_error=None, updated_at=now
            )
        )
        await db.commit()  # 释放锁
        log.info("done: lib=%s doc=%s rev=%s chunks=%s", lib_l.slug, doc_l.id, job.document_revision, len(chunks))
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed: %s", job.id, exc)
        await _mark_failed(db, job, str(exc)[:1000])

    # 即时 finalize：rebuild job 转终态（done 或 failed）后立刻尝试收口（新 session，锁序 library→operation）。
    # 失败/崩溃由主循环周期 reconcile 兜底。
    if job.rebuild_operation_id is not None:
        try:
            from app.services import rebuild as rebuild_svc
            async with async_session_factory() as fsession:
                await rebuild_svc.try_finalize(fsession, job.rebuild_operation_id)
        except Exception:  # noqa: BLE001
            log.exception("immediate finalize failed for op=%s (reconcile 兜底)", job.rebuild_operation_id)


async def _mark_failed(db: AsyncSession, job: EmbeddingJob, reason: str) -> None:
    now = datetime.now(timezone.utc)
    # revision 守卫（#6）：embedding 期间文档可能被另一事务更新/删除。**必须读新鲜行**——
    # 先 rollback 清掉本 session 的 identity-map 缓存与可能的未决事务，再按锁序
    # library FOR KEY SHARE → document FOR UPDATE 重新读取，避免 db.get 返回缓存旧 revision。
    await db.rollback()
    await db.execute(
        select(Library.id).where(Library.id == job.library_id).with_for_update(read=True, key_share=True)
    )
    doc = (await db.execute(
        select(Document).where(Document.id == job.document_id).with_for_update()
    )).scalar_one_or_none()
    if doc is None or doc.deleted_at is not None or job.document_revision != doc.current_revision:
        await db.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
                status="superseded", finished_at=now
            )
        )
        await db.commit()
        return

    # 仍是当前 revision：超 max_attempts → final failed；否则回 pending 重试
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
        # 只在仍是当前 revision 时标 Document failed（守卫已确认）
        await db.execute(
            update(Document).where(Document.id == job.document_id).values(
                status="failed", last_error=reason, updated_at=now
            )
        )
    await db.commit()


async def run(watch: bool) -> None:
    worker_id = _worker_id()
    log.info("starting worker id=%s watch=%s batch_docs=%s", worker_id, watch, settings.embed_worker_batch_docs)
    # 启动自检：embedding / Qdrant 用不了时立刻报（非 fatal）。
    from app.services import selfcheck
    degraded = not await selfcheck.run_startup_check("worker")
    if degraded:
        log.error("[worker] 自检失败 → 进入 degraded：暂停消费，避免把 pending 任务刷成 failed。")
    last_reconcile = 0.0
    while True:
        # 周期 reconcile：即使持续有任务也按间隔收口 running operation（不只在空闲时）。
        if not degraded and (time.monotonic() - last_reconcile) >= settings.worker_reconcile_seconds:
            last_reconcile = time.monotonic()
            try:
                from app.services import rebuild as rebuild_svc
                async with async_session_factory() as rsession:
                    advanced = await rebuild_svc.reconcile_running(rsession)
                    if advanced:
                        log.info("periodic reconcile finalized %s rebuild operation(s)", advanced)
            except Exception:  # noqa: BLE001
                log.exception("periodic rebuild reconcile failed")
        # degraded：不 claim 任务，定时重测自检（embedding + Qdrant 都要好），恢复后再消费
        if degraded:
            if not watch:
                # 单次模式下依赖不可用，直接退出（cron 会下次再来）
                log.error("[worker] 依赖不可用且非 watch 模式，退出。")
                return
            ok, msg = await selfcheck.check_consumable()
            if ok:
                log.warning("[worker] 依赖已恢复（embedding + Qdrant），退出 degraded，恢复消费。")
                degraded = False
            else:
                log.error("[worker] degraded：暂停消费，%ss 后重测（%s）",
                          settings.worker_degraded_retry_seconds, msg)
                await asyncio.sleep(settings.worker_degraded_retry_seconds)
                continue

        async with async_session_factory() as session:
            reset = await _reset_stale_jobs(session)
            if reset:
                log.warning("reset %s stale processing jobs", reset)
            jobs = await _claim_jobs(session, worker_id, settings.embed_worker_batch_docs)

        if not jobs:
            if not watch:
                # 单次模式退出前补一次 reconcile，避免遗留 running operation 卡住
                try:
                    from app.services import rebuild as rebuild_svc
                    async with async_session_factory() as rsession:
                        await rebuild_svc.reconcile_running(rsession)
                except Exception:  # noqa: BLE001
                    log.exception("final reconcile failed")
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
