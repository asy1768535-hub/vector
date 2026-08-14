from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

import app.api.chat as chat_api
from app.models.user import User
from app.schemas.chat import ChatMessageRequest
from app.schemas.dify import DifyRecord
from app.services.chat_answer import ChatError
from app.services.chat_history import get_conversation_messages, recent_turns


USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _FakeSessionCM:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *args):
        return False


def _record():
    return DifyRecord(
        content="source", score=0.9, title="title",
        metadata={"document_id": "d1", "chunk_id": "c1"},
    )


async def _open_stream(stream_fn, *, records=None, save_assistant=None):
    conv = SimpleNamespace(id=uuid.uuid4())
    user_message = SimpleNamespace(id=uuid.uuid4())
    request_db = AsyncMock()
    persist_db = AsyncMock()
    persist_db.add = MagicMock()
    save_assistant = save_assistant or AsyncMock()
    stack = ExitStack()
    stack.enter_context(patch.object(chat_api, "_resolve_conversation", new=AsyncMock(return_value=conv)))
    stack.enter_context(patch.object(
        chat_api, "_retrieve_for_chat",
        new=AsyncMock(return_value=(SimpleNamespace(), records or [_record()], None)),
    ))
    stack.enter_context(patch.object(
        chat_api.chat_graph_augmentation,
        "prepare_chat_graph_augmentation",
        new=AsyncMock(return_value=SimpleNamespace(records=(), context_chars=0)),
    ))
    stack.enter_context(patch.object(chat_api.chat_history, "recent_turns", new=AsyncMock(return_value=[])))
    stack.enter_context(patch.object(
        chat_api.chat_history, "save_user_message", new=AsyncMock(return_value=user_message),
    ))
    stack.enter_context(patch.object(
        chat_api.chat_history, "save_assistant_message", new=save_assistant,
    ))
    stack.enter_context(patch.object(
        chat_api.chat_history, "touch_conversation", new=AsyncMock(),
    ))
    stack.enter_context(patch.object(
        chat_api, "async_session_factory", new=lambda: _FakeSessionCM(persist_db),
    ))
    stack.enter_context(patch.object(chat_api.chat_answer, "stream_answer", new=stream_fn))
    response = await chat_api.chat_stream(
        ChatMessageRequest(
            library_slug="medical", query="question", conversation_id=conv.id,
        ),
        User(id=str(USER_ID), email="u@example.com", is_active=True),
        request_db,
    )
    return stack, response, save_assistant, request_db


async def _consume(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return chunks


async def _cancel_after_provider_started(response, started):
    chunks = []

    async def consume():
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return chunks


async def test_stream_cancel_without_delta_persists_failed_assistant():
    started = asyncio.Event()

    async def provider(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield "unreachable"

    stack, response, save_assistant, request_db = await _open_stream(provider)
    try:
        chunks = await _cancel_after_provider_started(response, started)
    finally:
        stack.close()

    assert request_db.commit.await_count == 1
    assert not any('"type": "error"' in chunk or '"type": "done"' in chunk for chunk in chunks)
    save_assistant.assert_awaited_once()
    assert save_assistant.await_args.args[0] is not request_db
    persisted = save_assistant.await_args.kwargs
    assert persisted["content"] == ""
    assert persisted["status"] == "failed"
    assert persisted["error_message"] == chat_api._CHAT_STREAM_CANCELLED


async def test_stream_cancel_after_delta_persists_partial_failed_assistant():
    started = asyncio.Event()

    async def provider(*args, **kwargs):
        yield "partial"
        started.set()
        await asyncio.Event().wait()

    stack, response, save_assistant, request_db = await _open_stream(provider)
    try:
        chunks = await _cancel_after_provider_started(response, started)
    finally:
        stack.close()

    assert request_db.commit.await_count == 1
    assert not any('"type": "error"' in chunk or '"type": "done"' in chunk for chunk in chunks)
    assert save_assistant.await_args.args[0] is not request_db
    persisted = save_assistant.await_args.kwargs
    assert persisted["content"] == "partial"
    assert persisted["status"] == "failed"
    assert persisted["error_message"] == chat_api._CHAT_STREAM_CANCELLED


async def test_stream_cancel_persistence_failure_keeps_cancelled_error(caplog):
    started = asyncio.Event()

    async def provider(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield "unreachable"

    save_assistant = AsyncMock(side_effect=RuntimeError("database detail"))
    stack, response, _, _ = await _open_stream(provider, save_assistant=save_assistant)
    try:
        with caplog.at_level("ERROR", logger=chat_api.log.name):
            chunks = await _cancel_after_provider_started(response, started)
    finally:
        stack.close()

    assert save_assistant.await_count == 1
    assert not any('"type": "error"' in chunk or '"type": "done"' in chunk for chunk in chunks)
    assert "chat stream cancellation persistence failed" in caplog.text
    assert "database detail" not in caplog.text


async def test_stream_success_persists_once_and_emits_done():
    async def provider(*args, **kwargs):
        yield "answer"

    stack, response, save_assistant, _ = await _open_stream(provider)
    try:
        chunks = await _consume(response)
    finally:
        stack.close()

    assert any('"type": "sources"' in chunk for chunk in chunks)
    assert any('"type": "delta"' in chunk for chunk in chunks)
    assert any('"type": "done"' in chunk for chunk in chunks)
    save_assistant.assert_awaited_once()
    assert save_assistant.await_args.kwargs["status"] == "success"


async def test_stream_chat_error_persists_failed_and_emits_error():
    async def provider(*args, **kwargs):
        raise ChatError("provider detail")
        yield "unreachable"

    stack, response, save_assistant, _ = await _open_stream(provider)
    try:
        chunks = await _consume(response)
    finally:
        stack.close()

    assert any('"type": "error"' in chunk for chunk in chunks)
    assert not any('"type": "done"' in chunk for chunk in chunks)
    save_assistant.assert_awaited_once()
    assert save_assistant.await_args.kwargs["status"] == "failed"
    assert save_assistant.await_args.kwargs["error_message"] == "provider detail"


async def test_stream_output_limit_closes_provider_and_persists_fixed_failure():
    class _ClosableProvider:
        def __init__(self):
            self.closed = False
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return "x" * (chat_api.chat_answer.CHAT_OUTPUT_MAX_CHARS + 1)

        async def aclose(self):
            self.closed = True

    provider = _ClosableProvider()

    def stream_fn(*args, **kwargs):
        return provider

    stack, response, save_assistant, _ = await _open_stream(stream_fn)
    try:
        chunks = await _consume(response)
    finally:
        stack.close()

    assert provider.closed is True
    assert any(chat_api._CHAT_OUTPUT_LIMIT_EXCEEDED in chunk for chunk in chunks)
    assert not any('"type": "done"' in chunk for chunk in chunks)
    save_assistant.assert_awaited_once()
    assert save_assistant.await_args.kwargs["content"] == ""
    assert save_assistant.await_args.kwargs["status"] == "failed"
    assert save_assistant.await_args.kwargs["error_message"] == chat_api._CHAT_OUTPUT_LIMIT_EXCEEDED


async def test_conversation_messages_query_has_sql_limit():
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Db:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    db = _Db()
    assert await get_conversation_messages(db, uuid.uuid4(), limit=700) == []
    sql = db.statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ).string.lower()
    assert "limit 500" in sql


async def test_conversation_messages_before_cursor_is_in_sql():
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Db:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    db = _Db()
    cursor = uuid.uuid4()
    assert await get_conversation_messages(db, uuid.uuid4(), limit=2, before=cursor) == []
    sql = db.statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ).string.lower()
    assert "created_at" in sql
    assert "limit 2" in sql
    assert str(cursor) in sql


async def test_recent_turns_flattens_database_pairs_chronologically():
    paired_user = SimpleNamespace(id=uuid.uuid4(), content="keep")
    paired_assistant = SimpleNamespace(content="answer")
    result = MagicMock()
    result.all.return_value = [(paired_user, paired_assistant)]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    history = await recent_turns(db, uuid.uuid4(), max_turns=5)

    assert history == [
        {"role": "user", "content": "keep"},
        {"role": "assistant", "content": "answer"},
    ]


async def test_recent_turns_limits_recent_valid_pairs_around_orphans_and_failures():
    conversation_id = uuid.uuid4()
    other_conversation_id = uuid.uuid4()
    messages = []
    valid_pairs = []

    def add_message(*, role, content, conversation=conversation_id, status=None, parent=None):
        message = SimpleNamespace(
            id=uuid.uuid4(), conversation_id=conversation, role=role, content=content,
            status=status, parent_message_id=parent, created_at=len(messages),
        )
        messages.append(message)
        return message

    for index in range(12):
        user = add_message(role="user", content=f"question-{index}")
        orphan = add_message(role="user", content=f"orphan-{index}")
        add_message(
            role="assistant", content="failed answer", status="failed", parent=orphan.id,
        )
        add_message(
            role="assistant", content=" \t", status="success", parent=user.id,
        )
        failed_assistant = messages[-2]
        add_message(
            role="assistant", content="wrong parent", status="success", parent=failed_assistant.id,
        )
        answer = add_message(
            role="assistant", content=f"answer-{index}", status="success", parent=user.id,
        )
        valid_pairs.append((user, answer))

    other_user = add_message(
        role="user", content="other conversation", conversation=other_conversation_id,
    )
    add_message(
        role="assistant", content="cross-conversation", status="success", parent=other_user.id,
    )
    for index in range(30):
        orphan = add_message(role="user", content=f"late-orphan-{index}")
        add_message(
            role="assistant", content="late failure", status="failed", parent=orphan.id,
        )

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class _FakeDb:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            pairs = [
                (user, assistant)
                for user in messages
                if user.conversation_id == conversation_id and user.role == "user"
                for assistant in messages
                if (
                    assistant.conversation_id == conversation_id
                    and assistant.role == "assistant"
                    and assistant.status == "success"
                    and (assistant.content or "").strip()
                    and assistant.parent_message_id == user.id
                )
            ]
            pairs.sort(key=lambda pair: pair[0].created_at, reverse=True)
            return _Result(pairs[:3])

    db = _FakeDb()

    history = await recent_turns(db, conversation_id, max_turns=3)

    assert history == [
        {"role": "user", "content": "question-9"},
        {"role": "assistant", "content": "answer-9"},
        {"role": "user", "content": "question-10"},
        {"role": "assistant", "content": "answer-10"},
        {"role": "user", "content": "question-11"},
        {"role": "assistant", "content": "answer-11"},
    ]
    assert [pair[0].content for pair in valid_pairs[-3:]] == [
        "question-9", "question-10", "question-11",
    ]
    statement = db.statement
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    )
    sql = compiled.string.lower()
    assert "limit" in sql
    assert "status" in sql and "success" in sql
    assert "trim" in sql
    assert "parent_message_id" in sql
