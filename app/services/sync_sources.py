from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    source = await require_sync_source(db, library, source_key)
    source.status = "deleted"
    source.deleted_at = datetime.now(timezone.utc)
    await db.flush()
