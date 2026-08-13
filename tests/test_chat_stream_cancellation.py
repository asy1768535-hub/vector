from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat as chat_api
from app.models.user import User
from app.schemas.chat import ChatMessageRequest
from app.schemas.dify import DifyRecord
from app.services.chat_answer import ChatError
from app.services.chat_history import recent_turns


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


async def test_recent_turns_excludes_unpaired_cancelled_user():
    paired_user_id = uuid.uuid4()
    cancelled_user_id = uuid.uuid4()
    rows = [
        SimpleNamespace(
            id=paired_user_id, role="user", content="keep", parent_message_id=None,
            status=None,
        ),
        SimpleNamespace(
            id=cancelled_user_id, role="user", content="drop", parent_message_id=None,
            status=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(), role="assistant", content="answer", parent_message_id=paired_user_id,
            status="success",
        ),
        SimpleNamespace(
            id=uuid.uuid4(), role="assistant", content="", parent_message_id=cancelled_user_id,
            status="failed",
        ),
    ]
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    history = await recent_turns(db, uuid.uuid4(), max_turns=5)

    assert history == [
        {"role": "user", "content": "keep"},
        {"role": "assistant", "content": "answer"},
    ]
