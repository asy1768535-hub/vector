"""Synthetic Chat evidence in isolated schemas of a loopback *_test database."""
from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest
from sqlalchemy import delete

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services.chat_evidence import collect_chat_evidence
from tests.test_rebuild_revision_pg import _database, _seed, _DSN

pytestmark = pytest.mark.skipif(not _DSN, reason="Needs disposable local PostgreSQL")


def _locator(document_id, revision_id, unit_id, parent_id, text, row_start, row_end, kind):
    return {
        "locator_version": "v1", "document_id": str(document_id), "document_revision_id": str(revision_id),
        "revision_no": 3, "unit_id": str(unit_id), "parent_unit_id": str(parent_id) if parent_id else None,
        "unit_kind": kind, "ordinal": row_start,
        "parser": {"name": "test", "version": "1"},
        "source": {"kind": "xlsx", "file_name": "Alpha台账.xlsx", "sheet": {"name": "SheetA"}, "row": {"start": row_start, "end": row_end}},
        "unit_text_sha256": sha256(text.encode()).hexdigest(), "quote_sha256": sha256(text.encode()).hexdigest(),
        "provenance_status": "legacy_unverified",
    }


async def _seed_table(engine, factory, count=3):
    import uuid

    async with engine.begin() as connection:
        await connection.run_sync(DocumentBlock.__table__.create)
    library_id, document_id, current_id, latest_id = await _seed(factory)
    ids = [uuid.uuid4() for _ in range(count)]
    parent_id = uuid.uuid4()
    texts = [f"字段\t数量\n测试项{index}\t{index + 1}" for index in range(count)]
    parent_text = "\n".join(texts)
    async with factory() as db:
        doc = await db.get(Document, document_id)
        doc.title = "Alpha台账.xlsx"
        current = await db.get(DocumentRevision, current_id)
        current.title = "Alpha台账.xlsx"
        await db.execute(delete(Chunk).where(Chunk.document_id == document_id))
        parent_raw = _locator(document_id, current_id, parent_id, None, parent_text, 1, count * 2, "table")
        db.add(DocumentBlock(id=parent_id, library_id=library_id, document_id=document_id,
            document_revision_id=current_id, block_kind="table", seq=0, text=parent_text,
            content={"evidence_locator_v1": parent_raw}, parser_name="test", parser_version="1"))
        db.add_all([Chunk(id=chunk_id, library_id=library_id, document_id=document_id,
            document_revision_id=current_id, block_id=parent_id, seq=index, text=body,
            chunk_metadata={"evidence_locator_v1": _locator(document_id, current_id, chunk_id, parent_id, body, index * 2 + 1, index * 2 + 2, "chunk")})
            for index, (chunk_id, body) in enumerate(zip(ids, texts))])
        await db.commit()
    return library_id, document_id, current_id, latest_id, ids, texts


def test_pg_scoped_retrieval_expands_real_current_group_in_original_order():
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, texts = await _seed_table(engine, factory)
            calls = []

            async def retrieve(request):
                calls.append(request)
                return DifyRetrievalResponse(records=[DifyRecord(content="untrusted returned body", title="wrong file",
                    score=0.9, metadata={"document_id": str(doc_id), "chunk_id": str(ids[-1]), "document_revision_id": str(current_id), "document_revision": 7})])

            async with factory() as db:
                lib = await db.get(Library, lib_id)
                records, debug = await collect_chat_evidence(db, lib, "《Alpha台账.xlsx》的数量合计", top_k=1,
                    max_context_chars=12000, retrieve=retrieve)
                assert len(calls) == 1
                assert calls[0].metadata_condition.conditions[0].value == [str(doc_id)]
                assert [record.content for record in records] == texts
                assert [record.metadata["chunk_id"] for record in records] == [str(value) for value in ids]
                assert all(record.title == "Alpha台账.xlsx" for record in records)
                assert all(record.metadata["document_revision_no"] == 3 for record in records)
                assert records[0].metadata["chat_evidence_origin"] == "table_expansion"
                assert records[-1].metadata["chat_evidence_origin"] == "retrieval"
                assert len(debug["chat_evidence"]["notices"]) == 1 and "单元格快照" in debug["chat_evidence"]["notices"][0]
                assert not debug["chat_evidence"]["calculation_context"]
    asyncio.run(scenario())


def test_pg_pending_revision_seed_is_excluded():
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, _, latest_id, ids, _ = await _seed_table(engine, factory)
            async def retrieve(request):
                return DifyRetrievalResponse(records=[DifyRecord(content="pending", score=1,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[0]), "document_revision_id": str(latest_id)})])
            async with factory() as db:
                records, debug = await collect_chat_evidence(db, await db.get(Library, lib_id), "《Alpha台账.xlsx》的明细",
                    top_k=5, max_context_chars=12000, retrieve=retrieve)
                assert not records
                assert debug["chat_evidence"]["notices"]
    asyncio.run(scenario())


def test_pg_group_over_limit_is_not_claimed_complete():
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, _ = await _seed_table(engine, factory, 9)
            async def retrieve(request):
                return DifyRetrievalResponse(records=[DifyRecord(content="seed", score=1,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[4]), "document_revision_id": str(current_id)})])
            async with factory() as db:
                records, debug = await collect_chat_evidence(db, await db.get(Library, lib_id), "《Alpha台账.xlsx》的明细合计",
                    top_k=1, max_context_chars=12000, retrieve=retrieve)
                assert len(records) == 1
                assert any("八" in note for note in debug["chat_evidence"]["notices"])
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["pending_title", "missing_row"])
def test_pg_published_title_is_used_and_missing_rows_are_explicit(change):
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, texts = await _seed_table(engine, factory)
            async with factory() as db:
                if change == "pending_title":
                    (await db.get(Document, doc_id)).title = "Unpublished new filename.docx"
                else:
                    await db.execute(delete(Chunk).where(Chunk.id == ids[1]))
                await db.commit()
            async def retrieve(request):
                return DifyRetrievalResponse(records=[DifyRecord(content="seed", score=1,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[-1]), "document_revision_id": str(current_id)})])
            async with factory() as db:
                records, debug = await collect_chat_evidence(db, await db.get(Library, lib_id), "《Alpha台账.xlsx》的明细合计",
                    top_k=1, max_context_chars=12000, retrieve=retrieve)
                assert records
                if change == "pending_title":
                    assert [record.content for record in records] == texts
                    assert len(debug["chat_evidence"]["notices"]) == 1 and "单元格快照" in debug["chat_evidence"]["notices"][0]
                else:
                    assert len(records) == 2
                    assert any("覆盖" in note for note in debug["chat_evidence"]["notices"])
    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("invalid_citation", [False, True])
def test_pg_real_http_context_and_persisted_sources_agree(monkeypatch, stream, invalid_citation):
    import json
    import uuid
    import httpx
    from sqlalchemy import select

    from app.api import chat as api
    from app.auth.backend import current_active_user
    from app.config import settings
    from app.db import get_db
    from app.main import app
    from app.models.chat_history import ChatConversation, ChatMessage, ChatMessageSource
    from app.models.user import User
    from app.services import chat_answer

    monkeypatch.setattr(settings, "chat_enabled", True)
    monkeypatch.setattr(settings, "organization_authorization_enabled", False)
    monkeypatch.setattr(settings, "chat_base_url", "http://provider.invalid/v1")
    monkeypatch.setattr(settings, "chat_api_key", "")
    monkeypatch.setattr(settings, "chat_model", "synthetic")
    monkeypatch.setattr(settings, "chat_history_max_turns", 0)
    monkeypatch.setattr(settings, "chat_max_context_chars", 12000)

    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, texts = await _seed_table(engine, factory)
            async with engine.begin() as connection:
                await connection.run_sync(lambda conn: ChatConversation.__table__.create(conn))
                await connection.run_sync(lambda conn: ChatMessage.__table__.create(conn))
                await connection.run_sync(lambda conn: ChatMessageSource.__table__.create(conn))
            user = User(id=uuid.uuid4(), email="synthetic@example.test", hashed_password="unused-test-hash", is_superuser=True, is_active=True)
            async with factory() as db:
                db.add(user)
                await db.commit()
                library = await db.get(Library, lib_id)
                slug = library.slug
            retrieval_calls = []
            async def retrieve(**kwargs):
                retrieval_calls.append(kwargs["request"])
                return DifyRetrievalResponse(records=[DifyRecord(content="wrong provider text", title="wrong provider title", score=0.9,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[-1]), "document_revision_id": str(current_id), "document_revision": 7})])
            monkeypatch.setattr(api, "run_retrieval", retrieve)
            monkeypatch.setattr(api, "async_session_factory", factory)
            async def user_dependency():
                return user
            async def db_dependency():
                async with factory() as db:
                    yield db
            previous = app.dependency_overrides.copy()
            app.dependency_overrides[current_active_user] = user_dependency
            app.dependency_overrides[get_db] = db_dependency
            provider_payloads = []
            def provider(request):
                payload = json.loads(request.content)
                provider_payloads.append(payload)
                if payload["stream"]:
                    body = "".join("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\n\n"
                                   for text in ("数量为六[", "99]" if invalid_citation else "1][2][3]")) + "data: [DONE]\n\n"
                    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={"choices": [{"message": {"content": "数量为六[99]" if invalid_citation else "数量为六[1][2][3]"}}]})
            original_client = httpx.AsyncClient
            http_client = original_client(transport=httpx.ASGITransport(app=app), base_url="http://test")
            monkeypatch.setattr(chat_answer.httpx, "AsyncClient", lambda *args, **kwargs: original_client(*args, transport=httpx.MockTransport(provider), **kwargs))
            try:
                async with http_client:
                    response = await http_client.post("/chat/stream" if stream else "/chat/messages",
                        json={"library_slug": slug, "query": "《Alpha台账.xlsx》的数量合计", "top_k": 1, "use_graph": False})
                    if invalid_citation:
                        assert len(provider_payloads) == len(retrieval_calls) == 1
                        if stream:
                            assert response.status_code == 200
                            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                            assert events[-1]["type"] == "error"
                            assert "[99]" not in "".join(event.get("text", "") for event in events if event["type"] == "delta")
                        else:
                            assert response.status_code == 502
                            assert "chat_invalid_citation" in response.text
                        async with factory() as db:
                            saved = (await db.execute(select(ChatMessage).where(ChatMessage.role == "assistant"))).scalar_one()
                            assert saved.status == "failed" and "chat_invalid_citation" in saved.error_message
                        return
                    assert response.status_code == 200, response.text
                    if stream:
                        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                        sources = events[0]["sources"]
                        conv_id = events[0]["conversation_id"]
                        answer = "".join(event.get("text", "") for event in events if event["type"] == "delta")
                        assert events[-1]["type"] == "done"
                    else:
                        body = response.json()
                        sources, conv_id, answer = body["sources"], body["conversation_id"], body["answer"]
                    assert len(retrieval_calls) == 1
                    assert len(provider_payloads) == 1
                    prompt = provider_payloads[0]["messages"][-1]["content"]
                    assert all(text in prompt for text in texts)
                    assert "wrong provider" not in prompt
                    assert [source["content"] for source in sources] == texts
                    assert [source["chunk_id"] for source in sources] == [str(value) for value in ids]
                    assert [source["citation_index"] for source in sources] == [1, 2, 3]
                    assert all(source["revision_no"] == 3 and source["document_revision_id"] == str(current_id) for source in sources)
                    assert all(source["location"]["sheet"]["name"] == "SheetA" for source in sources)
                    history = await http_client.get(f"/chat/conversations/{conv_id}/messages")
                    assert history.status_code == 200, history.text
                    assistant = next(message for message in history.json() if message["role"] == "assistant")
                    assert [source["citation_index"] for source in assistant["sources"]] == [1, 2, 3]
                    assert all(source["location"] is None for source in assistant["sources"])
                    assert assistant["content"] == answer == "数量为六[1][2][3]"
                    assert [(source["document_id"], source["chunk_id"], source["content"]) for source in assistant["sources"]] == [
                        (source["document_id"], source["chunk_id"], source["content"]) for source in sources]
            finally:
                app.dependency_overrides.clear()
                app.dependency_overrides.update(previous)
    asyncio.run(scenario())


def test_pg_topic_table_query_also_expands_the_trusted_group():
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, texts = await _seed_table(engine, factory)
            calls = []
            async def retrieve(request):
                calls.append(request)
                return DifyRetrievalResponse(records=[DifyRecord(content="seed", score=1,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[1]), "document_revision_id": str(current_id)})])
            async with factory() as db:
                records, debug = await collect_chat_evidence(db, await db.get(Library, lib_id), "测试项的数量合计",
                    top_k=1, max_context_chars=12000, retrieve=retrieve)
                assert len(calls) == 1 and calls[0].metadata_condition is None
                assert [record.content for record in records] == texts
                assert len(debug["chat_evidence"]["notices"]) == 1 and "单元格快照" in debug["chat_evidence"]["notices"][0]
                assert debug["chat_evidence"]["specified_files"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["opaque", "outside_folder"])
def test_pg_topic_seed_without_an_owned_document_is_never_used(identity):
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, _ = await _seed_table(engine, factory)
            async def retrieve(request):
                return DifyRetrievalResponse(records=[DifyRecord(content="unbound content", score=1,
                    metadata={"document_id": "opaque" if identity == "opaque" else str(doc_id),
                              "chunk_id": str(ids[0]), "document_revision_id": str(current_id)})])
            async with factory() as db:
                records, debug = await collect_chat_evidence(db, await db.get(Library, lib_id), "测试项的记载",
                    top_k=1, max_context_chars=12000, retrieve=retrieve,
                    allowed_document_ids=[] if identity == "outside_folder" else None)
                assert not records
                assert debug["chat_evidence"]["notices"]
    asyncio.run(scenario())
