"""#11 score_threshold 契约：dense 按 vector_score、rerank 按 rerank_score、失败 fallback、阈值后可少于 top_k。

直接调 run_retrieval（db=None/library=None → 跳过可见性过滤，只验阈值/评分语义）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.schemas.dify import DifyRetrievalRequest
from app.services import retrieval as R


def _req(top_k=5, threshold=0.0):
    return DifyRetrievalRequest(
        knowledge_id="k", query="q",
        retrieval_setting={"top_k": top_k, "score_threshold": threshold},
    )


def _hit(pid, score, text):
    return {"id": pid, "score": score,
            "payload": {"document_id": pid, "chunk_id": pid, "text": text, "title": "t"}}


def _run(request, *, rerank_enabled, search_return, rank_return=None, rerank_raises=False):
    with patch.object(R.embedding, "embed_one", new_callable=AsyncMock, return_value=[0.1] * 8), \
         patch.object(R.qdrant, "search", new_callable=AsyncMock, return_value=search_return) as msearch, \
         patch.object(R.rerank_svc, "is_configured", return_value=rerank_enabled):
        if rank_return is not None:
            cm = patch.object(R.rerank_svc, "rank_candidates", new_callable=AsyncMock, return_value=rank_return)
        elif rerank_raises:
            cm = patch.object(R.rerank_svc, "rerank", new_callable=AsyncMock, side_effect=RuntimeError("boom"))
        else:
            cm = patch.object(R.rerank_svc, "rerank", new_callable=AsyncMock, return_value=[])
        with cm:
            resp = asyncio.run(R.run_retrieval(
                collection="c", embedding_model="m", embedding_base_url=None,
                request=request, source_config=None, rerank_enabled=rerank_enabled,
                db=None, library=None,
            ))
    return resp, msearch


def test_dense_filters_by_vector_score():
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.5, "B"), _hit("c", 0.2, "C")]
    resp, msearch = _run(_req(top_k=5, threshold=0.4), rerank_enabled=False, search_return=hits)
    scores = [r.score for r in resp.records]
    assert scores == [0.9, 0.5]                       # 0.2 被阈值过滤
    assert all(r.metadata["vector_score"] == r.score for r in resp.records)  # similarity == vector_score
    # 召回阶段不得传 score_threshold（dense 也统一在应用层过滤）
    assert msearch.await_args.kwargs.get("score_threshold") is None


def test_rerank_filters_by_rerank_score_no_early_qdrant_filter():
    # 向量分：低分项 c=0.1（若在召回阶段按 vector 提前过滤会被裁掉）
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.8, "B"), _hit("c", 0.1, "C")]
    # reranker 把 c 排到最高：order=[2,0,1]，rerank_scores={2:0.95, 0:0.6, 1:0.3}
    rank_return = ([2, 0, 1], {2: 0.95, 0: 0.6, 1: 0.3})
    resp, msearch = _run(_req(top_k=5, threshold=0.5), rerank_enabled=True,
                         search_return=hits, rank_return=rank_return)
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["c", "a"]                          # 0.95, 0.6 过阈；b=0.3 被过滤
    assert [r.score for r in resp.records] == [0.95, 0.6]   # similarity == rerank_score
    assert resp.records[0].metadata["vector_score"] == 0.1  # 低 vector 的 c 仍返回 → 未在召回阶段提前过滤
    assert resp.records[0].metadata["rerank_score"] == 0.95
    assert msearch.await_args.kwargs.get("score_threshold") is None


def test_rerank_failure_falls_back_to_vector_score():
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.5, "B"), _hit("c", 0.2, "C")]
    # rerank 调用抛错 → rank_candidates 内部回退向量序、空分数 → 按 vector_score 过滤
    resp, _ = _run(_req(top_k=5, threshold=0.4), rerank_enabled=True,
                   search_return=hits, rerank_raises=True)
    assert [r.score for r in resp.records] == [0.9, 0.5]    # vector_score 排序/过滤
    assert all("rerank_score" not in r.metadata for r in resp.records)


def test_threshold_returns_fewer_than_top_k():
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.5, "B"), _hit("c", 0.2, "C")]
    resp, _ = _run(_req(top_k=5, threshold=0.6), rerank_enabled=False, search_return=hits)
    assert len(resp.records) == 1 and resp.records[0].score == 0.9   # 仅 1 条过阈，正常返回较少


def test_successful_rerank_returns_only_qualified_evidence():
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.8, "B")]
    resp, _ = _run(
        _req(top_k=5, threshold=0.0),
        rerank_enabled=True,
        search_return=hits,
        rank_return=([0, 1], {0: 0.8, 1: 0.01}),
    )

    assert [record.metadata["document_id"] for record in resp.records] == ["a"]


def test_threshold_zero_no_extra_filter():
    hits = [_hit("a", 0.9, "A"), _hit("b", 0.01, "B")]
    resp, _ = _run(_req(top_k=5, threshold=0.0), rerank_enabled=False, search_return=hits)
    assert len(resp.records) == 2                      # threshold=0 不额外过滤
