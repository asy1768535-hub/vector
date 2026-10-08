"""Collection 三阶段重建 + finalize（#6 设计 §9）。

prepare（短事务，锁库并保存 jobs 快照）→ qdrant（事务外，删/建 collection）→ activate（短事务，开放 jobs）。
绝不在 DB 事务里调 Qdrant。finalize 即时 + 周期 reconcile 收口；锁序 library → rebuild_operation。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services.ingest import _supersede_active_jobs

log = logging.getLogger(__name__)


class ExternalLibraryError(RuntimeError):
    """external 库由外部系统管理，Service 层禁止对其重建（绝不删/建其 Qdrant collection）。"""


class ConcurrentRebuildError(RuntimeError):
    """库已有进行中的 rebuild operation（锁内复核发现）→ 明确业务冲突，而非靠唯一索引兜底。"""


class RebuildTargetError(RuntimeError):
    """Published rebuild targets cannot be established safely."""


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
    """Lock the library, validate published targets and persist pending rebuild jobs."""
    # populate_existing=True：强制读锁定后的新鲜行，避免本 session 之前缓存的 lifecycle/状态被复用
    lib = (await db.execute(
        select(Library).where(Library.id == library_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    # Service 层 external 防护（锁内权威判断，fresh 读）：external 库不得重建
    if lib.lifecycle_mode == "external":
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    if lib.deleted_at is not None:
        raise RebuildTargetError("deleted library cannot be rebuilt")
    # 并发首次 rebuild：锁内复核已有进行中 operation → 明确冲突，不靠 uq_rebuild_op_active_per_lib 兜底
    if await _active_operation(db, library_id) is not None:
        await db.rollback()
        raise ConcurrentRebuildError(f"library {library_id} already has an active rebuild operation")
    docs = (await db.execute(
        select(Document).where(
            Document.library_id == lib.id, Document.deleted_at.is_(None)
        ).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    completed_ids = set((await db.execute(
        select(EmbeddingJob.document_id).where(
            EmbeddingJob.library_id == lib.id, EmbeddingJob.status == "done",
        ).distinct()
    )).scalars().all())
    current_ids = {d.current_revision_id for d in docs if d.current_revision_id is not None}
    revisions = {
        revision.id: revision for revision in (await db.execute(
            select(DocumentRevision).where(DocumentRevision.id.in_(current_ids))
            .with_for_update().execution_options(populate_existing=True)
        )).scalars().all()
    } if current_ids else {}
    targets = []
    for d in docs:
        if d.current_revision_id is not None:
            revision = revisions.get(d.current_revision_id)
            if (revision is None or revision.status != "ready"
                    or revision.document_id != d.id or revision.library_id != lib.id
                    or not revision.revision_no or revision.revision_no < 1):
                await db.rollback()
                raise RebuildTargetError("current published revision is missing or invalid")
            targets.append((d, revision.id, revision.revision_no))
        elif d.status == "ready" or d.id in completed_ids:
            # The legacy integer worker can publish without filling revision IDs.
            # Never confuse merely parsed chunks/pending latest with publication.
            if settings.enable_revision_id_worker or d.latest_revision_id is not None:
                await db.rollback()
                raise RebuildTargetError("published legacy document has no safe current revision ID")
            chunk_count = (await db.execute(
                select(func.count()).select_from(Chunk).where(
                    Chunk.document_id == d.id, Chunk.library_id == lib.id,
                )
            )).scalar_one()
            if not chunk_count:
                await db.rollback()
                raise RebuildTargetError("published legacy document has no chunks")
            revision_chunks = (await db.execute(
                select(func.count()).select_from(Chunk).where(
                    Chunk.document_id == d.id, Chunk.library_id == lib.id,
                    Chunk.document_revision_id.is_not(None),
                )
            )).scalar_one()
            if revision_chunks:
                await db.rollback()
                raise RebuildTargetError("legacy document contains unbound revision chunks")
            targets.append((d, None, None))

    op = RebuildOperation(
        library_id=lib.id, collection_name=lib.qdrant_collection,
        status="preparing", expected_job_count=0,
    )
    db.add(op)
    await db.flush()
    lib.index_state = "rebuilding"
    lib.active_rebuild_operation_id = op.id
    snapshot = []
    for d, revision_id, revision_no in targets:
        d.current_revision = (d.current_revision or 0) + 1
        d.status = "pending"
        d.last_error = None
        await _supersede_active_jobs(db, d.id)
        db.add(EmbeddingJob(
            library_id=lib.id, document_id=d.id, status="pending",
            document_revision=d.current_revision, document_revision_id=revision_id,
            document_revision_no=revision_no, rebuild_operation_id=op.id,
        ))
        snapshot.append((d.id, d.current_revision, revision_id, revision_no))
    await db.commit()
    return op.id, lib.qdrant_collection, lib.embedding_dim, lib.vector_distance, snapshot


async def _activate(db: AsyncSession, library_id, op_id, doc_revs) -> None:
    """阶段3：开放已持久化的 jobs 快照，写 expected_job_count，operation→running。

    锁序 library → rebuild_operation（§5.2）。
    """
    lib = (await db.execute(
        select(Library).where(Library.id == library_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    if (op.library_id != library_id
            or lib.active_rebuild_operation_id != op_id
            or lib.qdrant_collection != op.collection_name
            or lib.lifecycle_mode == "external" or lib.deleted_at is not None):
        await db.rollback()
        raise RebuildTargetError("rebuild operation no longer owns its target")
    if op.status == "running":
        await db.rollback()      # Already activated: no duplicate jobs.
        return
    if op.status != "preparing":
        await db.rollback()
        raise RebuildTargetError("rebuild operation is not preparing")
    jobs = (await db.execute(
        select(EmbeddingJob).where(EmbeddingJob.rebuild_operation_id == op_id)
    )).scalars().all()
    if not jobs and doc_revs:
        # Compatibility for old internal callers; never infer a content ID/no.
        for item in doc_revs:
            if len(item) != 2 or settings.enable_revision_id_worker:
                await db.rollback()
                raise RebuildTargetError("rebuild target snapshot is missing")
            doc_id, generation = item
            doc = await db.get(Document, doc_id)
            if (doc is None or doc.library_id != library_id or doc.deleted_at is not None
                    or doc.current_revision != generation or doc.current_revision_id is not None
                    or getattr(doc, "latest_revision_id", None) is not None):
                await db.rollback()
                raise RebuildTargetError("legacy rebuild target no longer matches")
            job = EmbeddingJob(
                library_id=library_id, document_id=doc_id, status="pending",
                document_revision=generation, rebuild_operation_id=op_id,
            )
            db.add(job)
            jobs.append(job)
    if any(job.library_id != library_id or job.status != "pending" for job in jobs):
        await db.rollback()
        raise RebuildTargetError("rebuild target snapshot is invalid")
    op.expected_job_count = len(jobs)
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
    """Recover the persisted operation snapshot without changing its content revision."""
    lib = (await db.execute(select(Library).where(Library.id == library_id))).scalar_one()
    if lib.qdrant_collection != op.collection_name or op.library_id != library_id:
        await db.rollback()
        raise RebuildTargetError("rebuild collection target changed")
    jobs = (await db.execute(
        select(EmbeddingJob).where(EmbeddingJob.rebuild_operation_id == op.id)
    )).scalars().all()
    if not jobs:
        published = (await db.execute(
            select(func.count()).select_from(Document).where(
                Document.library_id == library_id, Document.deleted_at.is_(None),
                (Document.current_revision_id.is_not(None) | (Document.status == "ready")
                 | select(EmbeddingJob.id).where(
                     EmbeddingJob.document_id == Document.id, EmbeddingJob.status == "done",
                 ).exists()),
            )
        )).scalar_one()
        if published:
            await db.rollback()
            raise RebuildTargetError("historical rebuild has no persisted target snapshot")
    if any(job.library_id != library_id for job in jobs):
        await db.rollback()
        raise RebuildTargetError("rebuild job belongs to another library")
    await _validate_rebuild_snapshot(db, library_id, jobs)
    snapshot = [(j.document_id, j.document_revision, j.document_revision_id, j.document_revision_no) for j in jobs]
    collection, dim, distance = op.collection_name, lib.embedding_dim, lib.vector_distance
    await db.commit()  # Qdrant is always outside this read transaction.
    return (
        collection, dim, distance, snapshot,
    )


async def _validate_rebuild_snapshot(db: AsyncSession, library_id, jobs) -> None:
    """Validate immutable targets before destructive recovery, without committing."""
    if not jobs:
        return
    documents = {d.id: d for d in (await db.execute(
        select(Document).where(Document.id.in_({j.document_id for j in jobs}))
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().all()}
    revision_ids = {j.document_revision_id for j in jobs if j.document_revision_id is not None}
    revisions = {r.id: r for r in (await db.execute(
        select(DocumentRevision).where(DocumentRevision.id.in_(revision_ids))
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().all()} if revision_ids else {}
    for job in jobs:
        doc = documents.get(job.document_id)
        if (doc is None or doc.deleted_at is not None or doc.library_id != library_id
                or job.library_id != library_id or doc.current_revision != job.document_revision):
            raise RebuildTargetError("persisted rebuild target is obsolete")
        if job.document_revision_id is not None:
            revision = revisions.get(job.document_revision_id)
            if (doc.current_revision_id != job.document_revision_id or revision is None
                    or revision.status != "ready" or revision.document_id != doc.id
                    or revision.library_id != library_id
                    or revision.revision_no != job.document_revision_no):
                raise RebuildTargetError("persisted published content revision changed")
        elif (settings.enable_revision_id_worker or doc.current_revision_id is not None
              or doc.latest_revision_id is not None):
            raise RebuildTargetError("persisted legacy rebuild target is unsafe")


async def _set_rebuilding(db: AsyncSession, library_id, op_id) -> bool:
    """恢复未 activate 阶段前：把库置回 rebuilding、operation 置回 preparing（锁序 library→op）。

    编排由同库 session lock 串行；锁内复核 preparing/failed，否则 no-op 返回 False。
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
    if (op.library_id != library_id or lib.active_rebuild_operation_id != op_id
            or op.collection_name != lib.qdrant_collection or lib.deleted_at is not None):
        await db.rollback()
        raise RebuildTargetError("rebuild operation no longer owns its target")
    jobs = (await db.execute(
        select(EmbeddingJob).where(EmbeddingJob.rebuild_operation_id == op_id)
    )).scalars().all()
    await _validate_rebuild_snapshot(db, library_id, jobs)
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
    if (op.library_id != library_id or lib.active_rebuild_operation_id != op_id
            or op.collection_name != lib.qdrant_collection or lib.deleted_at is not None):
        await db.rollback()
        raise RebuildTargetError("rebuild operation no longer owns its target")
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
    """Serialize the entire three-stage operation without a DB transaction over Qdrant."""
    lib = (await db.execute(
        select(Library).where(Library.id == library_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    if lib.lifecycle_mode == "external":
        raise ExternalLibraryError("external library cannot be rebuilt")
    await db.rollback()
    bind = db.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    if not isinstance(engine, AsyncEngine) or engine.dialect.name != "postgresql":
        raise RebuildTargetError("rebuild requires its session's PostgreSQL engine")
    lock_key = int.from_bytes(
        hashlib.blake2b(f"rebuild:{library_id}".encode(), digest_size=8).digest(),
        "big", signed=True,
    )
    async with engine.connect() as connection:
        try:
            acquired = (await connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
            )).scalar_one()
            await connection.commit()
        except BaseException:
            await connection.invalidate()
            raise
        if not acquired:
            raise ConcurrentRebuildError("library rebuild is already being orchestrated")
        try:
            return await _run_rebuild_locked(db, library_id)
        finally:
            try:
                released = (await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )).scalar_one()
                await connection.commit()
                if not released:
                    raise RuntimeError("rebuild advisory lock was not held")
            except BaseException:
                await connection.invalidate()
                raise


async def _run_rebuild_locked(db: AsyncSession, library_id) -> str:
    """编排三阶段，支持**恢复/重试**当前 operation（preparing 续跑、running 收口、failed 重试），
    全程复用 revision、不新建 operation；只有无活动 operation 时才新建（+revision）。
    qdrant 失败 → operation/库 failed 并抛出。"""
    from app.services import qdrant

    # 恢复锚点：sys_libraries.active_rebuild_operation_id（失败时不清空，见 §9）
    lib0 = (await db.execute(
        select(Library).where(Library.id == library_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    # Service 层 external 防护（前置，确保对 external 库绝不进入任何 Qdrant 删/建）
    if lib0.lifecycle_mode == "external":
        raise ExternalLibraryError(f"library {library_id} is external; rebuild not allowed")
    active = None
    if lib0.active_rebuild_operation_id is not None:
        active = (await db.execute(
            select(RebuildOperation).where(RebuildOperation.id == lib0.active_rebuild_operation_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()

    if active is not None and active.status == "running":
        await db.rollback()
        raise ConcurrentRebuildError(
            f"library {library_id} already has an active rebuild operation"
        )

    if active is not None and active.status in ("preparing", "failed"):
        op_id = active.id
        if active.status == "failed" and active.expected_job_count > 0:
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
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    op = (await db.execute(
        select(RebuildOperation).where(RebuildOperation.id == op_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (lib is None or op is None or op.status != "running"
            or op.library_id != lib.id or lib.active_rebuild_operation_id != op.id
            or op.collection_name != lib.qdrant_collection or lib.deleted_at is not None
            or lib.lifecycle_mode == "external"):
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
