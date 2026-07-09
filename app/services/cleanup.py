"""Cleanup outbox 服务（#7 §4.4 / §8）：入队（幂等）+ 单条执行（幂等删 Qdrant）。

入队在删除/更新的同一事务内调用（不 commit，由调用方提交）。执行由 Cleanup Worker 调用。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cleanup_outbox import (
    CleanupOutbox,
    EVENT_DELETE_COLLECTION,
    EVENT_DELETE_DOCUMENT_ALL,
    EVENT_DELETE_DOCUMENT_BEFORE_REVISION,
    EVENT_DELETE_DOCUMENT_REVISION,
    EVENT_DELETE_UNPUBLISHED_REVISION_POINTS,
)

log = logging.getLogger(__name__)


async def _enqueue(db: AsyncSession, *, event_type, library_id, collection_name,
                   idempotency_key, document_id=None, target_revision=None,
                   payload=None) -> None:
    """插一条 outbox（ON CONFLICT(idempotency_key) DO NOTHING → 幂等，重复删除不产生重复任务）。

    不提交：与触发它的删除/更新同事务，由调用方一起 commit（事务性 outbox）。
    """
    stmt = pg_insert(CleanupOutbox).values(
        id=uuid.uuid4(),
        event_type=event_type,
        library_id=library_id,
        document_id=document_id,
        collection_name=collection_name,
        target_revision=target_revision,
        payload=payload,
        idempotency_key=idempotency_key,
        status="pending",
    ).on_conflict_do_nothing(index_elements=["idempotency_key"])
    await db.execute(stmt)


async def enqueue_delete_document(db, library, document_id) -> None:
    from app.services import graph_evidence

    await graph_evidence.mark_document_graph_evidence_stale(db, library, document_id=document_id)
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_ALL, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        idempotency_key=f"delete-document:{document_id}",
    )


async def enqueue_delete_before_revision(db, library, document_id, target_revision) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_BEFORE_REVISION, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        target_revision=target_revision,
        idempotency_key=f"delete-before-revision:{document_id}:{target_revision}",
    )


async def enqueue_delete_document_revision(db, library, document_id, document_revision_id) -> None:
    from app.services import graph_evidence

    await graph_evidence.mark_document_revision_graph_evidence_stale(
        db, library, document_revision_id=document_revision_id
    )
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_REVISION, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        payload={"document_revision_id": str(document_revision_id)},
        idempotency_key=f"delete-document-revision:{document_id}:{document_revision_id}",
    )


async def enqueue_delete_unpublished_revision_points(db, library, document_id, document_revision_id) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_UNPUBLISHED_REVISION_POINTS, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        payload={"document_revision_id": str(document_revision_id)},
        idempotency_key=f"delete-unpublished-revision:{document_id}:{document_revision_id}",
    )


async def enqueue_delete_collection(db, library) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_COLLECTION, library_id=library.id,
        collection_name=library.qdrant_collection,
        idempotency_key=f"delete-collection:{library.qdrant_collection}",
    )


async def execute_event(row: CleanupOutbox) -> None:
    """按 event_type 执行 Qdrant 物理清理（幂等：不存在的 point/collection 视为成功）。"""
    from app.services import qdrant
    if row.event_type == EVENT_DELETE_DOCUMENT_ALL:
        await qdrant.delete_points_by_document_id(row.collection_name, str(row.document_id))
    elif row.event_type == EVENT_DELETE_DOCUMENT_BEFORE_REVISION:
        await qdrant.delete_points_before_revision(
            row.collection_name, str(row.document_id), int(row.target_revision))
    elif row.event_type in (EVENT_DELETE_DOCUMENT_REVISION, EVENT_DELETE_UNPUBLISHED_REVISION_POINTS):
        document_revision_id = (row.payload or {}).get("document_revision_id")
        if not document_revision_id:
            raise ValueError(f"cleanup event missing document_revision_id: {row.id}")
        await qdrant.delete_points_by_document_revision_id(row.collection_name, str(document_revision_id))
    elif row.event_type == EVENT_DELETE_COLLECTION:
        await qdrant.delete_collection(row.collection_name)
    else:
        raise ValueError(f"unknown cleanup event_type: {row.event_type}")
