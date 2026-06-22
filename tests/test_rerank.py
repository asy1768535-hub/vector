"""rerank 服务单测：解析/排序/截断/配置判定（纯单元，不连服务）。"""
from __future__ import annotations

from app.services import rerank


def test_parse_sorts_desc_and_truncates():
    body = {"results": [
        {"index": 2, "relevance_score": 0.1},
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.5},
    ]}
    assert rerank._parse_results(body, top_n=2) == [(0, 0.9), (1, 0.5)]


def test_parse_skips_missing_index():
    body = {"results": [{"relevance_score": 0.9}, {"index": 3, "relevance_score": 0.2}]}
    assert rerank._parse_results(body, top_n=5) == [(3, 0.2)]


def test_parse_missing_score_defaults_zero():
    assert rerank._parse_results({"results": [{"index": 0}]}, top_n=5) == [(0, 0.0)]


def test_parse_empty():
    assert rerank._parse_results({}, top_n=5) == []
    assert rerank._parse_results({"results": []}, top_n=5) == []


def test_is_configured(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rerank_base_url", "")
    monkeypatch.setattr(settings, "rerank_model", "")
    assert rerank.is_configured() is False
    monkeypatch.setattr(settings, "rerank_base_url", "http://x/rerank")
    monkeypatch.setattr(settings, "rerank_model", "bge-reranker-v2-m3")
    assert rerank.is_configured() is True


async def test_rerank_empty_docs_no_http():
    # 空候选直接返回，不发 HTTP（无需 reranker 服务）
    assert await rerank.rerank("q", [], top_n=5) == []


def test_fill_order_backfills_missing_in_vector_order():
    # reranker 只返回部分候选 → 余下下标按原向量序补满（不丢召回）
    assert rerank.fill_order([2, 0], total=5) == [2, 0, 1, 3, 4]


def test_fill_order_full_is_noop():
    assert rerank.fill_order([1, 0, 2], total=3) == [1, 0, 2]


def test_fill_order_empty_ranked_is_vector_order():
    assert rerank.fill_order([], total=3) == [0, 1, 2]


def test_fill_order_drops_out_of_range():
    # 外部 reranker 异常返回越界下标（999 / 负数）→ 丢弃，避免 raw[i] 越界
    assert rerank.fill_order([999, 1, -1], total=3) == [1, 0, 2]


def test_fill_order_dedups_keeping_first():
    # 重复下标 → 只保留首次出现，避免重复返回同一条
    assert rerank.fill_order([2, 2, 0], total=3) == [2, 0, 1]


def test_parse_dashscope_output_results():
    # DashScope 原生包装：结果在 output.results（单项结构与标准一致）
    body = {"output": {"results": [
        {"index": 2, "relevance_score": 0.3},
        {"index": 0, "relevance_score": 0.9},
    ]}}
    assert rerank._parse_results(body, top_n=5) == [(0, 0.9), (2, 0.3)]


def test_parse_dedups_before_truncate():
    # 重复 index 不应占用 top_n 名额：降序去重保留最高分，再截断，留出位置给不同文档
    body = {"results": [
        {"index": 2, "relevance_score": 0.90},
        {"index": 2, "relevance_score": 0.85},
        {"index": 0, "relevance_score": 0.80},
        {"index": 1, "relevance_score": 0.70},
    ]}
    assert rerank._parse_results(body, top_n=2) == [(2, 0.90), (0, 0.80)]


async def test_rank_candidates_dedup_keeps_first_score(monkeypatch):
    # reranker 异常返回重复 index：order 与 scores 同口径取首次（最高分），不取末项低分
    async def fake_rerank(query, docs, *, top_n):
        return [(2, 0.90), (2, 0.30), (0, 0.50)]

    monkeypatch.setattr(rerank, "rerank", fake_rerank)
    order, scores = await rerank.rank_candidates("q", ["a", "b", "c"], top_k=3, enabled=True)
    assert order == [2, 0, 1]          # 补满 + 去重，保留首次出现
    assert scores[2] == 0.90           # 首次（最高分），不是被末项 0.30 覆盖
    assert scores[0] == 0.50
    assert 1 not in scores             # 补满项无 rerank 分


async def test_rank_candidates_disabled_is_vector_order(monkeypatch):
    # enabled=False：不发请求、原向量序、空分数
    async def boom(*a, **k):  # 不应被调用
        raise AssertionError("rerank should not be called when disabled")

    monkeypatch.setattr(rerank, "rerank", boom)
    order, scores = await rerank.rank_candidates("q", ["a", "b"], top_k=5, enabled=False)
    assert order == [0, 1]
    assert scores == {}
