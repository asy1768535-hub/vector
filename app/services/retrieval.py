"""Dify 兼容检索：embed → Qdrant search → 转 DifyRecord。

metadata_condition 支持的运算符（按 Dify 文档常见组合）：
  - =, !=, contains, not contains, in, not in, is null, is not null
为减少首版复杂度，只把上述 6 个常用算子映射到 Qdrant filter；
未识别的运算符直接忽略对应条件（不会报错，避免阻断业务）。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from app.schemas.dify import (
    DifyRecord,
    DifyRetrievalRequest,
    DifyRetrievalResponse,
    MetadataConditionGroup,
)
from app.services import embedding, qdrant, source_enrichment

log = logging.getLogger(__name__)


def _to_qdrant_clause(field: str, op: str, value: Any) -> dict[str, Any] | None:
    op_lower = op.lower().strip()
    if op_lower in ("=", "eq"):
        return {"key": field, "match": {"value": value}}
    if op_lower in ("!=", "ne"):
        return {"key": field, "match": {"except": [value]}}
    if op_lower == "contains" and isinstance(value, str):
        return {"key": field, "match": {"text": value}}
    if op_lower in ("in",) and isinstance(value, list):
        return {"key": field, "match": {"any": value}}
    if op_lower in ("is null", "empty"):
        return {"is_empty": {"key": field}}
    if op_lower in ("is not null", "not empty"):
        return {"is_not_empty": {"key": field}}
    log.debug("unsupported comparison_operator=%s for field=%s; skipping", op, field)
    return None


def _build_qdrant_filter(group: MetadataConditionGroup | None) -> dict[str, Any] | None:
    if group is None or not group.conditions:
        return None
    clauses: list[dict[str, Any]] = []
    for cond in group.conditions:
        for name in cond.name:
            clause = _to_qdrant_clause(name, cond.comparison_operator, cond.value)
            if clause is not None:
                clauses.append(clause)
    if not clauses:
        return None
    key = "must" if group.logical_operator.lower() == "and" else "should"
    return {key: clauses}


async def run_retrieval(
    *,
    collection: str,
    embedding_model: str,
    embedding_base_url: str | None,
    request: DifyRetrievalRequest,
    source_config: dict[str, Any] | None = None,
) -> DifyRetrievalResponse:
    vector = await embedding.embed_one(
        request.query, model=embedding_model, base_url=embedding_base_url
    )

    qdrant_filter = _build_qdrant_filter(request.metadata_condition)
    raw = await qdrant.search(
        collection,
        vector,
        limit=request.retrieval_setting.top_k,
        score_threshold=request.retrieval_setting.score_threshold,
        payload_filter=qdrant_filter,
        with_payload=True,
    )

    payloads = [(item.get("payload") or {}) for item in raw]

    # 源库补全：payload 只有外键时，回查源库把正文拼回（未配置则 texts/rows 全 None）
    enr = await source_enrichment.enrich_payloads(source_config, payloads)

    internal_keys = {"text", "title", "library_id", "document_id", "chunk_id", "seq"}
    extra_columns = enr.parsed["extra_columns"] if enr.enabled else []

    records: list[DifyRecord] = []
    for item, payload, enriched, src_row in zip(raw, payloads, enr.texts, enr.rows):
        # 正文优先级：源库补全 > payload.text
        content = enriched if enriched is not None else (payload.get("text") or "")
        title = payload.get("title") or ""
        # 业务 metadata：剥掉内部字段
        metadata = {
            k: v for k, v in payload.items()
            if k not in internal_keys
        }
        metadata.setdefault("document_id", payload.get("document_id"))
        metadata.setdefault("chunk_id", payload.get("chunk_id"))
        # 把源库带回的 extra_columns 也并入 metadata（不覆盖已有键）
        if src_row:
            for col in extra_columns:
                metadata.setdefault(col, src_row.get(col))
        records.append(DifyRecord(
            content=content or "",
            score=float(item.get("score") or 0.0),
            title=title,
            metadata=metadata,
        ))

    log.info("retrieval: collection=%s top_k=%s returned=%s enriched=%s",
             collection, request.retrieval_setting.top_k, len(records),
             "yes" if enr.enabled else "no")
    return DifyRetrievalResponse(records=records)
