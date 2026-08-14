"""rerank 服务单测：解析/排序/截断/配置判定（纯单元，不连服务）。"""
from __future__ import annotations

import pytest

from app.services import rerank


def test_parse_sorts_desc_and_truncates():
    body = {"results": [
        {"index": 2, "relevance_score": 0.1},
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.5},
    ]}
    assert rerank._parse_results(body, top_n=2) == [(0, 0.9), (1, 0.5)]


def test_parse_ties_use_candidate_index():
    body = {"results": [
        {"index": 2, "relevance_score": 0.5},
        {"index": 0, "relevance_score": 0.5},
        {"index": 1, "relevance_score": 0.5},
    ]}
    assert rerank._parse_results(body, top_n=3) == [(0, 0.5), (1, 0.5), (2, 0.5)]


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


def test_parse_tei_bare_array_uses_score_and_deduplicates():
    body = [
        {"index": 1, "score": 0.72},
        {"index": 1, "score": 0.71},
        {"index": 0, "score": 0.86},
    ]
    assert rerank._parse_results(body, top_n=2, provider="tei", total=2) == [
        (0, 0.86),
        (1, 0.72),
    ]


def test_parse_tei_rejects_invalid_index_and_non_finite_score():
    with pytest.raises(rerank.RerankError, match="out of range"):
        rerank._parse_results(
            [{"index": 2, "score": 0.9}], top_n=2, provider="tei", total=2
        )
    with pytest.raises(rerank.RerankError, match="non-finite"):
        rerank._parse_results(
            [{"index": 0, "score": float("nan")}],
            top_n=2,
            provider="tei",
            total=2,
        )


async def test_tei_request_uses_texts_and_parses_bare_array(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return [{"index": 0, "score": 0.86}, {"index": 1, "score": 0.2}]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured.update(url=url, payload=json, headers=headers)
            return _Response()

    monkeypatch.setattr(rerank.httpx, "AsyncClient", _Client)
    result = await rerank.rerank(
        "q",
        ["a", "b"],
        top_n=2,
        model="BAAI/bge-reranker-v2-m3",
        base_url="http://10.0.10.2:8112/rerank",
        api_key="",
        provider="tei",
    )
    assert result == [(0, 0.86), (1, 0.2)]
    assert captured["payload"] == {
        "query": "q",
        "texts": ["a", "b"],
        "return_text": False,
        "raw_scores": False,
    }
    assert "documents" not in captured["payload"]
    assert captured["headers"] is None


async def test_tei_does_not_inherit_embedding_api_key(monkeypatch):
    from app.config import settings

    captured = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return [{"index": 0, "score": 0.86}]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(settings, "rerank_api_key", "")
    monkeypatch.setattr(settings, "embedding_api_key", "embedding-secret")
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _Client)
    await rerank.rerank("q", ["a"], top_n=1, provider="tei", base_url="http://tei/rerank")
    assert captured["headers"] is None


async def test_rank_candidates_falls_back_on_invalid_tei_result(monkeypatch):
    async def invalid_rerank(*args, **kwargs):
        raise rerank.RerankError("index out of range")

    monkeypatch.setattr(rerank, "rerank", invalid_rerank)
    order, scores = await rerank.rank_candidates(
        "q", ["a", "b"], top_k=2, enabled=True
    )
    assert order == [0, 1]
    assert scores == {}


async def test_rank_candidates_splits_large_pool_and_merges_global_order(monkeypatch):
    calls = []

    async def fake_rerank(query, docs, *, top_n):
        calls.append(list(docs))
        return [(index, 0.1 + int(doc.rsplit("-", 1)[1]) / 1000) for index, doc in enumerate(docs)]

    monkeypatch.setattr(rerank, "rerank", fake_rerank)
    result = await rerank.rank_candidates(
        "q", [f"doc-{index}" for index in range(50)], top_k=5, enabled=True
    )
    order, scores = result

    assert [len(batch) for batch in calls] == [32, 18]
    assert order == [49, 48, 47, 46, 45]
    assert scores[49] == 0.14900000000000002
    assert result.observation.effective == "success"
    assert result.observation.provider == rerank.settings.rerank_provider
    assert result.observation.candidate_count == 50
    assert result.observation.scored_count == 50
    assert result.observation.fallback_reason is None
