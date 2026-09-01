"""Hybrid 检索编排：dense/keyword 调用、RRF 融合、rerank 在融合后、threshold 语义、metadata、可见性。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.dify import DifyRetrievalRequest, DifyRetrievalResponse
from app.services import retrieval as R


def _req(top_k=5, threshold=0.0, query="保障农民工工资条例"):
    return DifyRetrievalRequest(
        knowledge_id="k", query=query,
        retrieval_setting={"top_k": top_k, "score_threshold": threshold},
    )


def _dense(pid, score, text="正文"):
    return {"id": pid, "score": score,
            "payload": {"document_id": pid, "chunk_id": pid, "text": text, "title": "t-" + pid}}


def _kw(cid, score, *, text="正文", title="t", external_id=None):
    return {"id": cid, "score": score,
            "payload": {"chunk_id": cid, "document_id": cid, "text": text, "title": title, "external_id": external_id}}


class _Lib:
    lifecycle_mode = "managed"


def _run(request, *, mode, dense=None, keyword=None, rerank=False, rank_return=None,
         visible_mask=None, db=None, library=None):
    embed_one = AsyncMock(return_value=[0.1] * 8)
    embed_texts = AsyncMock(side_effect=lambda texts, **k: [[0.1] * 8 for _ in texts])
    msearch = AsyncMock(return_value=dense or [])
    kw_recall = AsyncMock(return_value=keyword or [])

    def _mask(_db, _lib, payloads):
        return visible_mask if visible_mask is not None else [True] * len(payloads)

    stack = [
        patch.object(R.embedding, "embed_one", embed_one),
        patch.object(R.embedding, "embed_texts", embed_texts),
        patch.object(R.qdrant, "search", msearch),
        patch.object(R.keyword_search, "recall", kw_recall),
        patch.object(R.visibility, "compute_visible_mask", new=AsyncMock(side_effect=_mask)),
        patch.object(R.rerank_svc, "is_configured", return_value=rerank),
        patch.object(R.settings, "query_rewrite_enabled", False),
        patch.object(R.settings, "query_rewrite_llm_enabled", False),
    ]
    if rank_return is not None:
        rank = AsyncMock(return_value=rank_return)
        stack.append(patch.object(R.rerank_svc, "rank_candidates", rank))
    else:
        rank = None
        stack.append(patch.object(R.rerank_svc, "rerank", new_callable=AsyncMock, return_value=[]))

    for cm in stack:
        cm.start()
    try:
        resp = asyncio.run(R.run_retrieval(
            collection="c", embedding_model="m", embedding_base_url=None,
            request=request, source_config=None, rerank_enabled=rerank,
            retrieval_mode=mode, db=db, library=library,
        ))
    finally:
        for cm in reversed(stack):
            cm.stop()
    return resp, msearch, kw_recall, rank


# ── dense 模式不碰 keyword ───────────────────────────────────────────────────
def test_dense_mode_does_not_call_keyword_search():
    resp, msearch, kw_recall, _ = _run(
        _req(), mode="dense", dense=[_dense("a", 0.9)], db=None, library=None,
    )
    assert kw_recall.await_count == 0          # dense 路径不调 keyword
    assert [r.metadata["document_id"] for r in resp.records] == ["a"]
    assert "retrieval_mode" not in resp.records[0].metadata   # dense 不加 hybrid 留痕


# ── hybrid 调 dense + keyword ────────────────────────────────────────────────
def test_hybrid_calls_both_dense_and_keyword():
    resp, msearch, kw_recall, _ = _run(
        _req(), mode="hybrid", dense=[_dense("a", 0.9)], keyword=[_kw("a", 0.8)],
        db=object(), library=_Lib(),
    )
    assert msearch.await_count == 1 and kw_recall.await_count == 1
    assert isinstance(resp, DifyRetrievalResponse)
    assert resp.records[0].metadata["retrieval_mode"] == "hybrid"


def test_hybrid_degrades_to_dense_without_db():
    # 缺 db/library → 不能 hybrid，退化为 dense（不调 keyword）
    resp, _ms, kw_recall, _ = _run(
        _req(), mode="hybrid", dense=[_dense("a", 0.9)], db=None, library=None,
    )
    assert kw_recall.await_count == 0
    assert "retrieval_mode" not in resp.records[0].metadata


# ── RRF 融合排序稳定 + keyword-only 进结果 ──────────────────────────────────
def test_rrf_fusion_stable_and_keyword_only_enters():
    # dense: a(rank1), b(rank2)；keyword: a(rank1), c(rank2, 文件名命中, dense 没有)
    dense = [_dense("a", 0.9, text="A"), _dense("b", 0.5, text="B")]
    keyword = [_kw("a", 0.8, text="A"), _kw("c", 0.7, text="C", title="JGJ250-2011职业标准", external_id="JGJ250-2011")]
    resp, *_ = _run(_req(top_k=5), mode="hybrid", dense=dense, keyword=keyword,
                    db=object(), library=_Lib())
    ids = [r.metadata["document_id"] for r in resp.records]
    # a 双路命中(rrf=1/61+1/61 最高) → 第一；b/c 各单路一次命中(rrf=1/62) → 同分，按插入序 dense 先(b)再 c
    assert ids[0] == "a"
    assert "c" in ids                      # keyword-only 文件名命中进入结果
    by = {r.metadata["document_id"]: r for r in resp.records}
    assert by["a"].metadata["dense_rank"] == 1 and by["a"].metadata["keyword_rank"] == 1
    assert by["c"].metadata["dense_rank"] is None and by["c"].metadata["keyword_rank"] == 2
    assert "vector_score" not in by["c"].metadata        # keyword-only 无 vector_score
    assert by["a"].metadata["vector_score"] == 0.9       # dense 命中保留最高向量分
    assert by["c"].metadata["rrf_score"] > 0


def test_rrf_ties_are_stable_across_input_order():
    dense = [_dense("b", 0.8), _dense("a", 0.8)]
    keyword = [_kw("d", 0.7), _kw("c", 0.7)]

    first = R._rrf_fuse(dense, keyword, k=60)
    second = R._rrf_fuse(list(reversed(dense)), list(reversed(keyword)), k=60)

    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert [item["id"] for item in first] == ["a", "c", "b", "d"]


def test_deterministic_dense_control_uses_exact_pool_of_50():
    with patch.object(R.embedding, "embed_one", new=AsyncMock(return_value=[0.1] * 8)), \
         patch.object(R.qdrant, "search", new=AsyncMock(return_value=[])) as search, \
         patch.object(R.rerank_svc, "is_configured", return_value=False), \
         patch.object(R.rerank_svc, "rank_candidates", new=AsyncMock(return_value=([], {}))):
        asyncio.run(
            R.run_retrieval(
                collection="c",
                embedding_model="m",
                embedding_base_url=None,
                request=_req(top_k=10),
                retrieval_mode="dense",
                candidate_k=50,
                exact_vector_search=True,
            )
        )

    assert search.await_args.kwargs["limit"] == 50
    assert search.await_args.kwargs["exact"] is True


@pytest.mark.parametrize("candidate_k", [0, 201])
def test_candidate_pool_is_positive_and_bounded(candidate_k, monkeypatch):
    monkeypatch.setattr(R.settings, "visibility_overfetch_max", 200)

    with pytest.raises(ValueError, match="candidate_k"):
        asyncio.run(
            R.run_retrieval(
                collection="c",
                embedding_model="m",
                embedding_base_url=None,
                request=_req(top_k=10),
                retrieval_mode="dense",
                candidate_k=candidate_k,
                exact_vector_search=True,
            )
        )


# ── rerank 在融合后执行 ──────────────────────────────────────────────────────
def test_rerank_runs_after_fusion():
    dense = [_dense("a", 0.9, text="A"), _dense("b", 0.1, text="B")]
    keyword = [_kw("c", 0.7, text="C")]
    # 融合后 RRF 序 = [a, c, b]（a/c 同分按插入序 dense 先，b rank2 略低）；reranker 把 b 顶到第一
    rank_return = ([2, 0, 1], {2: 0.95, 0: 0.6, 1: 0.3})
    resp, _ms, _kwc, rank = _run(
        _req(top_k=5, threshold=0.0), mode="hybrid", dense=dense, keyword=keyword,
        rerank=True, rank_return=rank_return, db=object(), library=_Lib(),
    )
    assert rank.await_count == 1
    assert len(rank.await_args.args[1]) == 3        # rerank 作用于融合后的 3 个候选
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["b", "a", "c"]                   # 重排后顺序（证明 rerank 在融合之后）
    assert [r.score for r in resp.records] == [0.95, 0.6, 0.3]
    assert resp.records[0].metadata["retrieval_mode"] == "hybrid"


# ── threshold 语义不被破坏 ───────────────────────────────────────────────────
def test_threshold_skipped_for_hybrid_without_rerank():
    # rrf 量级 ~1/k，远小于 0.3 阈值；若误用阈值会清空 → 必须跳过
    dense = [_dense("a", 0.9)]
    keyword = [_kw("a", 0.8)]
    resp, *_ = _run(_req(top_k=5, threshold=0.3), mode="hybrid", dense=dense, keyword=keyword,
                    db=object(), library=_Lib())
    assert len(resp.records) == 1            # 未被 rrf<0.3 误杀


def test_hybrid_rejects_weak_dense_evidence_before_rrf_thresholding():
    resp, *_ = _run(
        _req(top_k=5, threshold=0.9),
        mode="hybrid",
        dense=[_dense("weak", 0.2, text="weak")],
        keyword=[_kw("weak", 0.9, text="weak")],
        db=object(),
        library=_Lib(),
    )

    assert resp.records == []
    assert resp.retrieval_debug["evidence"] == {
        "status": "insufficient",
        "policy": "hybrid_dense_minimum",
        "dense_minimum": 0.55,
        "max_dense_score": 0.2,
        "qualified_count": 0,
        "reason": "dense_score_below_minimum",
    }


def test_duplicate_content_uses_one_slot_and_keeps_bounded_sources():
    first = _dense("doc-a", 0.95, text="same content")
    first["payload"].update(
        document_revision_id="rev-a",
        document_revision=1,
        source_path="/contracts/a.pdf",
    )
    second = _dense("doc-b", 0.90, text="same   content")
    second["payload"].update(
        document_revision_id="rev-b",
        document_revision=2,
        source_path="/archive/b.pdf",
    )

    resp, *_ = _run(
        _req(top_k=2),
        mode="dense",
        dense=[first, second],
        db=None,
        library=None,
    )

    assert len(resp.records) == 1
    metadata = resp.records[0].metadata
    assert metadata["document_id"] == "doc-a"
    assert metadata["duplicate_count"] == 1
    assert metadata["duplicate_sources"] == [
        {
            "document_id": "doc-b",
            "document_revision_id": "rev-b",
            "document_revision": "2",
            "source_path": "/archive/b.pdf",
            "chunk_id": "doc-b",
            "title": "t-doc-b",
        }
    ]
    assert resp.retrieval_debug["duplicate_suppressed"] == 1


def test_threshold_applies_to_hybrid_rerank_score():
    dense = [_dense("a", 0.9), _dense("b", 0.5)]
    keyword = [_kw("a", 0.8)]
    # rerank 后 a=0.9 过阈，b=0.2 被 0.5 阈值过滤
    rank_return = ([0, 1], {0: 0.9, 1: 0.2})
    resp, *_ = _run(_req(top_k=5, threshold=0.5), mode="hybrid", dense=dense, keyword=keyword,
                    rerank=True, rank_return=rank_return, db=object(), library=_Lib())
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["a"]                      # 阈值作用在 rerank 分上，语义正常


def test_hybrid_rejects_successful_but_low_rerank_evidence():
    dense = [_dense("a", 0.9)]
    keyword = [_kw("a", 0.8)]
    resp, *_ = _run(
        _req(top_k=5),
        mode="hybrid",
        dense=dense,
        keyword=keyword,
        rerank=True,
        rank_return=([0], {0: 0.01}),
        db=object(),
        library=_Lib(),
    )

    assert resp.records == []
    assert resp.retrieval_debug["evidence"] == {
        "status": "insufficient",
        "policy": "rerank_minimum",
        "rerank_minimum": 0.05,
        "max_rerank_score": 0.01,
        "qualified_count": 0,
        "reason": "rerank_score_below_minimum",
    }


# ── 可见性过滤在 hybrid 仍生效 ──────────────────────────────────────────────
def test_visibility_filter_applies_in_hybrid():
    dense = [_dense("a", 0.9), _dense("b", 0.5)]
    keyword = [_kw("a", 0.8)]
    # 融合后 [a, b]，可见性把 b 过滤
    resp, *_ = _run(_req(top_k=5), mode="hybrid", dense=dense, keyword=keyword,
                    visible_mask=[True, False], db=object(), library=_Lib())
    ids = [r.metadata["document_id"] for r in resp.records]
    assert ids == ["a"]


# ── P1 修复：keyword-only 命中带 document_revision，更新文档不被误过滤（真实 visibility）──
def _run_real_visibility(*, keyword, doc_rows, lib_id):
    """跑 hybrid 但使用真实 visibility.compute_visible_mask；db.execute 返回 documents 行。"""
    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Res(doc_rows))

    class _Lib2:
        id = lib_id
        lifecycle_mode = "managed"

    stack = [
        patch.object(R.embedding, "embed_one", new=AsyncMock(return_value=[0.1] * 8)),
        patch.object(R.qdrant, "search", new=AsyncMock(return_value=[])),       # dense 没召回
        patch.object(R.keyword_search, "recall", new=AsyncMock(return_value=keyword)),
        patch.object(R.rerank_svc, "is_configured", return_value=False),
        patch.object(R.rerank_svc, "rerank", new_callable=AsyncMock, return_value=[]),
        patch.object(R.settings, "query_rewrite_enabled", False),
        patch.object(R.settings, "query_rewrite_llm_enabled", False),
        patch.object(R.settings, "retrieval_consistency_filter", True),         # 打开可见性过滤
    ]
    for cm in stack:
        cm.start()
    try:
        return asyncio.run(R.run_retrieval(
            collection="c", embedding_model="m", embedding_base_url=None,
            request=_req(top_k=5), source_config=None, rerank_enabled=False,
            retrieval_mode="hybrid", db=db, library=_Lib2(),
        ))
    finally:
        for cm in reversed(stack):
            cm.stop()


def _kw_doc(did, rev):
    """构造 keyword-only 命中（带 document_revision，模拟修复后的 keyword_search 输出）。"""
    h = _kw(str(did), 0.8, title="JGJ250-2011职业标准", external_id="JGJ250-2011")
    h["payload"]["document_revision"] = rev
    return h


def test_keyword_only_updated_doc_not_filtered():
    lib_id, did = uuid.uuid4(), uuid.uuid4()
    resp = _run_real_visibility(
        keyword=[_kw_doc(did, 2)],                    # 文档已更新到 rev 2，keyword 带 rev 2
        doc_rows=[(did, lib_id, 2, None)], lib_id=lib_id,
    )
    assert [r.metadata["document_id"] for r in resp.records] == [str(did)]   # 不被误过滤


def test_keyword_only_missing_revision_would_be_filtered():
    # 反证：若 keyword payload 不带 document_revision（旧 bug），rev 2 文档会被误过滤
    lib_id, did = uuid.uuid4(), uuid.uuid4()
    h = _kw(str(did), 0.8)
    h["payload"].pop("document_revision", None)        # 故意去掉
    resp = _run_real_visibility(keyword=[h], doc_rows=[(did, lib_id, 2, None)], lib_id=lib_id)
    assert resp.records == []                           # 缺 revision → 被当作 rev 1 → 误过滤


def test_keyword_only_deleted_doc_still_filtered():
    lib_id, did = uuid.uuid4(), uuid.uuid4()
    resp = _run_real_visibility(
        keyword=[_kw_doc(did, 2)],
        doc_rows=[(did, lib_id, 2, datetime(2026, 1, 1, tzinfo=timezone.utc))],  # 已删
        lib_id=lib_id,
    )
    assert resp.records == []                           # 删档仍过滤


def test_keyword_only_other_library_still_filtered():
    lib_id, other_lib, did = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    resp = _run_real_visibility(
        keyword=[_kw_doc(did, 2)],
        doc_rows=[(did, other_lib, 2, None)],          # 文档属于别的库
        lib_id=lib_id,
    )
    assert resp.records == []                           # 库隔离仍生效
