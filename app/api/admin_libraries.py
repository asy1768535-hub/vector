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
        embed_batch_size=body.embed_batch_size,
        rerank_enabled=body.rerank_enabled,
        ocr_enabled=body.ocr_enabled,
        docx_table_aware=body.docx_table_aware,
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
    # 锁新鲜库行（FOR UPDATE，与 rebuild 互斥）；重建中禁止改配置（dim/distance 等会与在建 collection 冲突）
    row = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None)).with_for_update()
    )
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    if lib.index_state != "ready":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "library index rebuilding，暂不可改配置",
            headers={"Retry-After": "5"},
        )
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

    # embed_batch_size 特殊：用 model_fields_set 区分「没传=不改」与「显式传 null=清空回全局」，
    # 这样前端能把库级覆盖清回全局默认（其余 Optional 字段仍是 None=不改的旧语义）。
    if "embed_batch_size" in body.model_fields_set:
        lib.embed_batch_size = body.embed_batch_size
        changes["embed_batch_size"] = body.embed_batch_size
    # rerank_enabled 同理：null 可清回"继承全局"
    if "rerank_enabled" in body.model_fields_set:
        lib.rerank_enabled = body.rerank_enabled
        changes["rerank_enabled"] = body.rerank_enabled
    # ocr_enabled 同理：null 可清回"继承全局"
    if "ocr_enabled" in body.model_fields_set:
        lib.ocr_enabled = body.ocr_enabled
        changes["ocr_enabled"] = body.ocr_enabled
    # docx_table_aware 同理：null 可清回"继承全局"
    if "docx_table_aware" in body.model_fields_set:
        lib.docx_table_aware = body.docx_table_aware
        changes["docx_table_aware"] = body.docx_table_aware

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


@router.post("/{slug}/test-embedding")
async def test_embedding(
    slug: str,
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """就地探活该库的 embedding 配置（模型 + base_url；key 走全局 settings）。

    用于建/改库后确认这个库真的能用 —— 避免配错 model/base_url 后等到摄入才发现。
    """
    from app.services import selfcheck

    row = await db.execute(select(Library).where(Library.slug == slug, Library.deleted_at.is_(None)))
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    ok, msg, dim = await selfcheck.probe_embedding_config(
        model=lib.embedding_model,
        base_url=lib.embedding_base_url,
        expected_dim=lib.embedding_dim,
    )
    return {
        "ok": ok,
        "message": msg,
        "dim": dim,
        "embedding_model": lib.embedding_model,
        "embedding_base_url": lib.embedding_base_url or settings.embedding_base_url,
    }


@router.post("/{slug}/rebuild-collection", response_model=LibraryRead)
async def rebuild_collection(
    slug: str,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    """重建 Qdrant collection（#6 §9 三阶段：prepare → qdrant → activate + finalize）。

    external 库 409（外部 collection 本系统不得删/建）；已在重建中 409（单活动 operation）。
    每篇活动文档 +revision、supersede 旧 job、新建带 rebuild_operation_id 的 job，worker 重 embed。
    """
    from sqlalchemy.exc import IntegrityError
    from app.services import rebuild as rebuild_svc

    row = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None))
    )
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    if lib.lifecycle_mode == "external":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "external 库由外部系统管理，禁止 rebuild（不会删除其 collection）",
        )

    await audit_log.record(
        db, actor.id, "library.rebuild_collection",
        {"slug": slug, "collection": lib.qdrant_collection,
         "embedding_dim": lib.embedding_dim, "distance": lib.vector_distance},
    )
    await db.commit()

    try:
        await rebuild_svc.run_rebuild(db, lib.id)
    except rebuild_svc.ExternalLibraryError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "external 库不支持 rebuild")
    except (rebuild_svc.ConcurrentRebuildError, IntegrityError):
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该库已有进行中的重建任务")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"rebuild failed: {exc}") from exc

    await db.refresh(lib)
    return lib


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library(
    slug: str,
    background: BackgroundTasks,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None)).with_for_update()
    )
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    if lib.index_state != "ready":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "library index rebuilding，暂不可删除",
            headers={"Retry-After": "5"},
        )
    lib.deleted_at = datetime.now(timezone.utc)
    # 终止该库在途 embedding job（否则删 collection 后它们会反复失败）
    from sqlalchemy import update as sa_update
    from app.models.embedding_job import EmbeddingJob as _EJob
    await db.execute(
        sa_update(_EJob).where(
            _EJob.library_id == lib.id, _EJob.status.in_(("pending", "processing"))
        ).values(status="superseded", finished_at=datetime.now(timezone.utc))
    )
    # external 库的 collection 由外部系统管理：**绝不删除其 Qdrant collection**（只取消注册）。
    is_external = lib.lifecycle_mode == "external"
    await audit_log.record(db, actor.id, "library.delete", {"slug": slug, "external": is_external})
    # #7：managed 库改为单事务入 cleanup outbox（替代不可靠的 BackgroundTask）；external 跳过。
    if not is_external:
        from app.services import cleanup as cleanup_service
        await cleanup_service.enqueue_delete_collection(db, lib)
    await db.commit()
    if is_external:
        log.info("library.delete: slug=%s 是 external，跳过 Qdrant collection 删除（%s）", slug, lib.qdrant_collection)
    return None
