"""Dify 兼容检索：embed → Qdrant search → 转 DifyRecord。

metadata_condition 支持的运算符（按 Dify 文档常见组合）：
  - =, !=, contains, not contains, in, not in, is null, is not null
全部映射到 Qdrant filter。遇到**未知运算符**会抛 FilterError（上层转 422），
而不是静默忽略——否则调用方以为过滤生效、实际可能返回不该返回的资料。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.dify import (
    DifyRecord,
    DifyRetrievalRequest,
    DifyRetrievalResponse,
    MetadataConditionGroup,
)
from app.config import settings
from app.services import embedding, qdrant, source_enrichment, visibility
from app.services import rerank as rerank_svc

log = logging.getLogger(__name__)


class FilterError(ValueError):
    """metadata_condition 含无法映射的运算符/取值；上层应转 422 而非静默忽略。"""


def _to_qdrant_clause(field: str, op: str, value: Any) -> dict[str, Any]:
    op_lower = op.lower().strip()
    if op_lower in ("=", "eq"):
        return {"key": field, "match": {"value": value}}
    if op_lower in ("!=", "ne"):
        return {"key": field, "match": {"except": [value]}}
    if op_lower == "contains":
        if not isinstance(value, str):
            raise FilterError(f"'contains' 需要字符串值（field={field}）")
        return {"key": field, "match": {"text": value}}
    if op_lower in ("not contains", "not_contains"):
        if not isinstance(value, str):
            raise FilterError(f"'not contains' 需要字符串值（field={field}）")
        # Qdrant 无「文本不包含」直接算子 → 用嵌套 must_not 取反（可作为一个条件参与组合）
        return {"must_not": [{"key": field, "match": {"text": value}}]}
    if op_lower == "in":
        if not isinstance(value, list):
            raise FilterError(f"'in' 需要列表值（field={field}）")
        return {"key": field, "match": {"any": value}}
    if op_lower in ("not in", "not_in"):
        if not isinstance(value, list):
            raise FilterError(f"'not in' 需要列表值（field={field}）")
        # MatchExcept：字段值不在该列表内
        return {"key": field, "match": {"except": value}}
    if op_lower in ("is null", "empty"):
        return {"is_empty": {"key": field}}
    if op_lower in ("is not null", "not empty"):
        return {"is_not_empty": {"key": field}}
    raise FilterError(f"不支持的 comparison_operator='{op}'（field={field}）")


def _build_qdrant_filter(group: MetadataConditionGroup | None) -> dict[str, Any] | None:
    if group is None or not group.conditions:
        return None
    clauses: list[dict[str, Any]] = []
    for cond in group.conditions:
        for name in cond.name:
            clauses.append(_to_qdrant_clause(name, cond.comparison_operator, cond.value))
    if not clauses:
        return None
    key = "must" if group.logical_operator.lower() == "and" else "should"
    return {key: clauses}


async def _recall_visible(
    db: AsyncSession | None, library, collection: str, vector, *, needed: int,
    score_threshold: float | None = None, payload_filter: dict | None = None,
) -> list[dict]:
    """有界 overfetch + 可见性过滤 + 补召回（#6 §7.2）：返回过滤后的 raw 命中（最多 needed 条）。

    db / library 为空（无生命周期上下文）时退化为单次召回不过滤。
    """
    if db is None or library is None:
        return await qdrant.search(collection, vector, limit=needed,
                                   score_threshold=score_threshold,
                                   payload_filter=payload_filter, with_payload=True)
    factor = max(1, settings.visibility_overfetch_factor)
    per_search_max = settings.visibility_overfetch_max        # 单次召回上限（每轮 limit 不得超过它）
    total_cap = settings.visibility_total_candidate_cap       # 累计候选硬上限
    budget = settings.visibility_latency_budget_ms / 1000.0
    start = time.monotonic()
    limit = min(max(needed * factor, needed), per_search_max)
    seen: set = set()           # 可见且去重后的命中
    examined: set = set()       # 已检视过的所有候选 point id（累计上限用）
    visible: list[dict] = []
    rounds = 0
    while True:
        raw = await qdrant.search(collection, vector, limit=limit,
                                  score_threshold=score_threshold,
                                  payload_filter=payload_filter, with_payload=True)
        payloads = [(it.get("payload") or {}) for it in raw]
        mask = await visibility.compute_visible_mask(db, library, payloads)
        for it, ok in zip(raw, mask):
            pid = it.get("id")
            examined.add(pid)
            if ok and pid not in seen:
                seen.add(pid)
                visible.append(it)
        if len(visible) >= needed:
            break
        rounds += 1
        if rounds > settings.visibility_refetch_max_rounds:
            break
        if len(examined) >= total_cap or len(raw) < limit:   # 累计上限 / Qdrant 已无更多
            break
        if (time.monotonic() - start) > budget:
            log.info("visibility refetch stopped by latency budget (%sms)", settings.visibility_latency_budget_ms)
            break
        limit = min(limit * 2, per_search_max)               # 单次 limit 永不超过 overfetch_max
    return visible[:needed]


async def run_retrieval(
    *,
    collection: str,
    embedding_model: str,
    embedding_base_url: str | None,
    request: DifyRetrievalRequest,
    source_config: dict[str, Any] | None = None,
    rerank_enabled: bool | None = None,
    db: AsyncSession | None = None,
    library=None,
) -> DifyRetrievalResponse:
    vector = await embedding.embed_one(
        request.query, model=embedding_model, base_url=embedding_base_url
    )

    top_k = request.retrieval_setting.top_k
    threshold = request.retrieval_setting.score_threshold or 0.0   # 0 = 不额外过滤（#11）
    # rerank 生效：库级覆盖优先，否则全局，且必须配好了 reranker 地址
    eff_rerank = (
        (rerank_enabled if rerank_enabled is not None else settings.rerank_enabled)
        and rerank_svc.is_configured()
    )
    recall_limit = max(settings.rerank_candidate_k, top_k) if eff_rerank else top_k

    qdrant_filter = _build_qdrant_filter(request.metadata_condition)
    # #11：**不在 Qdrant 召回阶段用 score_threshold 提前过滤**（rerank 模式下会把候选按 vector_score
    # 提前裁掉，threshold 语义应作用于最终分）。召回只做可见性过滤。
    raw = await _recall_visible(
        db, library, collection, vector, needed=recall_limit, payload_filter=qdrant_filter,
    )

    payloads = [(item.get("payload") or {}) for item in raw]

    # 源库补全：payload 只有外键时，回查源库把正文拼回（未配置则 texts/rows 全 None）
    enr = await source_enrichment.enrich_payloads(source_config, payloads)

    internal_keys = {"text", "title", "library_id", "document_id", "chunk_id", "seq"}
    extra_columns = enr.parsed["extra_columns"] if enr.enabled else []

    # 最终展示内容（源库补全优先），同时作为 rerank 的输入
    contents = [
        (enr.texts[i] if enr.texts[i] is not None else (payloads[i].get("text") or ""))
        for i in range(len(raw))
    ]

    # #11：rerank 对**全部召回候选**打分（top_k=recall_limit）→ 之后才按 final_score 过滤+截断；
    # 失败/未启用回退向量序（绝不阻断检索）。
    order, rerank_scores = await rerank_svc.rank_candidates(
        request.query, contents, top_k=recall_limit, enabled=bool(eff_rerank)
    )

    # 先按 final_score 过滤 threshold，再截取 top_k（#11 点 4）。order 已按相关性降序。
    records: list[DifyRecord] = []
    for i in order:
        item, payload, enriched, src_row = raw[i], payloads[i], enr.texts[i], enr.rows[i]
        vector_score = float(item.get("score") or 0.0)
        rr_score = rerank_scores.get(i)
        # final_score：有 rerank_score 用之（rerank 成功命中），否则 vector_score（dense / fallback / 未命中）
        final_score = rr_score if rr_score is not None else vector_score
        if threshold > 0 and final_score < threshold:
            continue
        content = enriched if enriched is not None else (payload.get("text") or "")
        title = payload.get("title") or ""
        metadata = {k: v for k, v in payload.items() if k not in internal_keys}
        metadata.setdefault("document_id", payload.get("document_id"))
        metadata.setdefault("chunk_id", payload.get("chunk_id"))
        if src_row:
            for col in extra_columns:
                metadata.setdefault(col, src_row.get(col))
        # 双分数留痕：vector_score 恒有；rerank_score 仅重排命中时有
        metadata["vector_score"] = vector_score
        if rr_score is not None:
            metadata["rerank_score"] = rr_score
        records.append(DifyRecord(content=content or "", score=final_score, title=title, metadata=metadata))
        if len(records) >= top_k:
            break

    log.info("retrieval: collection=%s top_k=%s thr=%s recalled=%s returned=%s rerank=%s enriched=%s",
             collection, top_k, threshold, len(raw), len(records),
             "yes" if eff_rerank else "no", "yes" if enr.enabled else "no")
    return DifyRetrievalResponse(records=records)
