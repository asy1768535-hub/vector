"""admin 库管理。建库时同步建 Qdrant collection。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user, current_superuser
from app.casbin.enforcer import has_permission
from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.models.classification_taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    LibraryClassificationLabel,
)
from app.models.library_faq import LibraryFAQQuestion
from app.models.user import User
from app.schemas.admin import (
    LibraryCreate,
    LibraryFAQCreate,
    LibraryFAQRead,
    LibraryFAQUpdate,
    LibraryRead,
    LibraryUpdate,
)
from app.services import (
    audit_log,
    classification_jobs,
    classification_runtime_policy,
    graph_extraction_safety,
    graph_seed,
    knowledge_artifact_jobs,
    knowledge_artifact_policy,
    library_faq,
    qdrant,
    source_enrichment,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)
from app.services.organization_permissions import (
    PermissionMutationResult,
    grant_platform_library_permissions,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/libraries", tags=["admin"])


def _collection_name(slug: str) -> str:
    """Qdrant collection 命名：lib_<slug>。

    slug 是 unique + 不可变（schema 层面没暴露 slug 改写接口），所以名字稳定。
    """
    return f"lib_{slug}"

async def _attach_active_classification_labels(
    db: AsyncSession,
    library: Library,
) -> None:
    taxonomy = (
        await db.execute(
            select(ClassificationTaxonomy).where(
                ClassificationTaxonomy.organization_id == library.organization_id,
                ClassificationTaxonomy.status == "active",
            )
        )
    ).scalar_one_or_none()
    if taxonomy is None:
        return
    labels = (
        await db.execute(
            select(ClassificationLabel)
            .where(
                ClassificationLabel.taxonomy_version_id == taxonomy.id,
                ClassificationLabel.status == "active",
            )
            .order_by(ClassificationLabel.sort_order, ClassificationLabel.key)
        )
    ).scalars().all()
    db.add_all(
        LibraryClassificationLabel(
            library_id=library.id,
            taxonomy_version_id=taxonomy.id,
            label_id=label.id,
            ordinal=ordinal,
        )
        for ordinal, label in enumerate(labels)
    )


def _has_non_empty_source_config(value: object) -> bool:
    return isinstance(value, dict) and bool(value)


def _validate_source_config_or_400(source_config: dict | None) -> dict | None:
    try:
        return source_enrichment.parse_source_config(source_config)
    except source_enrichment.SourceConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid source_config: {exc}") from exc


def _recreated_slug(base_slug: str, generation: int) -> str:
    suffix = f"__r{generation}"
    trimmed_base = base_slug[: 80 - len(suffix)].rstrip("_")
    return f"{trimmed_base}{suffix}"


async def _allocate_recreated_slug(
    db: AsyncSession,
    *,
    base_slug: str,
    name: str,
) -> tuple[str, int] | None:
    result = await db.execute(
        select(Library.slug, Library.deleted_at).where(Library.name == name)
    )
    history = list(result.all())
    if not history or any(deleted_at is None for _, deleted_at in history):
        return None

    generation = max(2, len(history) + 1)
    for _ in range(8):
        candidate = _recreated_slug(base_slug, generation)
        collision = await db.execute(select(Library.id).where(Library.slug == candidate))
        if collision.scalar_one_or_none() is None:
            return candidate, generation
        generation += 1
    return None


@router.post("", response_model=LibraryRead, status_code=status.HTTP_201_CREATED)
async def create_library(
    body: LibraryCreate,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> Library:
    creator_permissions: PermissionMutationResult | None = None
    embedding_model = body.embedding_model or settings.embedding_model
    embedding_dim = body.embedding_dim or settings.embedding_dim
    chunk_size = body.chunk_size or settings.default_chunk_size
    chunk_overlap = body.chunk_overlap or settings.default_chunk_overlap

    # 全文源：普通开关默认开启；显式自定义 source_config 仍按高级配置写入。
    if not body.source_enrichment_enabled and _has_non_empty_source_config(body.source_config):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "source_enrichment_enabled=false conflicts with non-empty source_config",
        )
    if _has_non_empty_source_config(body.source_config):
        source_config = body.source_config
    elif body.source_enrichment_enabled:
        source_config = source_enrichment.conventional_config(body.slug)
    else:
        source_config = None
    # 校验（非法直接 400，不落库；None 表示关闭）
    _validate_source_config_or_400(source_config)

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
        retrieval_mode=body.retrieval_mode or "dense",
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        qdrant_collection="",  # 写完 ID 后再 set
        source_config=source_config,
        graph_extraction_enabled=body.graph_extraction_enabled,
        schema_mode=body.schema_mode,
        graph_extraction_build_mode=body.graph_extraction_build_mode,
        external_llm_enabled=body.external_llm_enabled,
        graph_extraction_allowed_security_levels=(
            graph_extraction_safety.normalize_allowed_security_levels(
                body.graph_extraction_allowed_security_levels
            )
        ),
        knowledge_artifact_auto_enabled=True,
        summary_artifact_enabled=True,
        outline_artifact_enabled=True,
        knowledge_artifact_external_model_enabled=body.external_llm_enabled,
        knowledge_artifact_allowed_security_levels=(
            graph_extraction_safety.normalize_allowed_security_levels(
                body.graph_extraction_allowed_security_levels
            )
        ),
        revision_retention_enabled=body.revision_retention_enabled,
        classification_auto_enabled=True,
        classification_external_model_enabled=True,
        classification_allowed_security_levels=["internal"],
        revision_retention_days=body.revision_retention_days,
        revision_retention_notice_days=body.revision_retention_notice_days,
        created_by=actor.id,
    )
    lib.qdrant_collection = _collection_name(body.slug)
    creation_generation = 1
    db.add(lib)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        recreated = await _allocate_recreated_slug(
            db,
            base_slug=body.slug,
            name=body.name,
        )
        if recreated is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "已有同名知识库；如需重建，请先删除当前活动库",
            ) from exc
        lib.slug, creation_generation = recreated
        lib.qdrant_collection = _collection_name(lib.slug)
        if body.source_enrichment_enabled and not _has_non_empty_source_config(body.source_config):
            lib.source_config = source_enrichment.conventional_config(lib.slug)
        db.add(lib)
        try:
            await db.flush()
        except IntegrityError as retry_exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "知识库重建ID分配冲突，请重试",
            ) from retry_exc

    if body.schema_mode == "governed" and body.schema_template == "enterprise":
        await graph_seed.seed_enterprise_ontology(db, lib)
    await _attach_active_classification_labels(db, lib)

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

    try:
        if settings.organization_authorization_enabled:
            creator_permissions = await grant_platform_library_permissions(
                db,
                actor_user_id=actor.id,
                target_user_id=actor.id,
                library=lib,
                actions=("read", "insert", "delete", "admin"),
            )
        await audit_log.record(
            db, actor.id, "library.create",
            {
                "library_id": str(lib.id),
                "slug": lib.slug,
                "requested_slug": body.slug,
                "creation_generation": creation_generation,
                "collection": lib.qdrant_collection,
                "graph_extraction_enabled": lib.graph_extraction_enabled,
                "schema_mode": lib.schema_mode,
                "schema_template": body.schema_template,
            },
        )
        await db.commit()
    except Exception:
        if creator_permissions is not None:
            creator_permissions.compensate()
        await db.rollback()
        raise
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
        # retrieval_mode 是实值字段（dense/hybrid，永不为 None）：None=没传=不改，非 None=改。
        "retrieval_mode",
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

    # 全文源：source_enrichment_enabled 是普通 UI 开关；source_config 保留高级哨兵能力。
    source_config_provided = "source_config" in body.model_fields_set
    enrichment_switch_provided = "source_enrichment_enabled" in body.model_fields_set
    if body.source_enrichment_enabled is False and _has_non_empty_source_config(body.source_config):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "source_enrichment_enabled=false conflicts with non-empty source_config",
        )

    if source_config_provided and _has_non_empty_source_config(body.source_config):
        _validate_source_config_or_400(body.source_config)
        lib.source_config = body.source_config
        changes["source_config"] = body.source_config
    elif source_config_provided and body.source_config == {}:
        lib.source_config = None
        changes["source_config"] = None
    elif enrichment_switch_provided:
        if body.source_enrichment_enabled is False:
            lib.source_config = None
            changes["source_config"] = None
        elif body.source_enrichment_enabled is True and lib.source_config is None:
            source_config = source_enrichment.conventional_config(lib.slug)
            _validate_source_config_or_400(source_config)
            lib.source_config = source_config
            changes["source_config"] = source_config

    if "schema_mode" in body.model_fields_set and "graph_extraction_enabled" in body.model_fields_set:
        if (body.schema_mode == "disabled") != (body.graph_extraction_enabled is False):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "schema_mode and graph_extraction_enabled must describe the same mode",
            )

    safety_change_error = None
    if "schema_mode" in body.model_fields_set:
        lib.schema_mode = body.schema_mode
        changes["schema_mode"] = body.schema_mode
        if "graph_extraction_enabled" not in body.model_fields_set:
            lib.graph_extraction_enabled = body.schema_mode != "disabled"
            changes["graph_extraction_enabled"] = lib.graph_extraction_enabled
            if not lib.graph_extraction_enabled:
                safety_change_error = "library_opt_out"

    for field in (
        "graph_extraction_enabled",
        "graph_extraction_build_mode",
        "graph_assisted_chat_mode",
        "external_llm_enabled",
    ):
        if field in body.model_fields_set:
            value = getattr(body, field)
            setattr(lib, field, value)
            changes[field] = value
            if field == "graph_extraction_enabled" and "schema_mode" not in body.model_fields_set:
                lib.schema_mode = "disabled" if value is False else (
                    lib.schema_mode if lib.schema_mode != "disabled" else "governed"
                )
                changes["schema_mode"] = lib.schema_mode
            if value is False:
                safety_change_error = "library_opt_out"

    if "graph_extraction_allowed_security_levels" in body.model_fields_set:
        old_levels = list(lib.graph_extraction_allowed_security_levels or [])
        levels = graph_extraction_safety.normalize_allowed_security_levels(
            body.graph_extraction_allowed_security_levels
        )
        lib.graph_extraction_allowed_security_levels = levels
        changes["graph_extraction_allowed_security_levels"] = levels
        if levels != old_levels and safety_change_error is None:
            safety_change_error = "security_allowlist_changed"

    if safety_change_error is not None:
        changes["cancelled_graph_extraction_jobs"] = (
            await graph_extraction_safety.cancel_library_jobs_for_safety_change(
                db,
                library_id=lib.id,
                job_error_code=safety_change_error,
            )
        )

    cancelled_artifact_types: set[str] = set()
    cancel_model_artifact_jobs = (
        "external_llm_enabled" in body.model_fields_set
        and body.external_llm_enabled is False
        and bool(lib.knowledge_artifact_external_model_enabled)
    )
    for field in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
    ):
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        setattr(lib, field, value)
        changes[field] = value
        if value is False and field == "summary_artifact_enabled":
            cancelled_artifact_types.add("summary")
        elif value is False and field == "outline_artifact_enabled":
            cancelled_artifact_types.add("outline")
        elif value is False and field == "knowledge_artifact_external_model_enabled":
            cancel_model_artifact_jobs = True

    artifact_policy_changed = False
    if "knowledge_artifact_allowed_security_levels" in body.model_fields_set:
        old_levels = list(lib.knowledge_artifact_allowed_security_levels or [])
        levels = knowledge_artifact_policy.normalize_knowledge_artifact_security_levels(
            body.knowledge_artifact_allowed_security_levels
        )
        lib.knowledge_artifact_allowed_security_levels = levels
        changes["knowledge_artifact_allowed_security_levels"] = levels
        artifact_policy_changed = levels != old_levels
        cancel_model_artifact_jobs = cancel_model_artifact_jobs or artifact_policy_changed

    retention_fields = {
        "revision_retention_enabled",
        "revision_retention_days",
        "revision_retention_notice_days",
    }
    if retention_fields & body.model_fields_set:
        retention_days = (
            body.revision_retention_days
            if "revision_retention_days" in body.model_fields_set
            else lib.revision_retention_days
        )
        notice_days = (
            body.revision_retention_notice_days
            if "revision_retention_notice_days" in body.model_fields_set
            else lib.revision_retention_notice_days
        )
        if notice_days >= retention_days:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "revision retention notice days must be less than retention days",
            )
        for field in sorted(retention_fields & body.model_fields_set):
            value = getattr(body, field)
            setattr(lib, field, value)
            changes[field] = value

    if cancelled_artifact_types or cancel_model_artifact_jobs:
        error_code = (
            "library_artifact_policy_changed"
            if artifact_policy_changed or cancel_model_artifact_jobs
            else "library_artifact_type_disabled"
        )
        changes["cancelled_knowledge_artifact_jobs"] = (
            await knowledge_artifact_jobs.cancel_library_artifact_jobs_for_policy_change(
                db,
                library_id=lib.id,
                error_code=error_code,
                artifact_types=tuple(sorted(cancelled_artifact_types)),
                include_model_jobs=cancel_model_artifact_jobs,
            )
        )

    cancel_classification_jobs = (
        "external_llm_enabled" in body.model_fields_set
        and body.external_llm_enabled is False
        and bool(lib.classification_external_model_enabled)
    )
    for field in (
        "classification_auto_enabled",
        "classification_external_model_enabled",
    ):
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        setattr(lib, field, value)
        changes[field] = value
        if value is False:
            cancel_classification_jobs = True

    if "classification_allowed_security_levels" in body.model_fields_set:
        old_levels = list(lib.classification_allowed_security_levels or [])
        levels = classification_runtime_policy.normalize_classification_security_levels(
            body.classification_allowed_security_levels
        )
        lib.classification_allowed_security_levels = levels
        changes["classification_allowed_security_levels"] = levels
        cancel_classification_jobs = cancel_classification_jobs or levels != old_levels

    if cancel_classification_jobs:
        changes["cancelled_classification_jobs"] = (
            await classification_jobs.cancel_library_classification_jobs_for_policy_change(
                db,
                library_id=lib.id,
                error_code="library_classification_policy_changed",
            )
        )

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


# ── 常用问题（FAQ）：read 可看 active；admin/superuser 可管理 ───────────────
async def _can_manage_or_see_inactive(
    db: AsyncSession,
    user: User,
    library: Library,
) -> bool:
    """是否可管理 FAQ / 查看 inactive：superuser 直通，否则需库级 admin 权限。"""
    if not settings.organization_authorization_enabled:
        return user.is_superuser or has_permission(str(user.id), library.slug, "admin")
    try:
        await authorize_library_management(db, user=user, library=library)
        return True
    except OrganizationAuthorizationError:
        return False


@router.get("/{slug}/faqs", response_model=list[LibraryFAQRead])
async def list_library_faqs(
    slug: str,
    include_inactive: bool = Query(default=False),
    user: User = Depends(current_active_user),
    lib: Library = Depends(require_lib("read")),     # read 权限即可读；库不存在/无权 → 403
    db: AsyncSession = Depends(get_db),
) -> list[LibraryFAQQuestion]:
    # include_inactive 只对 admin/superuser 生效；普通 read 用户强制只看 active
    effective_inactive = include_inactive and await _can_manage_or_see_inactive(
        db, user, lib
    )
    return await library_faq.list_faqs(db, lib.id, include_inactive=effective_inactive)


@router.post("/{slug}/faqs", response_model=LibraryFAQRead, status_code=status.HTTP_201_CREATED)
async def create_library_faq(
    slug: str,
    body: LibraryFAQCreate,
    actor: User = Depends(current_active_user),
    lib: Library = Depends(require_lib("admin")),    # 需库级 admin 或 superuser
    db: AsyncSession = Depends(get_db),
) -> LibraryFAQQuestion:
    faq = await library_faq.create_faq(db, lib.id, body)
    await audit_log.record(db, actor.id, "library.faq.create", {"slug": slug, "faq_id": str(faq.id)})
    await db.commit()
    await db.refresh(faq)
    return faq


@router.patch("/{slug}/faqs/{faq_id}", response_model=LibraryFAQRead)
async def update_library_faq(
    slug: str,
    faq_id: uuid.UUID,
    body: LibraryFAQUpdate,
    actor: User = Depends(current_active_user),
    lib: Library = Depends(require_lib("admin")),
    db: AsyncSession = Depends(get_db),
) -> LibraryFAQQuestion:
    faq = await library_faq.get_faq(db, lib.id, faq_id)
    if faq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "faq not found")
    faq = await library_faq.update_faq(db, faq, body)
    await audit_log.record(db, actor.id, "library.faq.update", {"slug": slug, "faq_id": str(faq_id)})
    await db.commit()
    await db.refresh(faq)
    return faq


@router.delete("/{slug}/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_faq(
    slug: str,
    faq_id: uuid.UUID,
    actor: User = Depends(current_active_user),
    lib: Library = Depends(require_lib("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    faq = await library_faq.get_faq(db, lib.id, faq_id)
    if faq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "faq not found")
    await library_faq.delete_faq(db, faq)
    await audit_log.record(db, actor.id, "library.faq.delete", {"slug": slug, "faq_id": str(faq_id)})
    await db.commit()
    return None
