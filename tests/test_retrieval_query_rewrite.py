"""Query Rewrite（规则 + 可选 LLM）多 query dense 召回的集成行为（直接驱动 run_retrieval）。

覆盖任务验收点：
  - LLM/规则 都关时 dense 路径完全不变（embed_one + 单路召回，不调 embed_texts）
  - 开启后多 query 分别 embedding + 召回；按 chunk_id 合并去重
  - 同一 chunk 被多个 query 命中 → matched_queries 多条、多命中提升排序
  - rewrite_source 记录 original / rule / llm
  - LLM 开启时 query 扩展生效；LLM 失败(返回空)退回规则 rewrite，原始 query 永远保留
  - visibility 过滤仍生效；rerank 仍在合并后执行
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.schemas.dify import DifyRetrievalRequest
from app.services import retrieval as R


def _req(top_k=5, threshold=0.0, query="社保怎么交"):
    return DifyRetrievalRequest(
        knowledge_id="k", query=query,
        retrieval_setting={"top_k": top_k, "score_threshold": threshold},
    )


def _hit(pid, score, text):
    return {"id": pid, "score": score,
            "payload": {"document_id": pid, "chunk_id": pid, "text": text, "title": "t"}}


class _Lib:
    lifecycle_mode = "managed"


def _run(request, *, enabled, rule_queries=None, search_side_effect, rerank=False,
         rank_return=None, db=None, library=None, visible_mask=None,
         llm_enabled=False, llm_queries=None):
    """驱动 run_retrieval。

    enabled       → 规则 Query Rewrite 开关。
    rule_queries  → patch query_rewrite.expand_query 的返回（[原始, 规则...]）。
    llm_enabled   → LLM 改写开关；llm_queries → patch llm_query_rewrite.generate 的返回。
    """
    embed_texts = AsyncMock(side_effect=lambda texts, **kw: [[0.1] * 8 for _ in texts])
    embed_one = AsyncMock(return_value=[0.1] * 8)
    msearch = AsyncMock(side_effect=search_side_effect)
    gen = AsyncMock(return_value=list(llm_queries or []))

    stack = [
        patch.object(R.settings, "query_rewrite_enabled", enabled),
        patch.object(R.settings, "query_rewrite_max_queries", 4),
        patch.object(R.query_rewrite, "load_synonyms", return_value={}),
        patch.object(R.embedding, "embed_texts", embed_texts),
        patch.object(R.embedding, "embed_one", embed_one),
        patch.object(R.qdrant, "search", msearch),
        patch.object(R.rerank_svc, "is_configured", return_value=rerank),
    ]
    if rule_queries is not None:
        stack.append(patch.object(R.query_rewrite, "expand_query", return_value=rule_queries))
    if llm_enabled:
        stack += [
            patch.object(R.settings, "query_rewrite_llm_enabled", True),
            patch.object(R.llm_query_rewrite, "is_configured", return_value=True),
            patch.object(R.llm_query_rewrite, "generate", gen),
        ]
    if rank_return is not None:
        rank = AsyncMock(return_value=rank_return)
        stack.append(patch.object(R.rerank_svc, "rank_candidates", rank))
    else:
        rank = None
        stack.append(patch.object(R.rerank_svc, "rerank", new_callable=AsyncMock, return_value=[]))
    if visible_mask is not None:
        stack.append(patch.object(R.visibility, "compute_visible_mask",
                                  new_callable=AsyncMock, return_value=visible_mask))

    for cm in stack:
        cm.start()
    try:
        resp = asyncio.run(R.run_retrieval(
            collection="c", embedding_model="m", embedding_base_url=None,
            request=request, source_config=None, rerank_enabled=rerank,
            db=db, library=library,
        ))
    finally:
        for cm in reversed(stack):
            cm.stop()
    return resp, embed_one, embed_texts, msearch, rank, gen


def test_disabled_keeps_old_dense_path():
    # 规则 + LLM 都关：走 embed_one + 单路召回；embed_texts 一次都不调，无 rewrite 留痕
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.5, "B")]
    resp, embed_one, embed_texts, msearch, _, _ = _run(
        _req(), enabled=False, search_side_effect=lambda *a, **k: hits,
    )
    assert embed_one.await_count == 1
    assert embed_texts.await_count == 0
    assert [r.metadata["document_id"] for r in resp.records] == ["a", "b"]
    assert all("matched_queries" not in r.metadata for r in resp.records)
    assert all("rewrite_source" not in r.metadata for r in resp.records)


def test_enabled_multi_query_embeds_each_query():
    returns = iter([[_hit("a", 0.9, "A")], [_hit("c", 0.8, "C")]])
    resp, embed_one, embed_texts, msearch, _, _ = _run(
        _req(), enabled=True, rule_queries=["q1", "q2"],
        search_side_effect=lambda *a, **k: next(returns),
    )
    assert embed_one.await_count == 0            # 不再走单 query 路径
    assert embed_texts.await_count == 1          # 一次性 batch embed
    assert embed_texts.await_args.args[0] == ["q1", "q2"]
    assert msearch.await_count == 2              # 每个 query 各召回一次（db=None 单轮）
    # q1=原始 → original；q2=规则扩展 → rule
    by_id = {r.metadata["document_id"]: r for r in resp.records}
    assert by_id["a"].metadata["rewrite_source"] == ["original"]
    assert by_id["c"].metadata["rewrite_source"] == ["rule"]


def test_dedup_by_chunk_id_and_matched_queries():
    # A 被两个 query 命中（0.9 / 0.8）；B 仅 q1；C 仅 q2
    returns = iter([
        [_hit("a", 0.9, "A"), _hit("b", 0.5, "B")],
        [_hit("a", 0.8, "A"), _hit("c", 0.6, "C")],
    ])
    resp, *_ = _run(
        _req(), enabled=True, rule_queries=["q1", "q2"],
        search_side_effect=lambda *a, **k: next(returns),
    )
    ids = [r.metadata["document_id"] for r in resp.records]
    # 去重后按融合分（多命中加成）降序：a=0.9+0.8=1.7 > c=0.6 > b=0.5
    assert ids == ["a", "c", "b"]
    by_id = {r.metadata["document_id"]: r for r in resp.records}
    assert by_id["a"].metadata["matched_queries"] == ["q1", "q2"]   # 多 query 命中保留
    assert by_id["a"].metadata["vector_score"] == 0.9              # vector_score 取最高
    assert by_id["a"].metadata["rewrite_source"] == ["original", "rule"]
    assert by_id["b"].metadata["matched_queries"] == ["q1"]
    assert by_id["c"].metadata["matched_queries"] == ["q2"]


def test_multi_hit_boosts_ranking():
    # X 单次命中 0.7；Y 被两个 query 命中各 0.4（融合 0.8）→ Y 应排在 X 前
    returns = iter([
        [_hit("x", 0.7, "X"), _hit("y", 0.4, "Y")],
        [_hit("y", 0.4, "Y")],
    ])
    resp, *_ = _run(
        _req(), enabled=True, rule_queries=["q1", "q2"],
        search_side_effect=lambda *a, **k: next(returns),
    )
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["y", "x"]                      # 多命中提升排序
    by_id = {r.metadata["document_id"]: r for r in resp.records}
    assert by_id["y"].metadata["matched_queries"] == ["q1", "q2"]


def test_visibility_filter_still_applies():
    # 每路召回 [A, B]，可见性把 B 过滤 → 合并后只剩 A
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.5, "B")]
    resp, *_ = _run(
        _req(), enabled=True, rule_queries=["q1", "q2"],
        search_side_effect=lambda *a, **k: list(hits),
        db=object(), library=_Lib(), visible_mask=[True, False],
    )
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["a"]
    assert resp.records[0].metadata["matched_queries"] == ["q1", "q2"]


def test_rerank_runs_after_merge():
    # 合并候选 = [A(0.9), C(0.8), B(0.1)]；reranker 把 B 顶到第一
    returns = iter([
        [_hit("a", 0.9, "A"), _hit("b", 0.1, "B")],
        [_hit("c", 0.8, "C")],
    ])
    rank_return = ([2, 0, 1], {2: 0.95, 0: 0.6, 1: 0.3})   # 作用于合并后的 raw 索引
    resp, _eo, _et, _ms, rank, _ = _run(
        _req(top_k=5, threshold=0.0), enabled=True, rule_queries=["q1", "q2"],
        search_side_effect=lambda *a, **k: next(returns),
        rerank=True, rank_return=rank_return,
    )
    assert rank.await_count == 1
    assert len(rank.await_args.args[1]) == 3       # rerank 在合并后的 3 个候选上执行
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["b", "a", "c"]
    assert [r.score for r in resp.records] == [0.95, 0.6, 0.3]
    assert resp.records[0].metadata["matched_queries"] == ["q1"]
    assert "rewrite_source" in resp.records[0].metadata


# ── LLM Query Rewrite 集成 ─────────────────────────────────────────────────
def test_llm_enabled_expands_queries():
    # 规则只产出原始 query；LLM 追加一个改写 query，命中独有 chunk
    returns = iter([
        [_hit("a", 0.9, "A")],                     # 原始 query 命中 A
        [_hit("z", 0.7, "Z")],                     # LLM query 命中 Z
    ])
    resp, _eo, embed_texts, _ms, _rk, gen = _run(
        _req(query="社保咋交啊"), enabled=True, rule_queries=["社保咋交啊"],
        llm_enabled=True, llm_queries=["社会保险如何缴纳"],
        search_side_effect=lambda *a, **k: next(returns),
    )
    assert gen.await_count == 1
    # 原始在前、LLM query 在后，都参与 embedding
    assert embed_texts.await_args.args[0] == ["社保咋交啊", "社会保险如何缴纳"]
    by_id = {r.metadata["document_id"]: r for r in resp.records}
    assert by_id["a"].metadata["rewrite_source"] == ["original"]
    assert by_id["z"].metadata["rewrite_source"] == ["llm"]       # LLM 来源被记录
    assert by_id["z"].metadata["matched_queries"] == ["社会保险如何缴纳"]


def test_llm_failure_falls_back_to_rule_and_keeps_original():
    # LLM 返回空（超时/非法 JSON 在 generate 内部已兜底成 []）→ 退回规则 rewrite
    returns = iter([[_hit("a", 0.9, "A")]])
    resp, _eo, embed_texts, msearch, _rk, gen = _run(
        _req(query="原问题"), enabled=True, rule_queries=["原问题"],
        llm_enabled=True, llm_queries=[],          # 模拟 LLM 失败
        search_side_effect=lambda *a, **k: next(returns),
    )
    assert gen.await_count == 1
    assert embed_texts.await_args.args[0] == ["原问题"]            # 仅原始 query
    assert msearch.await_count == 1
    assert resp.records[0].metadata["rewrite_source"] == ["original"]   # 原始 query 永远保留


def test_llm_query_deduped_against_original():
    # LLM 把原始 query 也回显（任务 §4 要求数组首元素是原问题）→ 不应重复、不应改源
    returns = iter([[_hit("a", 0.9, "A")], [_hit("b", 0.6, "B")]])
    resp, _eo, embed_texts, _ms, _rk, _g = _run(
        _req(query="年假几天"), enabled=True, rule_queries=["年假几天"],
        llm_enabled=True, llm_queries=["年假几天", "带薪年休假多少天"],
        search_side_effect=lambda *a, **k: next(returns),
    )
    # 原始只出现一次（去重），LLM 新增的那条在后
    assert embed_texts.await_args.args[0] == ["年假几天", "带薪年休假多少天"]
    by_id = {r.metadata["document_id"]: r for r in resp.records}
    assert by_id["a"].metadata["rewrite_source"] == ["original"]


# ── _plan_queries 配额策略（LLM slot 预留）─────────────────────────────────
def _plan(query, *, cap=4, rule, llm_enabled=False, llm=None):
    """直接驱动 R._plan_queries：rule→patch expand_query，llm→patch generate。"""
    stack = [
        patch.object(R.settings, "query_rewrite_max_queries", cap),
        patch.object(R.query_rewrite, "load_synonyms", return_value={}),
        patch.object(R.query_rewrite, "expand_query", return_value=rule),
    ]
    if llm_enabled:
        stack += [
            patch.object(R.settings, "query_rewrite_llm_enabled", True),
            patch.object(R.llm_query_rewrite, "is_configured", return_value=True),
            patch.object(R.llm_query_rewrite, "generate", AsyncMock(return_value=list(llm or []))),
        ]
    for cm in stack:
        cm.start()
    try:
        return asyncio.run(R._plan_queries(query))
    finally:
        for cm in reversed(stack):
            cm.stop()


def test_llm_gets_reserved_slot_when_rule_queries_fill_cap():
    # 规则 query 足以填满 cap；LLM 开启且有产出 → 仍至少保留 1 条 LLM query
    plan = _plan(
        "Q", cap=4, rule=["Q", "r1", "r2", "r3", "r4"],
        llm_enabled=True, llm=["l1"],
    )
    assert len(plan) == 4                          # 不超过上限
    assert plan[0] == ("Q", "original")            # 原始第一位
    assert ("l1", "llm") in plan                   # LLM 拿到了预留 slot
    # 预留 1 个 slot 给 LLM → rule 阶段封顶 cap-1：original + 2 rule + 1 llm
    assert plan == [("Q", "original"), ("r1", "rule"), ("r2", "rule"), ("l1", "llm")]


def test_rule_queries_fill_cap_when_llm_returns_empty():
    # LLM 失败/返回空 → 不预留 slot，规则 query 正常填满上限
    plan = _plan(
        "Q", cap=4, rule=["Q", "r1", "r2", "r3", "r4"],
        llm_enabled=True, llm=[],
    )
    assert len(plan) == 4
    assert [s for _, s in plan] == ["original", "rule", "rule", "rule"]   # 无 llm
    assert plan == [("Q", "original"), ("r1", "rule"), ("r2", "rule"), ("r3", "rule")]


def test_original_always_first():
    # 各种组合下原始 query 恒为第一位
    p1 = _plan("Q", cap=4, rule=["Q", "r1", "r2"], llm_enabled=True, llm=["l1", "l2"])
    p2 = _plan("Q", cap=4, rule=["Q", "r1", "r2", "r3", "r4"], llm_enabled=False)
    p3 = _plan("Q", cap=2, rule=["Q", "r1"], llm_enabled=True, llm=["l1"])
    for p in (p1, p2, p3):
        assert p[0] == ("Q", "original")
    # cap=2 且 LLM 有产出：original 必留 + 预留给 LLM → 挤掉 rule
    assert p3 == [("Q", "original"), ("l1", "llm")]
