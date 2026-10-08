"""Chat 会话历史 + 问答审计：会话归属、续聊校验、落库、多轮上下文、管理日志。"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.api.admin_chat_logs as admin_logs_api
import app.api.chat as chat_api
from app.auth.backend import current_active_user, current_superuser
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.chat import ChatLogRow, ChatSource
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services.chat_answer import ChatAnswer, ChatError
from tests.test_chat_api import _unit_evidence

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

mock_user = User(id=str(USER_ID), email="u@example.com", is_superuser=False, is_active=True)
mock_library = Library(
    id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
    slug="medical", name="医学库", qdrant_collection="lib_medical",
    embedding_model="bge-m3", embedding_dim=1024, chunk_size=1000, chunk_overlap=120,
    lifecycle_mode="managed", index_state="ready",
)


class _Conv:
    def __init__(self, *, user_id=USER_ID, status="active", slug="medical"):
        self.id = uuid.uuid4()
        self.user_id = user_id
        self.library_slug = slug
        self.title = "标题"
        self.status = status
        self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)


class _FakeSessionCM:
    async def __aenter__(self):
        db = AsyncMock()
        db.add = MagicMock()
        return db

    async def __aexit__(self, *a):
        return False


def _override(db):
    async def ov_user():
        return mock_user

    async def ov_db():
        return db

    app.dependency_overrides[current_active_user] = ov_user
    app.dependency_overrides[get_db] = ov_db


def _rec(content="片段", *, doc="d1", chunk="c1", md=None):
    meta = {"document_id": doc, "chunk_id": chunk}
    if md:
        meta.update(md)
    return DifyRecord(content=content, score=0.9, title="标题", metadata=meta)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(chat_api.chat_evidence, "collect_chat_evidence", _unit_evidence)
    mock_user.is_superuser = False
    yield
    app.dependency_overrides.clear()


_UNSET = object()


def _qa_patches(records, *, conv=None, gen=None, recent=None, save_asst=None, get_conv=_UNSET):
    """问答管线 + 落库 patch。get_conv 显式传 None 表示会话不存在。"""
    conv = conv or _Conv()
    umsg = MagicMock()
    umsg.id = uuid.uuid4()
    gen = gen if gen is not None else AsyncMock(return_value=ChatAnswer(answer="答案", used_records=records))
    save_asst = save_asst or AsyncMock()
    resolved_get = conv if get_conv is _UNSET else get_conv
    patches = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=AsyncMock(return_value=DifyRetrievalResponse(records=records))),
        patch.object(chat_api.chat_answer, "generate_answer", new=gen),
        patch.object(chat_api.chat_history, "create_conversation", new=AsyncMock(return_value=conv)),
        patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=resolved_get)),
        patch.object(chat_api.chat_history, "recent_turns", new=AsyncMock(return_value=recent or [])),
        patch.object(chat_api.chat_history, "save_user_message", new=AsyncMock(return_value=umsg)),
        patch.object(chat_api.chat_history, "save_assistant_message", new=save_asst),
        patch.object(chat_api.chat_history, "touch_conversation", new=AsyncMock()),
        patch.object(chat_api, "async_session_factory", new=_FakeSessionCM),
    ]
    return patches, conv, umsg, gen, save_asst


# ── 会话归属 ────────────────────────────────────────────────────────────────
def test_list_conversations_scoped_to_current_user():
    convs = [_Conv()]
    _override(AsyncMock())
    lc = AsyncMock(return_value=convs)
    with patch.object(chat_api.chat_history, "list_conversations", new=lc):
        r = TestClient(app).get("/chat/conversations")
    assert r.status_code == 200 and len(r.json()) == 1
    assert lc.await_args.args[1] == mock_user.id      # 只查当前用户的会话


def test_history_messages_preserve_source_display_score_semantics():
    conv = _Conv()
    message = SimpleNamespace(
        id=uuid.uuid4(), role="assistant", content="answer", status="success",
        error_message=None, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        graph_augmented=False, graph_evidence=[],
    )
    source = SimpleNamespace(
        title="source", document_id="d1", chunk_id="c1", seq=3, score=0.0164,
        score_type="vector", display_score=0.724, content="chunk",
    )
    _override(AsyncMock())
    with patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=conv)), \
         patch.object(chat_api.chat_history, "get_conversation_messages", new=AsyncMock(return_value=[(message, [source])])) as get_messages:
        before = uuid.uuid4()
        response = TestClient(app).get(
            f"/chat/conversations/{conv.id}/messages?limit=7&before={before}"
        )

    assert response.status_code == 200
    restored = response.json()[0]["sources"][0]
    assert restored["score"] == 0.0164
    assert restored["score_type"] == "vector"
    assert restored["display_score"] == 0.724
    assert get_messages.await_args.kwargs["limit"] == 7
    assert get_messages.await_args.kwargs["before"] == before


def test_get_other_users_conversation_messages_403():
    other = _Conv(user_id=OTHER_ID)
    _override(AsyncMock())
    with patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=other)):
        r = TestClient(app).get(f"/chat/conversations/{other.id}/messages")
    assert r.status_code == 403


def test_continue_conversation_not_owned_403():
    other = _Conv(user_id=OTHER_ID)
    _override(AsyncMock())
    patches, *_ = _qa_patches([_rec()], get_conv=other)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "q",
                                       "conversation_id": str(other.id)})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 403


def test_continue_conversation_not_owned_does_not_run_retrieval():
    other = _Conv(user_id=OTHER_ID)
    _override(AsyncMock())
    retr = AsyncMock(return_value=DifyRetrievalResponse(records=[_rec()]))
    patches = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=retr),
        patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=other)),
    ]
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post(
            "/chat/messages",
            json={"library_slug": "medical", "query": "q", "conversation_id": str(other.id)},
        )
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 403
    retr.assert_not_awaited()


def test_stream_conversation_not_owned_does_not_run_retrieval():
    other = _Conv(user_id=OTHER_ID)
    _override(AsyncMock())
    retr = AsyncMock(return_value=DifyRetrievalResponse(records=[_rec()]))
    patches = [
        patch.object(chat_api.settings, "chat_enabled", True),
        patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)),
        patch.object(chat_api, "has_permission", return_value=True),
        patch.object(chat_api, "run_retrieval", new=retr),
        patch.object(chat_api.chat_history, "get_conversation", new=AsyncMock(return_value=other)),
    ]
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post(
            "/chat/stream",
            json={"library_slug": "medical", "query": "q", "conversation_id": str(other.id)},
        )
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 403
    retr.assert_not_awaited()


def test_no_read_permission_cannot_create_conversation():
    _override(AsyncMock())
    with patch.object(chat_api.settings, "chat_enabled", True), \
         patch.object(chat_api, "load_active_library", new=AsyncMock(return_value=mock_library)), \
         patch.object(chat_api, "has_permission", return_value=False):
        r = TestClient(app).post("/chat/messages", json={"library_slug": "medical", "query": "q"})
    assert r.status_code == 403


def test_archived_conversation_cannot_continue_409():
    archived = _Conv(status="archived")
    _override(AsyncMock())
    patches, *_ = _qa_patches([_rec()], get_conv=archived)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "q",
                                       "conversation_id": str(archived.id)})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 409


def test_delete_then_continue_404_or_403():
    conv = _Conv()
    _override(AsyncMock())
    # 删除后该会话不存在 → get_conversation 返回 None → 续聊 403
    patches, *_ = _qa_patches([_rec()], get_conv=None)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "q",
                                       "conversation_id": str(conv.id)})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 403


# ── 落库 ────────────────────────────────────────────────────────────────────
def test_success_persists_user_and_assistant_with_sources():
    records = [_rec("片段A", doc="d1", chunk="c1")]
    _override(AsyncMock())
    save_user = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    save_asst = AsyncMock()
    patches, conv, _umsg, _gen, _sa = _qa_patches(records, save_asst=save_asst)
    # 替换 save_user_message 以便断言被调用
    patches.append(patch.object(chat_api.chat_history, "save_user_message", new=save_user))
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages", json={"library_slug": "medical", "query": "保障条例"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    assert r.json()["conversation_id"] == str(conv.id)
    save_user.assert_awaited_once()
    save_asst.assert_awaited_once()
    kw = save_asst.await_args.kwargs
    assert kw["status"] == "success"
    assert len(kw["sources"]) == 1 and kw["sources"][0].document_id == "d1"
    assert kw["parent_message_id"] == save_user.return_value.id


def test_llm_failure_persists_failed_message():
    records = [_rec()]
    _override(AsyncMock())
    gen = AsyncMock(side_effect=ChatError("chat 模型调用超时"))
    save_asst = AsyncMock()
    patches, *_ = _qa_patches(records, gen=gen, save_asst=save_asst)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages", json={"library_slug": "medical", "query": "q"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 502
    save_asst.assert_awaited_once()                  # 失败也落库
    kw = save_asst.await_args.kwargs
    assert kw["status"] == "failed"
    assert kw["error_message"] and "超时" in kw["error_message"]


# ── 多轮上下文 ──────────────────────────────────────────────────────────────
def test_multi_turn_passes_recent_history_to_llm():
    records = [_rec()]
    history = [{"role": "user", "content": "上一个问题"}, {"role": "assistant", "content": "上一个答案"}]
    _override(AsyncMock())
    gen = AsyncMock(return_value=ChatAnswer(answer="答", used_records=records))
    conv = _Conv()
    patches, *_ = _qa_patches(records, conv=conv, gen=gen, recent=history, get_conv=conv)
    for p in patches:
        p.start()
    try:
        r = TestClient(app).post("/chat/messages",
                                 json={"library_slug": "medical", "query": "那它罚多少",
                                       "conversation_id": str(conv.id)})
    finally:
        for p in reversed(patches):
            p.stop()
    assert r.status_code == 200
    assert gen.await_args.kwargs["history"] == history      # 最近上下文带给了 LLM


# ── 管理后台问答日志 ────────────────────────────────────────────────────────
def test_admin_chat_logs_superuser_only():
    row = ChatLogRow(
        message_id=uuid.uuid4(), conversation_id=uuid.uuid4(), user_id=USER_ID,
        library_slug="medical", question="问", answer="答", status="success",
        latency_ms=120, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sources=[ChatSource(title="t", document_id="d1", chunk_id="c1", score=0.9, content="片段")],
    )

    async def ov_super():
        return mock_user

    app.dependency_overrides[current_superuser] = ov_super
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with patch.object(admin_logs_api.chat_history, "list_logs", new=AsyncMock(return_value=[row])):
        r = TestClient(app).get("/admin/chat-logs?library_slug=medical&status=success")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["question"] == "问" and body[0]["answer"] == "答"
    assert body[0]["sources"][0]["document_id"] == "d1"


def test_admin_chat_logs_unauthenticated_401():
    app.dependency_overrides.clear()
    r = TestClient(app).get("/admin/chat-logs")
    assert r.status_code in (401, 403)


def test_history_restores_mixed_citation_numbers_without_current_location():
    from types import SimpleNamespace
    from app.services import chat_history
    sources = [SimpleNamespace(seq=i, title="saved", document_id="doc", chunk_id=f"chunk-{i}",
                               score=1, score_type="legacy", display_score=None, content="saved body")
               for i in range(2)]
    # The persisted graph indexes occupy the gaps in the ordinary source order.
    restored = chat_history.historical_sources_to_schema(sources, [{"citation_index": 2}])
    assert [source.citation_index for source in restored] == [1, 3]
    assert all(source.location is None and source.document_revision_id is None for source in restored)
    assert [source.content for source in restored] == ["saved body", "saved body"]


def test_history_legacy_sources_without_graph_keep_contiguous_numbers():
    from types import SimpleNamespace
    from app.services import chat_history
    source = SimpleNamespace(seq=0, title="saved", document_id="doc", chunk_id="chunk",
                             score=1, score_type="legacy", display_score=None, content="saved body")
    assert chat_history.historical_sources_to_schema([source], None)[0].citation_index == 1
