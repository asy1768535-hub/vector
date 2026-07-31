"""Collection 三阶段重建 + finalize（#6 设计 §9）。

prepare（短事务，库 FOR UPDATE）→ qdrant（事务外，删/建 collection）→ activate（短事务，建 job）。
绝不在 DB 事务里调 Qdrant。finalize 即时 + 周期 reconcile 收口；锁序 library → rebuild_operation。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services.ingest import _supersede_active_jobs

log = logging.getLogger(__name__)


class ExternalLibraryError(RuntimeError):
    """external 库由外部系统管理，Service 层禁止对其重建（绝不删/建其 Qdrant collection）。"""


class ConcurrentRebuildError(RuntimeError):
    """库已有进行中的 rebuild operation（锁内复核发现）→ 明确业务冲突，而非靠唯一索引兜底。"""


async def _active_operation(db: AsyncSession, library_id):
    """锁内复核：该库是否已有进行中（preparing/running）的 operation（fresh 读，不走缓存）。"""
    return (await db.execute(
        select(RebuildOperation).where(
            RebuildOperation.library_id == library_id,
            RebuildOperation.status.in_(("preparing", "running")),
        )
    )).scalars().first()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _prepare(db: AsyncSession, library_id):
    """阶段1：库 FOR UPDATE → 建 preparing operation → 设 rebuilding → 各活动文档 +revision、supersede 旧 job。"""
    # populate_existing=True：强制读锁定后的新鲜行，避免本 session 之前缓存的 lifecycle/状态被复用
    lib = (await db.execute(
        select(Library).where(Library.id == library_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    # Service 层 external 防护（锁内权威判断，fresh 读）：external 库不得重建
    if lib.lifecycle_mode == "external":
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    # 并发首次 rebuild：锁内复核已有进行中 operation → 明确冲突，不靠 uq_rebuild_op_active_per_lib 兜底
    if await _active_operation(db, library_id) is not None:
        await db.rollback()
        raise ConcurrentRebuildError(f"library {library_id} already has an active rebuild operation")
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
    """恢复一个未 activate 的 operation：从已 +revision 的活动文档重建 doc_revs（不再 +1）。"""
    lib = (await db.execute(select(Library).where(Library.id == library_id))).scalar_one()
    docs = (await db.execute(
        select(Document.id, Document.current_revision).where(
            Document.library_id == library_id, Document.deleted_at.is_(None)
        )
    )).all()
    return op.collection_name, lib.embedding_dim, lib.vector_distance, [(d, r) for d, r in docs]


async def _set_rebuilding(db: AsyncSession, library_id, op_id) -> bool:
    """恢复未 activate 阶段前：把库置回 rebuilding、operation 置回 preparing（锁序 library→op）。

    锁内复核 op 状态：仅当仍为 preparing/failed 才推进（并发恢复只一个生效）；否则 no-op 返回 False。
    """
    lib = (await db.execute(
        select(Library).where(Library.id == library_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    if lib.lifecycle_mode == "external":          # fresh 读后复核 lifecycle
        await db.rollback()
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    if op.status not in ("preparing", "failed"):
        await db.rollback()
        return False
    lib.index_state = "rebuilding"
    lib.active_rebuild_operation_id = op_id
    op.status = "preparing"
    op.last_error = None
    op.finished_at = None
    await db.commit()
    return True


async def _retry_jobs(db: AsyncSession, library_id, op_id) -> bool:
    """finalize 阶段失败重试：把本 operation 的失败/未完 job 重置为 pending（保留 done），
    operation→running、库→rebuilding。复用 revision，不重跑 Qdrant、不动 done job。

    **锁内复核 op.status == 'failed'**：并发重试只有一个生效，避免把另一重试已转 running、
    worker 正在 processing 的 job 误重置回 pending。非 failed → no-op 返回 False。
    """
    lib = (await db.execute(
        select(Library).where(Library.id == library_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    if lib.lifecycle_mode == "external":          # fresh 读后复核 lifecycle
        await db.rollback()
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    if op.status != "failed":
        await db.rollback()
        return False
    await db.execute(
        sa_update(EmbeddingJob)
        .where(EmbeddingJob.rebuild_operation_id == op_id,
               EmbeddingJob.status.in_(("failed", "processing", "superseded")))
        .values(status="pending", worker_id=None, claimed_at=None, finished_at=None,
                attempt_count=0, last_error=None)
    )
    op.status = "running"
    op.last_error = None
    op.finished_at = None
    lib.index_state = "rebuilding"
    lib.active_rebuild_operation_id = op_id
    await db.commit()
    return True


async def run_rebuild(db: AsyncSession, library_id) -> str:
    """编排三阶段，支持**恢复/重试**当前 operation（preparing 续跑、running 收口、failed 重试），
    全程复用 revision、不新建 operation；只有无活动 operation 时才新建（+revision）。
    qdrant 失败 → operation/库 failed 并抛出。"""
    from app.services import qdrant

    # 恢复锚点：sys_libraries.active_rebuild_operation_id（失败时不清空，见 §9）
    lib0 = (await db.execute(select(Library).where(Library.id == library_id))).scalar_one()
    # Service 层 external 防护（前置，确保对 external 库绝不进入任何 Qdrant 删/建）
    if lib0.lifecycle_mode == "external":
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    active = None
    if lib0.active_rebuild_operation_id is not None:
        active = (await db.execute(
            select(RebuildOperation).where(RebuildOperation.id == lib0.active_rebuild_operation_id)
        )).scalar_one_or_none()

    if active is not None and active.status == "running":
        await db.rollback()
        raise ConcurrentRebuildError(
            f"library {library_id} already has an active rebuild operation"
        )

    if active is not None and active.status in ("preparing", "failed"):
        op_id = active.id
        njobs = (await db.execute(
            select(func.count()).select_from(EmbeddingJob).where(
                EmbeddingJob.rebuild_operation_id == op_id)
        )).scalar_one()
        if njobs > 0:
            # 已 activate（finalize 阶段失败）→ 只重置失败/未完 job，复用 revision，保留 done
            await _retry_jobs(db, library_id, op_id)   # 并发下只一个生效（锁内复核 status）
            await try_finalize(db, op_id)
            return str(op_id)
        # 未 activate（preparing 或 qdrant 阶段失败）→ 复跑 qdrant + activate（复用已 +1 的 revision）
        collection, dim, distance, doc_revs = await _resume_state(db, library_id, active)
        if not await _set_rebuilding(db, library_id, op_id):
            # 并发恢复已被他者推进 → 不重跑 Qdrant，直接尝试收口
            await try_finalize(db, op_id)
            return str(op_id)
    else:
        # 无活动 operation → 全新（+revision）
        op_id, collection, dim, distance, doc_revs = await _prepare(db, library_id)

    # 阶段2（事务外）：删/建 collection。delete 容忍 404；真失败（5xx 等）→ 致命
    try:
        await qdrant.delete_collection(collection)
        await qdrant.ensure_collection(collection, dim=dim, distance=distance)
    except Exception as exc:  # noqa: BLE001
        await _mark_op_failed(db, op_id, f"qdrant rebuild failed: {exc}")
        raise

    await _activate(db, library_id, op_id, doc_revs)
    await try_finalize(db, op_id)   # 覆盖空库 expected=0 直接完成
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
    superseded = (await db.execute(
        select(func.count()).select_from(EmbeddingJob).where(
            EmbeddingJob.rebuild_operation_id == op.id,
            EmbeddingJob.status == "superseded",
        )
    )).scalar_one()
    if failed > 0 or superseded > 0:
        op.status = "failed"
        op.last_error = (
            f"rebuild jobs did not complete: failed={failed}, superseded={superseded}"
        )
        op.finished_at = _now()
        lib.index_state = "failed"
        await db.commit()
        log.warning(
            "rebuild op=%s FAILED (failed=%s, superseded=%s)",
            op_id,
            failed,
            superseded,
        )
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
