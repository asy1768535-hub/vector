from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.sync_source import SyncSource


def ensure_sync_source_writable(source: SyncSource) -> None:
    if source.deleted_at is not None or source.status != "active":
        raise ValueError("sync source is not active")


async def get_sync_source(
    db: AsyncSession,
    library: Library,
    source_key: str,
    *,
    include_deleted: bool = False,
) -> SyncSource | None:
    stmt = select(SyncSource).where(
        SyncSource.library_id == library.id,
        SyncSource.source_key == source_key,
    )
    if not include_deleted:
        stmt = stmt.where(SyncSource.deleted_at.is_(None))
    stmt = stmt.order_by(SyncSource.created_at.desc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def require_sync_source(
    db: AsyncSession,
    library: Library,
    source_key: str,
    *,
    include_deleted: bool = False,
) -> SyncSource:
    source = await get_sync_source(db, library, source_key, include_deleted=include_deleted)
    if source is None:
        raise LookupError("sync source not found")
    return source


async def require_active_sync_source(db: AsyncSession, library: Library, source_key: str) -> SyncSource:
    source = await require_sync_source(db, library, source_key, include_deleted=True)
    ensure_sync_source_writable(source)
    return source


async def create_sync_source(db: AsyncSession, library: Library, payload) -> SyncSource:
    source = SyncSource(
        library_id=library.id,
        source_key=payload.source_key,
        display_name=payload.display_name,
        source_type=payload.source_type,
        config=payload.config,
        status=payload.status,
    )
    db.add(source)
    await db.flush()
    return source


async def list_sync_sources(db: AsyncSession, library: Library) -> list[SyncSource]:
    result = await db.execute(
        select(SyncSource)
        .where(SyncSource.library_id == library.id, SyncSource.deleted_at.is_(None))
        .order_by(SyncSource.source_key.asc())
    )
    return list(result.scalars().all())


async def update_sync_source(db: AsyncSession, library: Library, source_key: str, payload) -> SyncSource:
    source = await require_sync_source(db, library, source_key)
    fields = getattr(payload, "model_fields_set", set())
    if "display_name" in fields and payload.display_name is not None:
        source.display_name = payload.display_name
    if "source_type" in fields:
        source.source_type = payload.source_type
    if "config" in fields:
        source.config = payload.config
    if "status" in fields and payload.status is not None:
        source.status = payload.status
    await db.flush()
    return source


async def delete_sync_source(db: AsyncSession, library: Library, source_key: str) -> None:
    from app.services import cleanup as cleanup_service

    source = await require_sync_source(db, library, source_key)
    # Lock the source row early so concurrent upsert_sync_document calls
    # (which also acquire the source via require_active_sync_source) serialise
    # and cannot insert new documents after our cascade loop finishes.
    await db.execute(
        select(SyncSource.id)
        .where(SyncSource.id == source.id)
        .with_for_update()
    )
    now = datetime.now(timezone.utc)

    # Cascade: soft-delete all active documents belonging to this sync source.
    # Batched to avoid locking too many rows at once.
    while True:
        rows = await db.execute(
            select(Document.id)
            .where(
                Document.library_id == library.id,
                Document.sync_source_id == source.id,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.id)
            .limit(200)
            .with_for_update()
        )
        document_ids = list(rows.scalars().all())
        if not document_ids:
            break
        await db.execute(
            update(Document)
            .where(Document.id.in_(document_ids))
            .values(deleted_at=now, status="deleted", updated_at=now)
        )
        await db.execute(
            update(EmbeddingJob)
            .where(
                EmbeddingJob.library_id == library.id,
                EmbeddingJob.document_id.in_(document_ids),
                EmbeddingJob.status.in_(("pending", "processing")),
            )
            .values(status="superseded", finished_at=now)
        )
        for document_id in document_ids:
            await cleanup_service.enqueue_delete_document(db, library, document_id)
        await db.flush()

    source.status = "deleted"
    source.deleted_at = now
    await db.flush()
