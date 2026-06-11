"""admin 库管理。建库时同步建 Qdrant collection。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import LibraryCreate, LibraryRead, LibraryUpdate
from app.services import audit_log, qdrant, source_enrichment

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/libraries", tags=["admin"])


def _collection_name(slug: str) -> str:
    """Qdrant collection 命名：lib_<slug>。

    slug 是 unique + 不可变（schema 层面没暴露 slug 改写接口），所以名字稳定。
    """
    return f"lib_{slug}"


@router.post("", response_model=LibraryRead, status_code=status.HTTP_201_CREATED)
async def create_library(
    body: LibraryCreate,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    embedding_model = body.embedding_model or settings.embedding_model
    embedding_dim = body.embedding_dim or settings.embedding_dim
    chunk_size = body.chunk_size or settings.default_chunk_size
    chunk_overlap = body.chunk_overlap or settings.default_chunk_overlap

    # 全文源：默认按约定自动生成（表=slug、text_id→content、bigint、库=.env）；
    # 仅当显式传 source_config 时才用自定义结构（高级/脚本用法）。
    if body.source_config:
        source_config = body.source_config
    else:
        source_config = source_enrichment.conventional_config(body.slug)
    # 校验（非法直接 400，不落库）
    try:
        source_enrichment.parse_source_config(source_config)
    except source_enrichment.SourceConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid source_config: {exc}") from exc

    lib = Library(
        slug=body.slug,
        name=body.name,
        description=body.description,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        vector_distance=body.vector_distance,
        embedding_base_url=body.embedding_base_url,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        qdrant_collection="",  # 写完 ID 后再 set
        source_config=source_config,
        created_by=actor.id,
    )
    lib.qdrant_collection = _collection_name(body.slug)
    db.add(lib)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already exists") from exc

    # 建 Qdrant collection（失败回滚库记录）
    try:
        await qdrant.ensure_collection(
            lib.qdrant_collection,
            dim=lib.embedding_dim,
            distance=lib.vector_distance,
        )
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.exception("ensure_collection failed for %s", lib.slug)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"qdrant collection creation failed: {exc}"
        ) from exc

    await audit_log.record(
        db, actor.id, "library.create",
        {"library_id": str(lib.id), "slug": lib.slug, "collection": lib.qdrant_collection},
    )
    await db.commit()
    await db.refresh(lib)
    return lib


@router.get("", response_model=list[LibraryRead])
async def list_libraries(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_deleted: bool = Query(default=False),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[Library]:
    stmt = select(Library).order_by(Library.created_at.desc()).limit(limit).offset(offset)
    if not include_deleted:
        stmt = stmt.where(Library.deleted_at.is_(None))
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.get("/{slug}", response_model=LibraryRead)
async def get_library(
    slug: str,
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    row = await db.execute(select(Library).where(Library.slug == slug))
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    return lib


@router.patch("/{slug}", response_model=LibraryRead)
async def update_library(
    slug: str,
    body: LibraryUpdate,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    """所有字段可改。注意：
    - 改 embedding_dim / vector_distance 后已有 Qdrant collection 结构对不上，
      之后新 embed 会失败，需要调 POST /admin/libraries/{slug}/rebuild-collection。
    - 改 embedding_model / embedding_base_url 不会重 embed 已有 chunk；
      新提交的文档用新参数。
    """
    row = await db.execute(select(Library).where(Library.slug == slug, Library.deleted_at.is_(None)))
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    changes: dict[str, object] = {}
    editable_fields = (
        "name", "description", "chunk_size", "chunk_overlap",
        "embedding_model", "embedding_dim", "vector_distance", "embedding_base_url",
    )
    for field in editable_fields:
        if not hasattr(body, field):
            continue
        value = getattr(body, field)
        if value is not None:
            setattr(lib, field, value)
            changes[field] = value

    # 全文源（高级哨兵）：不传 → 不变；传 {} → 清空；传非空 dict → 校验后写入
    if body.source_config is not None:
        if body.source_config == {}:
            lib.source_config = None
            changes["source_config"] = None
        else:
            try:
                source_enrichment.parse_source_config(body.source_config)
            except source_enrichment.SourceConfigError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"invalid source_config: {exc}"
                ) from exc
            lib.source_config = body.source_config
            changes["source_config"] = body.source_config

    if changes:
        await audit_log.record(db, actor.id, "library.update", {"slug": slug, **changes})
    await db.commit()
    await db.refresh(lib)
    return lib


@router.post("/{slug}/rebuild-collection", response_model=LibraryRead)
async def rebuild_collection(
    slug: str,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    """重建 Qdrant collection：删旧建新（用当前 PG 里的 embedding_dim / distance）
    + 把所有该库的文档标 pending、对应 embedding_jobs 标 pending，worker 会重新 embed 全部。
    """
    from app.models.document import Document
    from app.models.embedding_job import EmbeddingJob

    row = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None))
    )
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")

    # 1. 删旧 collection（容忍不存在）
    try:
        await qdrant.delete_collection(lib.qdrant_collection)
    except Exception:  # noqa: BLE001
        log.exception("delete_collection failed; continuing")

    # 2. 用当前 PG 参数重新建
    try:
        await qdrant.ensure_collection(
            lib.qdrant_collection,
            dim=lib.embedding_dim,
            distance=lib.vector_distance,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"qdrant rebuild failed: {exc}",
        ) from exc

    # 3. 重置所有文档与 job 状态
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Document)
        .where(Document.library_id == lib.id, Document.deleted_at.is_(None))
        .values(status="pending", last_error=None)
    )
    # 旧 job 全部标 pending 让 worker 重抢；同时清掉 worker_id/claim 时间
    await db.execute(
        sa_update(EmbeddingJob)
        .where(EmbeddingJob.library_id == lib.id)
        .values(status="pending", worker_id=None, claimed_at=None,
                finished_at=None, attempt_count=0, last_error=None)
    )

    await audit_log.record(
        db, actor.id, "library.rebuild_collection",
        {"slug": slug, "collection": lib.qdrant_collection,
         "embedding_dim": lib.embedding_dim, "distance": lib.vector_distance},
    )
    await db.commit()
    await db.refresh(lib)
    return lib


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library(
    slug: str,
    background: BackgroundTasks,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.execute(select(Library).where(Library.slug == slug, Library.deleted_at.is_(None)))
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    lib.deleted_at = datetime.now(timezone.utc)
    collection = lib.qdrant_collection
    await audit_log.record(db, actor.id, "library.delete", {"slug": slug})
    await db.commit()

    # Qdrant 异步清理（失败只记日志）
    async def _purge():
        try:
            await qdrant.delete_collection(collection)
        except Exception:  # noqa: BLE001
            log.exception("delete_collection failed: %s", collection)

    background.add_task(_purge)
    return None
