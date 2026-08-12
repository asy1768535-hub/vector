"""Dify 兼容检索：embed → Qdrant search → 转 DifyRecord。

metadata_condition 支持的运算符（按 Dify 文档常见组合）：
  - =, !=, contains, not contains, in, not in, is null, is not null
全部映射到 Qdrant filter。遇到**未知运算符**会抛 FilterError（上层转 422），
而不是静默忽略——否则调用方以为过滤生效、实际可能返回不该返回的资料。
"""
from __future__ import annotations

import logging
import hashlib
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
from app.services import (
    embedding,
    keyword_search,
    llm_query_rewrite,
    qdrant,
    query_rewrite,
    source_enrichment,
    visibility,
)
from app.services.evidence_locator_projection import validate_projection
from app.services import rerank as rerank_svc

log = logging.getLogger(__name__)


def _content_hash(content: str) -> str | None:
    normalized = " ".join(content.split())
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _duplicate_source(payload: dict[str, Any]) -> dict[str, Any]:
    source: dict[str, Any] = {}
    for key in (
        "library_id",
        "document_id",
        "document_revision_id",
        "document_revision",
        "source_path",
        "relative_path",
        "chunk_id",
        "seq",
        "title",
    ):
        value = payload.get(key)
        if value is not None:
            source[key] = str(value) if key != "seq" else value
    return source


def _add_duplicate_source(record: DifyRecord, payload: dict[str, Any]) -> None:
    metadata = record.metadata
    metadata["duplicate_count"] = int(metadata.get("duplicate_count") or 0) + 1
    sources = metadata.setdefault("duplicate_sources", [])
    source = _duplicate_source(payload)
    if source and source not in sources and len(sources) < 4:
        sources.append(source)


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
    exact_vector_search: bool = False,
) -> list[dict]:
    """有界 overfetch + 可见性过滤 + 补召回（#6 §7.2）：返回过滤后的 raw 命中（最多 needed 条）。

    db / library 为空（无生命周期上下文）时退化为单次召回不过滤。
    """
    if db is None or library is None:
        return await qdrant.search(collection, vector, limit=needed,
                                   score_threshold=score_threshold,
                                   payload_filter=payload_filter, with_payload=True,
                                   exact=exact_vector_search)
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
                                  payload_filter=payload_filter, with_payload=True,
                                  exact=exact_vector_search)
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


def _hit_chunk_id(hit: dict) -> str:
    """命中项的稳定去重键：优先 payload.chunk_id，回退 Qdrant point id（二者通常相等）。"""
    return str((hit.get("payload") or {}).get("chunk_id") or hit.get("id"))


# rewrite_source 输出的固定优先级顺序（便于稳定展示/断言）
_SOURCE_ORDER = {"original": 0, "rule": 1, "llm": 2}


def _order_sources(sources: list[str]) -> list[str]:
    return sorted(dict.fromkeys(sources), key=lambda s: _SOURCE_ORDER.get(s, 99))


async def _plan_queries(query: str) -> list[tuple[str, str]]:
    """构造最终 query 计划：[(query, source)]，source ∈ {original, rule, llm}。

    顺序与配额（任务 §1-§4）：
      1. 原始 query 永远第一位、永远保留；
      2. 规则 query（normalize/synonym）先加入，但当 LLM 会产出时**预留 1 个 slot 给 LLM**
         （rule 阶段最多填到 cap-1，保证至少 1 条 LLM query 能进最终 plan）；
      3. 加入 LLM query（填到 cap 上限）；
      4. 若 LLM 没用满预留（或规则尚有余量），再用剩余规则 query 回填，避免浪费 slot；
      5. 全程去重，总数不超过 QUERY_REWRITE_MAX_QUERIES。
    LLM 仅在开关开启且配置齐全时调用；失败/超时/非法 JSON → generate() 返回 [] → 不预留 slot，
    规则 query 正常填满上限（自动退回纯规则 rewrite）。
    """
    cap = max(1, settings.query_rewrite_max_queries)
    plan: list[tuple[str, str]] = []

    def _add(q: str, source: str) -> None:
        if q and len(plan) < cap and all(q != p for p, _ in plan):
            plan.append((q, source))

    # 规则 rewrite：expand_query[0] 恒为原始 query，其余为 normalize/synonym（统一记为 rule）
    rule = query_rewrite.expand_query(
        query, query_rewrite.load_synonyms(), max_queries=cap,
    )
    original = rule[0] if rule else query
    rule_expansions = list(rule[1:])

    # LLM rewrite（可选）：失败/超时/非法 JSON → 返回 [] → reserve=0，等价于不启用
    llm_qs: list[str] = []
    if settings.query_rewrite_llm_enabled and llm_query_rewrite.is_configured():
        llm_qs = await llm_query_rewrite.generate(
            query,
            base_url=settings.query_rewrite_llm_base_url,
            model=settings.query_rewrite_llm_model,
            api_key=settings.query_rewrite_llm_api_key,
            timeout=settings.query_rewrite_llm_timeout_seconds,
            max_queries=settings.query_rewrite_llm_max_queries,
        )

    _add(original, "original")                    # §1 原始永远第一位

    # §2/§3 规则先加入，但 LLM 有产出时预留 1 个 slot（rule 阶段封顶 cap-reserve）
    reserve = 1 if llm_qs else 0
    rule_budget = cap - reserve
    for q in rule_expansions:
        if len(plan) >= rule_budget:
            break
        _add(q, "rule")

    for q in llm_qs:                              # §3 加入 LLM，填到上限
        if len(plan) >= cap:
            break
        _add(q, "llm")

    for q in rule_expansions:                     # §4 还有空位 → 回填剩余规则 query（去重，不重复）
        if len(plan) >= cap:
            break
        _add(q, "rule")

    return plan


async def _multi_query_recall(
    db: AsyncSession | None, library, collection: str,
    plan: list[tuple[str, str]], vectors: list[list[float]], *,
    needed: int, payload_filter: dict | None, exact_vector_search: bool = False,
) -> list[dict]:
    """多 query 召回：每个 query 向量各自走 _recall_visible（含可见性过滤），再按 chunk_id 合并去重。

    合并规则：
      - item["score"]（vector_score）取命中该 chunk 的各 query 中的**最高分**（保持 0~1 dense 语义，
        供 threshold/返回用）；
      - item["_fuse_score"] = 各命中 query 的分数之和（CombSUM）：同一 chunk 被多个 query 命中 →
        融合分更高 → 排序更靠前（任务 §9「多次命中可提升排序」）；
      - item["_matched_queries"]：命中该 chunk 的 query 文本（首次出现序、去重）；
      - item["_rewrite_sources"]：命中来源集合（original/rule/llm，按固定优先级）。
    可见性过滤在每路 _recall_visible 内部完成，合并不改变其语义。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for (q, source), vec in zip(plan, vectors):
        hits = await _recall_visible(
            db, library, collection, vec, needed=needed, payload_filter=payload_filter,
            exact_vector_search=exact_vector_search,
        )
        for hit in hits:
            key = _hit_chunk_id(hit)
            score = float(hit.get("score") or 0.0)
            cur = merged.get(key)
            if cur is None:
                item = dict(hit)
                item["score"] = score
                item["_fuse_score"] = score
                item["_matched_queries"] = [q]
                item["_rewrite_sources"] = [source]
                merged[key] = item
                order.append(key)
            else:
                if q not in cur["_matched_queries"]:
                    cur["_matched_queries"].append(q)
                if source not in cur["_rewrite_sources"]:
                    cur["_rewrite_sources"].append(source)
                cur["_fuse_score"] += score                       # 多命中加成
                if score > float(cur.get("score") or 0.0):
                    cur["score"] = score                          # vector_score 取最高
    fused = [merged[k] for k in order]
    fused.sort(key=lambda it: (-it["_fuse_score"], _hit_chunk_id(it)))
    for it in fused:
        it["_rewrite_sources"] = _order_sources(it["_rewrite_sources"])
    return fused


def _rrf_fuse(dense_hits: list[dict], keyword_hits: list[dict], *, k: int) -> list[dict]:
    """RRF 融合 dense + keyword 两路（按 chunk_id 并集）。

    rrf_score = Σ 1/(k + rank)，rank 从 1 起（两路各自名次）。返回 raw item，字段与 dense 对齐
    并附 _vector_score / _rrf_score / _dense_rank / _keyword_rank。payload 优先取 dense 一路
    （其 document_revision 是 embed 时快照，与 Qdrant 一致）；keyword-only 命中用 keyword payload。
    稳定排序：rrf 降序，同分按插入序（dense 先入）→ dense 占先。
    """
    agg: dict[str, dict] = {}
    order: list[str] = []

    def _slot(key: str) -> dict:
        if key not in agg:
            agg[key] = {"payload": None, "rrf": 0.0, "vector": None,
                        "dense_rank": None, "keyword_rank": None}
            order.append(key)
        return agg[key]

    dense_hits = sorted(
        dense_hits,
        key=lambda hit: (-float(hit.get("score") or 0.0), _hit_chunk_id(hit)),
    )
    keyword_hits = sorted(
        keyword_hits,
        key=lambda hit: (-float(hit.get("score") or 0.0), _hit_chunk_id(hit)),
    )

    for rank, hit in enumerate(dense_hits, start=1):
        e = _slot(_hit_chunk_id(hit))
        e["dense_rank"] = rank
        e["vector"] = float(hit.get("score") or 0.0)
        e["rrf"] += 1.0 / (k + rank)
        e["payload"] = hit.get("payload") or {}            # dense payload 优先
    for rank, hit in enumerate(keyword_hits, start=1):
        e = _slot(_hit_chunk_id(hit))
        e["keyword_rank"] = rank
        e["rrf"] += 1.0 / (k + rank)
        if e["payload"] is None:                           # 仅 keyword 命中 → 用 keyword payload
            e["payload"] = hit.get("payload") or {}

    fused = [
        {
            "id": key,
            "score": agg[key]["rrf"],
            "payload": agg[key]["payload"] or {},
            "_vector_score": agg[key]["vector"],
            "_rrf_score": agg[key]["rrf"],
            "_dense_rank": agg[key]["dense_rank"],
            "_keyword_rank": agg[key]["keyword_rank"],
        }
        for key in order
    ]
    fused.sort(
        key=lambda item: (
            -item["_rrf_score"],
            item["_dense_rank"] if item["_dense_rank"] is not None else float("inf"),
            item["_keyword_rank"] if item["_keyword_rank"] is not None else float("inf"),
            _hit_chunk_id(item),
        )
    )
    return fused


def _hybrid_evidence(raw: list[dict]) -> dict[str, Any]:
    """Classify hybrid evidence from raw dense scores, never RRF scores."""
    dense_scores = [
        float(item["_vector_score"])
        for item in raw
        if item.get("_vector_score") is not None
    ]
    max_dense = max(dense_scores, default=None)
    qualified_count = sum(
        score >= settings.hybrid_min_dense_score for score in dense_scores
    )
    if not raw:
        status, reason = "insufficient", "no_candidates"
    elif dense_scores and max_dense < settings.hybrid_min_dense_score:
        status, reason = "insufficient", "dense_score_below_minimum"
    elif dense_scores:
        status, reason = "sufficient", "dense_score"
    else:
        status, reason = "sufficient", "keyword_only_evidence"
    return {
        "status": status,
        "policy": "hybrid_dense_minimum",
        "dense_minimum": settings.hybrid_min_dense_score,
        "max_dense_score": max_dense,
        "qualified_count": qualified_count,
        "reason": reason,
    }


def _rerank_evidence(scores: dict[int, float]) -> dict[str, Any]:
    """Classify a successful rerank response using its native score scale."""
    max_score = max(scores.values(), default=None)
    sufficient = max_score is not None and max_score >= settings.rerank_min_score
    return {
        "status": "sufficient" if sufficient else "insufficient",
        "policy": "rerank_minimum",
        "rerank_minimum": settings.rerank_min_score,
        "max_rerank_score": max_score,
        "qualified_count": sum(
            score >= settings.rerank_min_score for score in scores.values()
        ),
        "reason": "rerank_score" if sufficient else "rerank_score_below_minimum",
    }


async def _hybrid_recall(
    db: AsyncSession, library, collection: str, vector, query: str, *,
    needed: int, qdrant_filter: dict | None, exact_vector_search: bool = False,
) -> list[dict]:
    """hybrid 召回：dense(Qdrant) + keyword(pg_trgm) → RRF 融合 → 一次性可见性过滤（任务 §4）。

    metadata_condition（qdrant_filter）只能下推到 dense 一路；存在 filter 时把 keyword 命中
    限定在 dense 候选内，避免引入未过滤候选（keyword 仅起重排/加权，不破坏过滤语义）。
    """
    cand_k = max(settings.hybrid_candidate_k, needed)
    dense_hits = await qdrant.search(
        collection, vector, limit=cand_k, payload_filter=qdrant_filter, with_payload=True,
        exact=exact_vector_search,
    )
    keyword_hits = await keyword_search.recall(db, library, query, limit=cand_k)
    if qdrant_filter is not None and keyword_hits:
        dense_ids = {_hit_chunk_id(h) for h in dense_hits}
        keyword_hits = [h for h in keyword_hits if _hit_chunk_id(h) in dense_ids]

    fused = _rrf_fuse(dense_hits, keyword_hits, k=settings.hybrid_rrf_k)
    mask = await visibility.compute_visible_mask(db, library, [it["payload"] for it in fused])
    visible = [it for it, ok in zip(fused, mask) if ok]
    return visible[:needed]


async def run_retrieval(
    *,
    collection: str,
    embedding_model: str,
    embedding_base_url: str | None,
    request: DifyRetrievalRequest,
    source_config: dict[str, Any] | None = None,
    rerank_enabled: bool | None = None,
    retrieval_mode: str = "dense",
    db: AsyncSession | None = None,
    library=None,
    candidate_k: int | None = None,
    exact_vector_search: bool = False,
) -> DifyRetrievalResponse:
    top_k = request.retrieval_setting.top_k
    threshold = request.retrieval_setting.score_threshold or 0.0   # 0 = 不额外过滤（#11）
    # rerank 生效：库级覆盖优先，否则全局，且必须配好了 reranker 地址
    requested_rerank = bool(
        rerank_enabled if rerank_enabled is not None else settings.rerank_enabled
    )
    rerank_configured = rerank_svc.is_configured()
    eff_rerank = requested_rerank and rerank_configured
    rerank_disabled_reason = (
        "disabled" if not requested_rerank else "not_configured"
    )
    recall_limit = max(settings.rerank_candidate_k, top_k) if eff_rerank else top_k
    if candidate_k is not None:
        if candidate_k < 1:
            raise ValueError("candidate_k must be positive")
        if candidate_k > settings.visibility_overfetch_max:
            raise ValueError("candidate_k exceeds visibility overfetch bound")
        recall_limit = max(recall_limit, candidate_k)

    qdrant_filter = _build_qdrant_filter(request.metadata_condition)
    # #11：**不在 Qdrant 召回阶段用 score_threshold 提前过滤**（rerank 模式下会把候选按 vector_score
    # 提前裁掉，threshold 语义应作用于最终分）。召回只做可见性过滤。
    # hybrid 需库级生命周期上下文（可见性 + keyword 查 PG）；缺 db/library 时退化为 dense。
    hybrid = retrieval_mode == "hybrid" and db is not None and library is not None
    # Query Rewrite 激活条件：规则开关开，或 LLM 改写开关开且配置齐全。dense 模式下生效。
    qr_enabled = settings.query_rewrite_enabled or (
        settings.query_rewrite_llm_enabled and llm_query_rewrite.is_configured()
    )
    if hybrid:
        # Hybrid（第一版）：单 query dense + keyword RRF 融合。Query Rewrite 多 query 仅在
        # dense 模式生效，hybrid 第一版不与之叠加（保持简单、可解释）。
        vector = await embedding.embed_one(
            request.query, model=embedding_model, base_url=embedding_base_url,
        )
        raw = await _hybrid_recall(
            db, library, collection, vector, request.query,
            needed=recall_limit, qdrant_filter=qdrant_filter,
            exact_vector_search=exact_vector_search,
        )
    elif qr_enabled:
        # Query Rewrite：规则(normalize+synonym) + 可选 LLM 改写 → 合并去重的多 query →
        # 分别 embedding → 多路召回按 chunk_id 合并。后续 visibility/enrich/rerank/threshold 流程不变。
        plan = await _plan_queries(request.query)
        queries = [q for q, _ in plan]
        vectors = await embedding.embed_texts(
            queries, model=embedding_model, base_url=embedding_base_url,
        )
        raw = await _multi_query_recall(
            db, library, collection, plan, vectors,
            needed=recall_limit, payload_filter=qdrant_filter,
            exact_vector_search=exact_vector_search,
        )
    else:
        # 旧逻辑完全不变：单 query embed + 单路召回。
        vector = await embedding.embed_one(
            request.query, model=embedding_model, base_url=embedding_base_url,
        )
        raw = await _recall_visible(
            db, library, collection, vector, needed=recall_limit, payload_filter=qdrant_filter,
            exact_vector_search=exact_vector_search,
        )

    payloads = [(item.get("payload") or {}) for item in raw]

    # 源库补全：payload 只有外键时，回查源库把正文拼回（未配置则 texts/rows 全 None）
    enr = await source_enrichment.enrich_payloads(source_config, payloads)

    internal_keys = {
        "text", "title", "library_id", "document_id", "chunk_id", "seq",
        "evidence_locator_v1", "evidence_locator_v1_projection",
    }
    extra_columns = enr.parsed["extra_columns"] if enr.enabled else []

    # 最终展示内容（源库补全优先），同时作为 rerank 的输入
    contents = [
        (enr.texts[i] if enr.texts[i] is not None else (payloads[i].get("text") or ""))
        for i in range(len(raw))
    ]

    # #11：rerank 对**全部召回候选**打分（top_k=recall_limit）→ 之后才按 final_score 过滤+截断；
    # 失败/未启用回退向量序（绝不阻断检索）。
    ranked_candidates = await rerank_svc.rank_candidates(
        request.query,
        contents,
        top_k=recall_limit,
        enabled=bool(eff_rerank),
        disabled_reason=rerank_disabled_reason,
    )
    order, rerank_scores = ranked_candidates
    rerank_observation = getattr(ranked_candidates, "observation", None)
    if rerank_observation is None:
        # Keep compatibility with older tuple-returning test doubles/extensions.
        rerank_observation = rerank_svc.RerankObservation(
            "success" if eff_rerank and rerank_scores else (
                "disabled" if not eff_rerank else "fallback"
            ),
            (settings.rerank_provider or "standard").lower(),
            len(contents),
            len(rerank_scores),
            None if eff_rerank and rerank_scores else rerank_disabled_reason,
        )

    if rerank_observation.effective == "success":
        evidence_debug = _rerank_evidence(rerank_scores)
    elif hybrid:
        evidence_debug = _hybrid_evidence(raw)
    else:
        evidence_debug = None
    # 先按 final_score 过滤 threshold，再截取 top_k（#11 点 4）。order 已按相关性降序。
    records: list[DifyRecord] = []
    seen_content: dict[str, DifyRecord] = {}
    duplicate_count = 0
    candidate_order = (
        []
        if evidence_debug is not None and evidence_debug["status"] == "insufficient"
        else order
    )
    for i in candidate_order:
        item, payload, enriched, src_row = raw[i], payloads[i], enr.texts[i], enr.rows[i]
        if hybrid:
            # hybrid：vector_score 可能为 None（keyword-only 命中）；base = rrf_score
            vector_score = item.get("_vector_score")
            base_score = item.get("_rrf_score") or 0.0
        else:
            vector_score = float(item.get("score") or 0.0)
            base_score = vector_score
        rr_score = rerank_scores.get(i)
        # final_score：有 rerank_score 用之（rerank 成功命中），否则 base（dense=vector / hybrid=rrf）
        final_score = rr_score if rr_score is not None else base_score
        # threshold（Dify score_threshold，0~1 相似度语义）只在分数可比时套用：
        # dense 的 vector/rerank 分、hybrid 的 rerank 分都是 0~1；hybrid 未重排时 final=rrf（量级~1/k，
        # 与 0~1 阈值不可比）→ 跳过 threshold，避免一刀切清空（任务 §8：不破坏 threshold 语义）。
        threshold_applies = (not hybrid) or (rr_score is not None)
        if threshold > 0 and threshold_applies and final_score < threshold:
            continue
        content = enriched if enriched is not None else (payload.get("text") or "")
        content_key = _content_hash(content)
        if content_key is not None and content_key in seen_content:
            _add_duplicate_source(seen_content[content_key], payload)
            duplicate_count += 1
            continue
        title = payload.get("title") or ""
        metadata = {k: v for k, v in payload.items() if k not in internal_keys}
        metadata.setdefault("document_id", payload.get("document_id"))
        metadata.setdefault("chunk_id", payload.get("chunk_id"))
        metadata.setdefault("seq", payload.get("seq"))
        locator_projection = (
            validate_projection(
                payload.get("evidence_locator_v1_projection"),
                expected_identity={
                    "document_id": payload.get("document_id"),
                    "document_revision_id": payload.get("document_revision_id"),
                    "document_revision": payload.get("document_revision"),
                    "document_revision_no": payload.get("document_revision_no"),
                    "chunk_id": payload.get("chunk_id"),
                },
            )
            if settings.enable_evidence_locator_read
            else None
        )
        if locator_projection is not None:
            metadata["evidence_locator_v1_projection"] = locator_projection
        if src_row:
            for col in extra_columns:
                metadata.setdefault(col, src_row.get(col))
        # 分数留痕：vector_score（dense 恒有；hybrid 仅 dense 命中项有）；rerank_score 仅重排命中时有
        if vector_score is not None:
            metadata["vector_score"] = vector_score
        if rr_score is not None:
            metadata["rerank_score"] = rr_score
        if qr_enabled and not hybrid:
            # Query Rewrite 留痕：命中该 chunk 的 query 文本 + 命中来源（original/rule/llm）
            metadata["matched_queries"] = item.get("_matched_queries") or []
            metadata["rewrite_source"] = item.get("_rewrite_sources") or []
        if hybrid:
            # Hybrid 融合留痕（普通用户可不显示，debug/日志可用）
            metadata["retrieval_mode"] = "hybrid"
            metadata["dense_rank"] = item.get("_dense_rank")
            metadata["keyword_rank"] = item.get("_keyword_rank")
            metadata["rrf_score"] = item.get("_rrf_score")
        metadata["rerank_effective"] = rerank_observation.effective
        metadata["rerank_provider"] = rerank_observation.provider
        metadata["rerank_candidate_count"] = rerank_observation.candidate_count
        metadata["rerank_scored_count"] = rerank_observation.scored_count
        if rerank_observation.fallback_reason:
            metadata["rerank_fallback_reason"] = rerank_observation.fallback_reason
        record = DifyRecord(content=content or "", score=final_score, title=title, metadata=metadata)
        if content_key is not None:
            seen_content[content_key] = record
        records.append(record)
        if len(records) >= top_k:
            break

    retrieval_debug = {
        "rerank": {
            "effective": rerank_observation.effective,
            "provider": rerank_observation.provider,
            "candidate_count": rerank_observation.candidate_count,
            "scored_count": rerank_observation.scored_count,
            "fallback_reason": rerank_observation.fallback_reason,
        },
        "duplicate_suppressed": duplicate_count,
    }
    if evidence_debug is not None:
        retrieval_debug["evidence"] = evidence_debug
    log.info("retrieval: collection=%s mode=%s qr=%s top_k=%s thr=%s recalled=%s returned=%s rerank=%s enriched=%s",
             collection, "hybrid" if hybrid else "dense", "yes" if (qr_enabled and not hybrid) else "no",
             top_k, threshold, len(raw), len(records),
             "yes" if eff_rerank else "no", "yes" if enr.enabled else "no")
    return DifyRetrievalResponse(records=records, retrieval_debug=retrieval_debug)
