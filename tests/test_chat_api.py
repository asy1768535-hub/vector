"""Chat 用户端接口测试：开关/鉴权/权限/校验/检索接入/调试开关。

真实 router + schema 校验；检索与 LLM 用 mock（不连真实服务）。
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.auth.backend import current_active_user
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.folder import Folder
from app.models.user import User
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services.chat_answer import ChatAnswer

mock_user = User(
    id="00000000-0000-0000-0000-000000000001",
    email="u@example.com", is_superuser=True, is_active=True,
)
mock_library = Library(
    id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
    slug="medical", name="医学库", description="desc", qdrant_collection="lib_medical",
    embedding_model="bge-m3", embedding_dim=1024, chunk_size=1000, chunk_overlap=120,
    lifecycle_mode="managed", index_state="ready",
)


def _rec(content="片段正文", *, doc="d1", chunk="c1", score=0.88, md=None):
    meta = {"document_id": doc, "chunk_id": chunk}
    if md:
        meta.update(md)
    return DifyRecord(content=content, score=score, title="标题", metadata=meta)


@pytest.mark.parametrize(
    ("metadata", "score_type", "display_score"),
    [
        ({"rerank_score": 0.863, "vector_score": 0.724, "rrf_score": 0.0164}, "rerank", 0.863),
        ({"vector_score": 0.724, "rrf_score": 0.0164}, "vector", 0.724),
        ({"rrf_score": 0.0164}, "rrf", None),
    ],
)
def test_chat_source_uses_only_reliable_display_scores(metadata, score_type, display_score):
    record = DifyRecord(
        content="source", score=0.0164, title="source",
        metadata={"document_id": "d1", "chunk_id": "c1", **metadata},
    )
    source = chat_api._to_source(record)

    assert source.score == 0.0164
    assert source.score_type == score_type
    assert source.display_score == display_score


class _Conv:
    def __init__(self):
        self.id = uuid.uuid4()
        self.user_id = mock_user.id
        self.library_slug = "medical"
        self.status = "active"


class _FakeSessionCM:
    async def __aenter__(self):
        db = AsyncMock()
        db.add = MagicMock()
        return db

    async def __aexit__(self, *a):
        return False


def _fake_session_factory():
    return _FakeSessionCM()


def _history_patches():
    """patch chat_history 落库函数（不连真实 DB）+ 流式用的独立 session 工厂。"""
    conv = _Conv()
    umsg = MagicMock()
    umsg.id = uuid.uuid4()
    return [
        patch.object(chat_api.chat_history, "create_conversation", new=AsyncMock(return_value=conv)),
        patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=conv)),
        patch.object(chat_api.chat_history, "recent_turns", new=AsyncMock(return_value=[])),
        patch.object(chat_api.chat_history, "save_user_message", new=AsyncMock(return_value=umsg)),
        patch.object(chat_api.chat_history, "save_assistant_message", new=AsyncMock()),
        patch.object(chat_api.chat_history, "touch_conversation", new=AsyncMock()),
        patch.object(chat_api, "async_session_factory", new=_fake_session_factory),
    ]


def _override(user, db):
    async def ov_user():
        return user

    async def ov_db():
        return db

    app.dependency_overrides[current_active_user] = ov_user
    app.dependency_overrides[get_db] = ov_db


def _db_with_libs(libs):
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = libs
    db.execute = AsyncMock(return_value=res)
    db.add = MagicMock()
    return db


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # API contract tests use opaque synthetic IDs; actual ownership SQL and
    # both HTTP entrypoints are exercised separately in test_chat_evidence_pg.
    monkeypatch.setattr(chat_api.chat_evidence, "collect_chat_evidence", _unit_evidence)
    mock_user.is_superuser = True
    yield
    app.dependency_overrides.clear()


async def _unit_evidence(db, library, query, *, top_k, max_context_chars, retrieve, allowed_document_ids=None):
    from app.schemas.dify import DifyRetrievalRequest
    metadata_condition = None if allowed_document_ids is None else {"conditions": [{
        "name": ["document_id"], "comparison_operator": "in", "value": allowed_document_ids,
    }]}
    response = await retrieve(DifyRetrievalRequest.model_validate({"knowledge_id": library.slug, "query": query,
        "retrieval_setting": {"top_k": top_k}, "metadata_condition": metadata_condition}))
    return response.records, response.retrieval_debug


# ── /chat/libraries ─────────────────────────────────────────────────────────
def test_superuser_lists_all_active_libs():
    _override(mock_user, _db_with_libs([mock_library]))
    r = TestClient(app).get("/chat/libraries")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["slug"] == "medical"
    assert body[0]["name"] == "医学库" and body[0]["description"] == "desc"


def test_read_user_lists_only_read_libs():
    mock_user.is_superuser = False
    db = _db_with_libs([mock_library])
    _override(mock_user, db)
    with patch.object(chat_api.casbin_service, "list_user_permissions",
                      return_value={"medical": ["read"]}):
        r = TestClient(app).get("/chat/libraries")
    assert r.status_code == 200
    assert [x["slug"] for x in r.json()] == ["medical"]


def test_read_user_without_perms_gets_empty_list():
    mock_user.is_superuser = False
    db = _db_with_libs([mock_library])
    _override(mock_user, db)
    with patch.object(chat_api.casbin_service, "list_user_permissions", return_value={}):
        r = TestClient(app).get("/chat/libraries")
    assert r.status_code == 200 and r.json() == []
    db.execute.assert_not_awaited()           # 无 read 权限直接短路，不查库


def test_unauthenticated_rejected():
    app.dependency_overrides.clear()          # 不注入用户 → 真实鉴权
    c = TestClient(app)
    assert c.get("/chat/libraries").status_code in (401, 403)
    assert c.post("/chat/messages",
                  json={"library_slug": "medical", "query": "q"}).status_code in (401, 403)


# ── /chat/messages：开关 / 权限 / 校验 ───────────────────────────────────────
def test_messages_503_when_disabled():
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", False):
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "保障农民工工资条例"})
    assert r.status_code == 503
    assert "未启用" in r.json()["detail"]


def test_messages_403_without_read_permission():
    mock_user.is_superuser = False
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", True), \
         patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)), \
         patch.object(chat_api, "has_permission", return_value=False):
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "条例说了什么"})
    assert r.status_code == 403


def test_messages_empty_query_422():
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", True):
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "   "})
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("合同金额和供应商是谁？只根据材料回答。", "合同金额和供应商是谁"),
        ("合同金额和供应商是谁，请仅依据原文回答", "合同金额和供应商是谁"),
        ("只根据材料回答", "只根据材料回答"),
        ("根据材料回答的内容是什么", "根据材料回答的内容是什么"),
    ],
)
def test_retrieval_query_removes_only_explicit_answer_constraint(query, expected):
    assert chat_api._retrieval_query(query) == expected


def test_messages_top_k_out_of_range_422():
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", True):
        r1 = TestClient(app).post("/chat/messages",
                                  json={"library_slug": "medical", "query": "q", "top_k": 0})
        r2 = TestClient(app).post("/chat/messages",
                                  json={"library_slug": "medical", "query": "q", "top_k": 99})
    assert r1.status_code == 422 and r2.status_code == 422


# ── /chat/messages：检索接入 + 答案 + 调试 ───────────────────────────────────
def _patch_pipeline(records, *, answer="答案"):
    """patch 掉检索、LLM、落库，返回固定 records / answer。"""
    retr = AsyncMock(return_value=DifyRetrievalResponse(records=records))
    gen = AsyncMock(return_value=ChatAnswer(answer=answer, used_records=records))
    base = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=retr),
        patch.object(chat_api.chat_answer, "generate_answer", new=gen),
    ]
    return base + _history_patches(), retr, gen


def test_messages_return_rerank_display_score():
    records = [DifyRecord(
        content="source", score=0.0164, title="source",
        metadata={"document_id": "d1", "chunk_id": "c1", "rerank_score": 0.863},
    )]
    _override(mock_user, AsyncMock())
    patches, retr, gen = _patch_pipeline(records)
    for p in patches:
        p.start()
    try:
        query = "合同金额和供应商是谁？只根据材料回答。"
        response = TestClient(app).post(
            "/chat/messages", json={"library_slug": "medical", "query": query}
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert response.status_code == 200
    assert response.json()["sources"][0]["score_type"] == "rerank"
    assert response.json()["sources"][0]["display_score"] == 0.863
    assert retr.await_args.kwargs["request"].query == "合同金额和供应商是谁"
    assert gen.await_args.args[0] == query


def test_messages_calls_retrieval_and_returns_sources():
    records = [_rec("片段A", doc="d1", chunk="c1", score=0.91, md={"seq": 7})]
    _override(mock_user, AsyncMock())
    patches, retr, gen = _patch_pipeline(records, answer="根据[1]作答")
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "条例说了什么", "top_k": 5})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "根据[1]作答"
    assert len(body["sources"]) == 1
    s = body["sources"][0]
    assert s["document_id"] == "d1" and s["chunk_id"] == "c1"
    assert s["seq"] == 7
    assert s["score"] == 0.91 and s["content"] == "片段A"
    assert body["debug"] is None                 # 默认不返回调试
    retr.assert_awaited_once()                   # 确实复用了检索
    # 越权防护：collection 由库推导，不接受用户传入
    assert retr.await_args.kwargs["collection"] == "lib_medical"


def test_messages_folder_filter_limits_retrieval_documents():
    folder_id = uuid.uuid4()
    document_id = uuid.uuid4()
    folder = Folder(
        id=folder_id,
        library_id=mock_library.id,
        name="合同",
        path="/合同",
        deleted_at=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=folder)
    folder_result = MagicMock()
    folder_result.scalars.return_value.all.return_value = [folder_id]
    document_result = MagicMock()
    document_result.scalars.return_value.all.return_value = [document_id]
    db.execute = AsyncMock(side_effect=[folder_result, document_result])
    _override(mock_user, db)
    records = [_rec("片段A", doc=str(document_id))]
    patches, retr, _gen = _patch_pipeline(records)
    for patcher in patches:
        patcher.start()
    try:
        response = TestClient(app).post(
            "/chat/messages",
            json={
                "library_slug": "medical",
                "folder_id": str(folder_id),
                "query": "合同内容",
            },
        )
    finally:
        for patcher in reversed(patches):
            patcher.stop()
    assert response.status_code == 200
    condition = retr.await_args.kwargs["request"].metadata_condition
    assert condition.conditions[0].name == ["document_id"]
    assert condition.conditions[0].value == [str(document_id)]


def test_messages_no_records_returns_no_evidence_without_llm():
    _override(mock_user, AsyncMock())
    patches, retr, gen = _patch_pipeline([], answer="x")
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "无关问题"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    assert "未找到明确依据" in r.json()["answer"]
    assert r.json()["sources"] == []
    gen.assert_not_awaited()                      # 无命中不调用 LLM


def test_messages_debug_toggle():
    records = [_rec("片段A", md={"matched_queries": ["q1", "q2"], "rerank_score": 0.8})]
    _override(mock_user, AsyncMock())
    patches, _retr, _gen = _patch_pipeline(records)
    for p in patches:
        p.start()
    try:
        r_on = TestClient(app).post("/chat/messages",
                                    json={"library_slug": "medical", "query": "q", "show_debug": True})
        r_off = TestClient(app).post("/chat/messages",
                                     json={"library_slug": "medical", "query": "q", "show_debug": False})
    finally:
        for p in reversed(patches):
            p.stop()
    dbg = r_on.json()["debug"]
    assert dbg is not None
    assert dbg["matched_queries"] == ["q1", "q2"]
    assert dbg["rerank_scores"] == [0.8]
    assert r_off.json()["debug"] is None


def test_chat_debug_includes_bounded_retrieval_observation():
    records = [_rec("片段A", md={"rerank_score": 0.8})]
    retrieval_debug = {
        "rerank": {
            "effective": "fallback",
            "provider": "tei",
            "candidate_count": 50,
            "scored_count": 0,
            "fallback_reason": "provider_error",
        },
        "duplicate_suppressed": 2,
        "evidence": {"status": "insufficient", "reason": "dense_score_below_minimum"},
    }

    debug = chat_api._build_debug(records, 5, retrieval_debug)

    assert debug["retrieval"] == retrieval_debug


def test_messages_llm_failure_returns_502():
    records = [_rec("片段A")]
    _override(mock_user, AsyncMock())
    from app.services.chat_answer import ChatError
    gen = AsyncMock(side_effect=ChatError("chat 模型调用超时"))
    patches = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=AsyncMock(return_value=DifyRetrievalResponse(records=records))),
        patch.object(chat_api.chat_answer, "generate_answer", new=gen),
    ] + _history_patches()
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages", json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 502
    assert "answer generation failed" in r.json()["detail"]


# ── /chat/stream（SSE 流式）────────────────────────────────────────────────
def test_stream_503_when_disabled():
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", False):
        r = TestClient(app).post("/chat/stream", json={"library_slug": "medical", "query": "q"})
    assert r.status_code == 503


def test_stream_403_without_read():
    mock_user.is_superuser = False
    _override(mock_user, AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", True), \
         patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)), \
         patch.object(chat_api, "has_permission", return_value=False):
        r = TestClient(app).post("/chat/stream", json={"library_slug": "medical", "query": "q"})
    assert r.status_code == 403


def _stream_patches(records, stream_fn):
    base = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=AsyncMock(return_value=DifyRetrievalResponse(records=records))),
        patch.object(chat_api.chat_answer, "stream_answer", new=stream_fn),
    ]
    return base + _history_patches()


def test_stream_emits_sources_then_deltas_then_done():
    records = [_rec("片段A", doc="d1", chunk="c1", score=0.9)]
    _override(mock_user, AsyncMock())

    async def fake_stream(*a, **k):
        yield "答"
        yield "案"

    patches = _stream_patches(records, fake_stream)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/stream",
                                 json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert '"type": "sources"' in body and '"document_id": "d1"' in body
    assert '"type": "delta"' in body and "答" in body and "案" in body
    assert '"type": "done"' in body
    # sources 事件必须在 delta 之前
    assert body.index('"sources"') < body.index('"delta"')


def test_stream_emits_vector_display_score():
    records = [DifyRecord(
        content="source", score=0.0164, title="source",
        metadata={"document_id": "d1", "chunk_id": "c1", "vector_score": 0.724},
    )]
    _override(mock_user, AsyncMock())

    async def fake_stream(*_args, **_kwargs):
        yield "answer"

    patches = _stream_patches(records, fake_stream)
    for p in patches:
        p.start()
    try:
        response = TestClient(app).post("/chat/stream", json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()

    assert response.status_code == 200
    assert '"score_type": "vector"' in response.text
    assert '"display_score": 0.724' in response.text


def test_stream_emits_error_event_on_chat_error():
    records = [_rec("片段A")]
    _override(mock_user, AsyncMock())

    async def boom(*a, **k):
        raise chat_api.chat_answer.ChatError("chat 模型调用超时")
        yield  # 不可达：仅让函数成为 async generator

    patches = _stream_patches(records, boom)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/stream",
                                 json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200            # 流已开始，错误以事件形式下发
    assert '"type": "error"' in r.text


def test_stream_no_records_emits_no_evidence():
    _override(mock_user, AsyncMock())

    async def fake_stream(*a, **k):       # 不应被调用
        yield "x"

    patches = _stream_patches([], fake_stream)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/stream",
                                 json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    assert "未找到明确依据" in r.text
    assert '"type": "done"' in r.text
