from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import chat as api
from app.config import settings
from app.schemas.chat import ChatMessageRequest
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services import chat_evidence
from app.services.chat_answer import ChatAnswer
from tests.test_chat_api import _history_patches, mock_library, mock_user
from tests.test_chat_graph_augmentation import _evidence
from tests.test_chat_evidence_pg import _locator
from app.services.evidence_locator_projection import chunk_locator_projection


@pytest.fixture
def chat_setup(monkeypatch):
    monkeypatch.setattr(settings, "chat_enabled", True)
    monkeypatch.setattr(settings, "organization_authorization_enabled", False)
    monkeypatch.setattr(api, "load_active_library", AsyncMock(return_value=mock_library))
    mock_user.is_superuser = True
    return AsyncMock()


def test_chat_uses_the_shared_evidence_owner_after_authorization(monkeypatch, chat_setup):
    calls = []
    async def collect(db, library, query, **options):
        calls.append((db, library, query, options))
        request = chat_evidence.DifyRetrievalRequest(knowledge_id=library.slug, query=query)
        response = await options["retrieve"](request)
        return response.records, {"chat_evidence": {"specified_files": 1, "notices": []}}
    monkeypatch.setattr(chat_evidence, "collect_chat_evidence", collect)
    retrieve = AsyncMock(return_value=DifyRetrievalResponse(records=[]))
    monkeypatch.setattr(api, "run_retrieval", retrieve)
    body = ChatMessageRequest(library_slug="medical", query="《Alpha.pdf》的记载")
    asyncio.run(api._retrieve_for_chat(body, mock_user, chat_setup))
    assert len(calls) == 1
    assert calls[0][0] is chat_setup and calls[0][1] is mock_library
    assert calls[0][3]["allowed_document_ids"] is None
    assert retrieve.call_args.kwargs["collection"] == mock_library.qdrant_collection


@pytest.mark.parametrize("stream", [False, True])
def test_file_ambiguity_is_returned_without_calling_a_model(monkeypatch, chat_setup, stream):
    notices = ["文件名称存在歧义，请明确完整文件名。"]
    monkeypatch.setattr(api, "_retrieve_for_chat", AsyncMock(return_value=(mock_library, [],
        {"chat_evidence": {"notices": notices, "clarification_required": True}})))
    generate = AsyncMock(side_effect=AssertionError("Clarification must not call a model"))
    monkeypatch.setattr(api.chat_answer, "generate_answer", generate)
    monkeypatch.setattr(api.chat_answer, "stream_answer", generate)
    async def scenario():
        body = ChatMessageRequest(library_slug="medical", query="对比《年度报告》", use_graph=False)
        with ExitStack() as stack:
            for patcher in _history_patches():
                stack.enter_context(patcher)
            if stream:
                response = await api.chat_stream(body, mock_user, chat_setup)
                events = [json.loads(line.removeprefix("data: ").strip()) async for line in response.body_iterator]
                answer = "".join(event.get("text", "") for event in events if event["type"] == "delta")
                assert events[-1]["type"] == "done"
            else:
                response = await api.chat_messages(body, mock_user, chat_setup)
                answer = response.answer
            assert "歧义" in answer and "文件名" in answer
            assert api.chat_history.save_assistant_message.call_args.kwargs["content"] == answer
    asyncio.run(scenario())
    generate.assert_not_called()


def test_partial_evidence_notes_reach_the_model_call_without_hiding_in_debug(monkeypatch, chat_setup):
    record = DifyRecord(content="已验证的局部事实", title="Alpha.xlsx", score=0.9,
                        metadata={"document_id": "d1", "chunk_id": "c1"})
    notice = "表格覆盖不完整，不得据此作完整合计。"
    monkeypatch.setattr(api, "_retrieve_for_chat", AsyncMock(return_value=(mock_library, [record],
        {"chat_evidence": {"specified_files": 1, "notices": [notice], "clarification_required": False}})))
    generate = AsyncMock(return_value=ChatAnswer(answer="局部事实[1]", used_records=[record]))
    monkeypatch.setattr(api.chat_answer, "generate_answer", generate)
    with ExitStack() as stack:
        for patcher in _history_patches():
            stack.enter_context(patcher)
        response = asyncio.run(api.chat_messages(ChatMessageRequest(library_slug="medical", query="明细合计", use_graph=False), mock_user, chat_setup))
    assert response.answer == "局部事实[1]"
    assert notice in generate.call_args.kwargs["evidence_notice"]
    assert generate.call_args.kwargs["max_context_chars"] <= 12000
    assert response.debug is None


def test_graph_source_content_is_the_actual_context_excerpt():
    evidence = _evidence()
    record = DifyRecord(title="graph", content="实际入模片段", score=1,
                        metadata={"chat_graph_evidence": evidence.model_dump(mode="json")})
    _sources, graphs = api._split_used_records([record])
    assert graphs[0].content == record.content
    assert graphs[0].citation_index == 1


def test_topic_table_scope_keeps_other_permitted_graph_documents_optional():
    record = DifyRecord(title="table", content="body", score=1, metadata={"document_id": "d1"})
    graph = DifyRecord(title="graph", content="body", score=1,
                      metadata={"chat_graph_evidence": _evidence().model_dump(mode="json")})
    assert api._scoped_answer_records([record], [graph], True, document_scoped=False) == [record, graph]
    assert api._scoped_answer_records([record], [graph], True, document_scoped=True) == [record]


def _position_record():
    document_id, revision_id, chunk_id, parent_id = (uuid.uuid4() for _ in range(4))
    text = "验证正文，不从第九页字样推断位置"
    chunk = SimpleNamespace(id=chunk_id, block_id=parent_id, evidence_id=None,
                            chunk_metadata={"evidence_locator_v1": _locator(document_id, revision_id, chunk_id, parent_id, text, 3, 4, "chunk")})
    projection = chunk_locator_projection(chunk, document_id=document_id, document_revision_id=revision_id, revision_no=3)
    return DifyRecord(title="Alpha.xlsx", content=text, score=1,
        metadata={"document_id": str(document_id), "chunk_id": str(chunk_id), "document_revision_id": str(revision_id),
                  "document_revision": 7, "document_revision_no": 3, "evidence_locator_v1_projection": projection})


def test_source_identity_position_and_citation_index_come_from_bound_projection():
    record = _position_record()
    sources, _graphs = api._split_used_records([record])
    source = sources[0]
    assert source.citation_index == 1
    assert source.document_revision_id == record.metadata["document_revision_id"]
    assert source.revision_no == 3
    assert source.location["row"] == {"start": 3, "end": 4}
    assert source.location["sheet"] == {"name": "SheetA"}


def test_forged_projection_and_body_page_numbers_do_not_produce_positions():
    record = _position_record()
    record.metadata["evidence_locator_v1_projection"]["chunk_id"] = str(uuid.uuid4())
    source = api._to_source(record)
    assert source.location is None


def test_context_exposes_verified_sheet_rows_but_does_not_guess_from_body():
    record = _position_record()
    context, used = api.chat_answer.build_context([record], 1000)
    assert "SheetA" in context.split("\n", 1)[0]
    assert "3–4" in context.split("\n", 1)[0]
    assert used[0].content == record.content
    record.metadata.pop("evidence_locator_v1_projection")
    context, _used = api.chat_answer.build_context([record], 1000)
    assert "第九页" not in context.split("\n", 1)[0]


def test_ordinary_sources_keep_real_indexes_among_graph_evidence():
    ordinary = _position_record()
    graph = DifyRecord(title="graph", content="graph", score=1,
                      metadata={"chat_graph_evidence": _evidence().model_dump(mode="json")})
    sources, graphs = api._split_used_records([ordinary, graph, ordinary])
    assert [source.citation_index for source in sources] == [1, 3]
    assert graphs[0].citation_index == 2


def test_verified_calculation_reaches_both_prompt_scope_and_reserved_context_budget(monkeypatch):
    monkeypatch.setattr(settings, "chat_max_context_chars", 12000)
    debug = {"chat_evidence": {"notices": [], "calculation_context": "费用合计=0.3[1]", "context_chars": 11800}}
    notice, clarification, scoped, _document_scoped = api._evidence_scope(debug)
    assert "费用合计=0.3" in notice and not clarification and scoped
    assert api._answer_context_chars(debug, 5000) == 11800
