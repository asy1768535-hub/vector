"""Real XLSX parser snapshots, isolated PostgreSQL, real Chat HTTP contract."""
from __future__ import annotations

import asyncio
import uuid
from io import BytesIO

import openpyxl
import pytest
from sqlalchemy import delete

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services import chat_answer, chat_evidence
from app.services.evidence_write_path import _structural_payload
from app.services.splitter import _segment_flat_text
from app.services.xlsx_extract import extract_xlsx_segments
from tests import test_chat_evidence_pg as entry
from tests.test_rebuild_revision_pg import _DSN, _database, _seed

pytestmark = pytest.mark.skipif(not _DSN, reason="Needs disposable loopback PostgreSQL")


async def _seed_calculation_table(engine, factory):
    async with engine.begin() as connection:
        await connection.run_sync(DocumentBlock.__table__.create)
    lib_id, doc_id, current_id, latest_id = await _seed(factory)
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "SheetA"
    for row in (["对象", "数量(件)", "费用(元)"], ["甲", 2, 0.1], ["乙", 4, 0.2]):
        worksheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    segment = extract_xlsx_segments(buffer.getvalue())[0]
    parent_id = uuid.uuid4()
    parent_text = _segment_flat_text(segment)
    texts = [_segment_flat_text({**segment, "rows": segment["rows"][:1]})] + [
        _segment_flat_text({**segment, "rows": [segment["rows"][0], line]}) for line in segment["rows"][1:]]
    ids = [uuid.uuid4() for _text in texts]
    parent_locator = entry._locator(doc_id, current_id, parent_id, None, parent_text, 1, 3, "table")
    blocks = [DocumentBlock(id=parent_id, library_id=lib_id, document_id=doc_id, document_revision_id=current_id,
        seq=0, block_kind="table", text=parent_text, content={"evidence_locator_v1": parent_locator,
            "parser_unit": _structural_payload({"unit_key": segment["unit_key"], "unit_kind": "table", "source_kind": "xlsx"}, segment)},
        parser_name=segment["parser"]["name"], parser_version="1")]
    for index, unit in enumerate(segment["structured_units"], 1):
        cell_id = uuid.uuid4()
        row = unit["source"]["row"]["start"]
        value = unit.get("value")
        text = str(value)
        locator = entry._locator(doc_id, current_id, cell_id, parent_id, text, row, row, "cell")
        locator["source"]["column"] = unit["source"]["column"]
        blocks.append(DocumentBlock(id=cell_id, library_id=lib_id, document_id=doc_id, document_revision_id=current_id,
            parent_block_id=parent_id, seq=index, block_kind="cell", text=text,
            content={"evidence_locator_v1": locator, "parser_unit": _structural_payload(unit, segment)},
            parser_name=segment["parser"]["name"], parser_version="1"))
    async with factory() as db:
        (await db.get(Document, doc_id)).title = "Alpha台账.xlsx"
        (await db.get(DocumentRevision, current_id)).title = "Alpha台账.xlsx"
        await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        db.add_all(blocks)
        db.add_all([Chunk(id=chunk_id, library_id=lib_id, document_id=doc_id, document_revision_id=current_id,
            block_id=parent_id, seq=index, text=text, chunk_metadata={"evidence_locator_v1":
                entry._locator(doc_id, current_id, chunk_id, parent_id, text, index + 1, index + 1, "chunk")})
            for index, (chunk_id, text) in enumerate(zip(ids, texts))])
        await db.commit()
    return lib_id, doc_id, current_id, latest_id, ids, texts


def test_actual_parser_cells_are_calculated_only_when_the_whole_group_is_used():
    async def scenario():
        async with _database() as (engine, factory):
            lib_id, doc_id, current_id, _, ids, texts = await _seed_calculation_table(engine, factory)
            calls = []
            async def retrieve(request):
                calls.append(request)
                return DifyRetrievalResponse(records=[DifyRecord(content="untrusted", score=1,
                    metadata={"document_id": str(doc_id), "chunk_id": str(ids[-1]), "document_revision_id": str(current_id)})])
            async with factory() as db:
                records, debug = await chat_evidence.collect_chat_evidence(db, await db.get(Library, lib_id),
                    "《Alpha台账.xlsx》各字段数量与费用合计", top_k=1, max_context_chars=12000, retrieve=retrieve)
                assert len(calls) == 1
                assert [record.content for record in records] == texts
                scope = debug["chat_evidence"]
                assert not scope["notices"]
                assert "合计=6" in scope["calculation_context"] and "合计=0.3" in scope["calculation_context"]
                assert "[1][2][3]" in scope["calculation_context"]
                context, used = chat_answer.build_context(records, scope["context_chars"])
                assert len(used) == 3 and len(context) + len(scope["calculation_context"]) <= 12000
                tiny_records, tiny_debug = await chat_evidence.collect_chat_evidence(db, await db.get(Library, lib_id),
                    "《Alpha台账.xlsx》数量合计", top_k=1, max_context_chars=30, retrieve=retrieve)
                assert not tiny_records and not tiny_debug["chat_evidence"]["calculation_context"]
                assert tiny_debug["chat_evidence"]["notices"]
    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_real_http_generation_gets_calculations_and_keeps_sources_unchanged(monkeypatch, stream):
    monkeypatch.setattr(entry, "_seed_table", _seed_calculation_table)
    original = chat_answer._messages
    def messages(query, context, history=None, *, evidence_notice=""):
        assert "合计=6" in evidence_notice and "合计=0.3" in evidence_notice
        assert len(context) + len(evidence_notice) <= 12000
        result = original(query, context, history, evidence_notice=evidence_notice)
        assert "合计=0.3" in result[0]["content"]
        return result
    monkeypatch.setattr(chat_answer, "_messages", messages)
    entry.test_pg_real_http_context_and_persisted_sources_agree(monkeypatch, stream, False)
