"""Collection 三阶段重建 + finalize（#6 设计 §9）。

prepare（短事务，库 FOR UPDATE）→ qdrant（事务外，删/建 collection）→ activate（短事务，建 job）。
绝不在 DB 事务里调 Qdrant。finalize 即时 + 周期 reconcile 收口；锁序 library → rebuild_operation。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services.ingest import _supersede_active_jobs

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _prepare(db: AsyncSession, library_id):
    """阶段1：库 FOR UPDATE → 建 preparing operation → 设 rebuilding → 各活动文档 +revision、supersede 旧 job。"""
    lib = (await db.execute(
        select(Library).where(Library.id == library_id).with_for_update()
    )).scalar_one()
    op = RebuildOperation(
        library_id=lib.id, collection_name=lib.qdrant_collection,
        status="preparing", expected_job_count=0,
    )
    db.add(op)
    await db.flush()
    lib.index_state = "rebuilding"
    lib.active_rebuild_operation_id = op.id

    docs = (await db.execute(
        select(Document).where(
            Document.library_id == lib.id, Document.deleted_at.is_(None)
        ).with_for_update()
    )).scalars().all()
    for d in docs:
        d.current_revision = (d.current_revision or 0) + 1
        d.status = "pending"
        d.last_error = None
        await _supersede_active_jobs(db, d.id)

    snapshot = [(d.id, d.current_revision) for d in docs]
    await db.commit()
    return op.id, lib.qdrant_collection, lib.embedding_dim, lib.vector_distance, snapshot


async def _activate(db: AsyncSession, library_id, op_id, doc_revs) -> None:
    """阶段3：为每篇活动文档建一条带 rebuild_operation_id 的 job，写 expected_job_count，operation→running。

    锁序 library → rebuild_operation（§5.2）。
    """
    await db.execute(
        select(Library.id).where(Library.id == library_id).with_for_update()
    )
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id).with_for_update()
    )).scalar_one()
    if op.status == "running":
        await db.rollback()      # 已 activate（恢复时幂等）
        return
    for doc_id, rev in doc_revs:
        db.add(EmbeddingJob(
            library_id=op.library_id,
            document_id=doc_id, status="pending",
            document_revision=rev, rebuild_operation_id=op_id,
        ))
    op.expected_job_count = len(doc_revs)
    op.status = "running"
    await db.commit()


async def _mark_op_failed(db: AsyncSession, op_id, reason: str) -> None:
    # 锁序 library → rebuild_operation：先无锁读 library_id（标量，不进 identity map），再依次加锁
    lib_id = (await db.execute(
        select(RebuildOperation.library_id).where(RebuildOperation.id == op_id)
    )).scalar_one_or_none()
    if lib_id is None:
        return
    lib = (await db.execute(
        select(Library).where(Library.id == lib_id).with_for_update()
    )).scalar_one()
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    op.status = "failed"
    op.last_error = reason[:1000]
    op.finished_at = _now()
    lib.index_state = "failed"
    await db.commit()


async def _resume_state(db: AsyncSession, library_id, op):
    """恢复一个 preparing operation：从已 +revision 的活动文档重建 doc_revs（不再 +1）。"""
    lib = (await db.execute(select(Library).where(Library.id == library_id))).scalar_one()
    docs = (await db.execute(
        select(Document.id, Document.current_revision).where(
            Document.library_id == library_id, Document.deleted_at.is_(None)
        )
    )).all()
    return op.collection_name, lib.embedding_dim, lib.vector_distance, [(d, r) for d, r in docs]


async def run_rebuild(db: AsyncSession, library_id) -> str:
    """编排三阶段（支持恢复进行中的 operation）。qdrant 失败 → operation/库 failed 并抛出。"""
    from app.services import qdrant

    # 恢复入口：已有进行中 operation 则续跑，不新建（避免撞 uq_rebuild_op_active_per_lib）
    existing = (await db.execute(
        select(RebuildOperation).where(
            RebuildOperation.library_id == library_id,
            RebuildOperation.status.in_(("preparing", "running")),
        )
    )).scalars().first()
    if existing is not None and existing.status == "running":
        await try_finalize(db, existing.id)        # 已 activate → 只补收口
        return str(existing.id)
    if existing is not None and existing.status == "preparing":
        op_id = existing.id
        collection, dim, distance, doc_revs = await _resume_state(db, library_id, existing)
    else:
        op_id, collection, dim, distance, doc_revs = await _prepare(db, library_id)

    # 阶段2（事务外）：删/建 collection。delete 已容忍 404；真失败（5xx 等）→ 视为致命
    try:
        await qdrant.delete_collection(collection)
        await qdrant.ensure_collection(collection, dim=dim, distance=distance)
    except Exception as exc:  # noqa: BLE001
        await _mark_op_failed(db, op_id, f"qdrant rebuild failed: {exc}")
        raise

    await _activate(db, library_id, op_id, doc_revs)
    # 即时 finalize：覆盖空库（expected=0）直接完成的情况
    await try_finalize(db, op_id)
    return str(op_id)


async def try_finalize(db: AsyncSession, op_id) -> bool:
    """收口：锁序 library → rebuild_operation；done 数达 expected 则完成、有 failed 则置库 failed。

    幂等：operation 非 running 直接返回。返回 True 表示本次推进了终态。
    """
    # 仅取 library_id（标量，不进 identity map，避免后续锁定读返回缓存旧状态）
    lib_id = (await db.execute(
        select(RebuildOperation.library_id).where(RebuildOperation.id == op_id)
    )).scalar_one_or_none()
    if lib_id is None:
        return False
    # 锁序 library → operation；populate_existing 保证读到锁内最新状态（并发 finalize 只成一次）
    lib = (await db.execute(
        select(Library).where(Library.id == lib_id).with_for_update()
    )).scalar_one_or_none()
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if lib is None or op is None or op.status != "running":
        await db.rollback()
        return False

    failed = (await db.execute(
        select(func.count()).select_from(EmbeddingJob).where(
            EmbeddingJob.rebuild_operation_id == op.id, EmbeddingJob.status == "failed"
        )
    )).scalar_one()
    if failed > 0:
        op.status = "failed"
        op.finished_at = _now()
        lib.index_state = "failed"
        await db.commit()
        log.warning("rebuild op=%s FAILED (%s failed jobs)", op_id, failed)
        return True

    done = (await db.execute(
        select(func.count()).select_from(EmbeddingJob).where(
            EmbeddingJob.rebuild_operation_id == op.id, EmbeddingJob.status == "done"
        )
    )).scalar_one()
    if done >= op.expected_job_count:
        op.status = "done"
        op.finished_at = _now()
        lib.index_state = "ready"
        lib.active_rebuild_operation_id = None
        await db.commit()
        log.info("rebuild op=%s DONE (%s/%s)", op_id, done, op.expected_job_count)
        return True

    await db.rollback()
    return False


async def reconcile_running(db: AsyncSession) -> int:
    """周期兜底：无锁扫描 running operation 的 id，逐个按锁序 try_finalize。返回推进数。"""
    ids = (await db.execute(
        select(RebuildOperation.id).where(RebuildOperation.status == "running")
    )).scalars().all()
    n = 0
    for op_id in ids:
        if await try_finalize(db, op_id):
            n += 1
    return n
