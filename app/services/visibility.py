"""检索可见性过滤（#6 批次 A，§3.2 / §7.2）。

按库级 lifecycle_mode 分流：
  - external：整库绕过（由外部系统/源库补全管理，不保证一致性）。
  - managed：Qdrant 召回后回查 PG，丢弃「不存在 / payload 缺 document_id / library 不匹配 /
    payload.document_revision != current_revision」的候选。批次 B 再加 deleted_at 维度。

注意 payload 的字段名是 `document_revision`，缺失按过渡策略视作 1。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision


def _missing_revision_ok(payload_rev: Any) -> int:
    # 过渡兼容：历史 point 无 document_revision → 视作 1
    try:
        return int(payload_rev) if payload_rev is not None else 1
    except (TypeError, ValueError):
        return 1


async def compute_visible_mask(
    db: AsyncSession, library, payloads: list[dict]
) -> list[bool]:
    """返回与 payloads 等长的可见性 bool。managed 库回查 PG；external 库全 True。"""
    # external 库或总开关关闭 → 不过滤
    if getattr(library, "lifecycle_mode", "managed") == "external" or not settings.retrieval_consistency_filter:
        return [True] * len(payloads)

    # 收集候选 document_id（缺失者直接不可见）
    ids: list[str] = []
    for p in payloads:
        did = p.get("document_id")
        if did:
            ids.append(str(did))
    info: dict[str, tuple] = {}
    if ids:
        if settings.enable_revision_id_visibility:
            rows = (await db.execute(
                select(
                    Document.id,
                    Document.library_id,
                    Document.current_revision,
                    Document.current_revision_id,
                    Document.deleted_at,
                    DocumentRevision.status,
                )
                .outerjoin(DocumentRevision, DocumentRevision.id == Document.current_revision_id)
                .where(Document.id.in_(list(set(ids))))
            )).all()
            info = {
                str(did): (str(lib_id), int(rev), str(cur_rev_id) if cur_rev_id else None, deleted, rev_status)
                for did, lib_id, rev, cur_rev_id, deleted, rev_status in rows
            }
        else:
            rows = (await db.execute(
                select(Document.id, Document.library_id, Document.current_revision, Document.deleted_at).where(
                    Document.id.in_(list(set(ids)))
                )
            )).all()
            info = {str(did): (str(lib_id), int(rev), deleted) for did, lib_id, rev, deleted in rows}

    lib_id = str(library.id)
    mask: list[bool] = []
    for p in payloads:
        did = p.get("document_id")
        if not did:
            mask.append(False)            # managed 库缺 document_id = 数据异常 → 丢弃
            continue
        rec = info.get(str(did))
        if rec is None:                    # 不存在（硬删/异常）
            mask.append(False)
            continue
        if settings.enable_revision_id_visibility:
            doc_lib, cur_rev, current_revision_id, deleted_at, current_revision_status = rec
        else:
            doc_lib, cur_rev, deleted_at = rec
            current_revision_id = None
            current_revision_status = None
        if deleted_at is not None:         # #7 已 tombstone → 立即不可见（不等 Qdrant 清理）
            mask.append(False)
            continue
        if doc_lib != lib_id:              # 库不匹配
            mask.append(False)
            continue
        payload_revision_id = p.get("document_revision_id")
        if settings.enable_revision_id_visibility and payload_revision_id:
            mask.append(str(payload_revision_id) == current_revision_id and current_revision_status == "ready")
            continue
        mask.append(_missing_revision_ok(p.get("document_revision")) == cur_rev)
    return mask
