from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from app.models.chunk import Chunk
from app.models.document import Document
from app.services.evidence_backfill import (
    BackfillBatchResult,
    backfill_v02_m1_batch,
    build_legacy_chunk_backfill_rows,
    build_legacy_revision_backfill_rows,
)


LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DOC_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
CHUNK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")


def _doc() -> Document:
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    return Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Legacy Doc",
        external_id="legacy-1",
        doc_metadata={"department": "legal"},
        content_hash="a" * 64,
        current_revision=3,
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _chunk() -> Chunk:
    return Chunk(
        id=CHUNK_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        seq=7,
        text="legacy chunk text",
        token_count=17,
    )


def test_build_legacy_revision_backfill_rows_sets_current_and_latest_revision_ids():
    rows = build_legacy_revision_backfill_rows(_doc())

    assert rows.revision.library_id == LIB_ID
    assert rows.revision.document_id == DOC_ID
    assert rows.revision.revision_no == 3
    assert rows.revision.title == "Legacy Doc"
    assert rows.revision.document_metadata == {"department": "legal"}
    assert rows.revision.content_hash == "a" * 64
    assert rows.revision.parser_name == "legacy"
    assert rows.revision.parser_version == "v0.2-m1"
    assert rows.revision.chunking_strategy == "legacy"
    assert rows.revision.chunking_strategy_version == "v0.2-m1"
    assert rows.revision.status == "ready"
    assert rows.revision.published_at is not None

    assert rows.document_updates["current_revision_id"] == rows.revision.id
    assert rows.document_updates["latest_revision_id"] == rows.revision.id


def test_build_legacy_revision_backfill_rows_fills_missing_timestamps():
    doc = _doc()
    doc.created_at = None
    doc.updated_at = None

    rows = build_legacy_revision_backfill_rows(doc)

    assert rows.revision.created_at is not None
    assert rows.revision.updated_at is not None
    assert rows.revision.published_at is not None


def test_build_legacy_chunk_backfill_rows_maps_chunk_to_block_evidence_and_links():
    revision_rows = build_legacy_revision_backfill_rows(_doc())
    rows = build_legacy_chunk_backfill_rows(_doc(), revision_rows.revision, _chunk())

    assert rows.block.library_id == LIB_ID
    assert rows.block.document_id == DOC_ID
    assert rows.block.document_revision_id == revision_rows.revision.id
    assert rows.block.seq == 7
    assert rows.block.block_kind == "paragraph"
    assert rows.block.text == "legacy chunk text"
    assert rows.block.parser_name == "legacy"
    assert rows.block.parser_version == "v0.2-m1"

    assert rows.evidence.library_id == LIB_ID
    assert rows.evidence.document_revision_id == revision_rows.revision.id
    assert rows.evidence.document_block_id == rows.block.id
    assert rows.evidence.evidence_kind == "chunk"
    assert rows.evidence.text_quote == "legacy chunk text"
    assert len(rows.evidence.text_quote_hash) == 64

    assert rows.chunk_block.chunk_id == CHUNK_ID
    assert rows.chunk_block.document_block_id == rows.block.id
    assert rows.chunk_block.document_revision_id == revision_rows.revision.id

    assert rows.chunk_evidence.chunk_id == CHUNK_ID
    assert rows.chunk_evidence.evidence_id == rows.evidence.id
    assert rows.chunk_evidence.document_revision_id == revision_rows.revision.id

    assert rows.chunk_updates["document_revision_id"] == revision_rows.revision.id
    assert rows.chunk_updates["block_id"] == rows.block.id
    assert rows.chunk_updates["evidence_id"] == rows.evidence.id
    assert rows.chunk_updates["chunk_kind"] == "text"


def test_legacy_backfill_row_ids_are_deterministic_for_retry():
    doc = _doc()
    chunk = _chunk()

    first_revision = build_legacy_revision_backfill_rows(doc)
    second_revision = build_legacy_revision_backfill_rows(doc)
    assert first_revision.revision.id == second_revision.revision.id

    first_chunk = build_legacy_chunk_backfill_rows(doc, first_revision.revision, chunk)
    second_chunk = build_legacy_chunk_backfill_rows(doc, second_revision.revision, chunk)

    assert first_chunk.block.id == second_chunk.block.id
    assert first_chunk.evidence.id == second_chunk.evidence.id
    assert first_chunk.chunk_block.chunk_id == second_chunk.chunk_block.chunk_id
    assert first_chunk.chunk_evidence.evidence_id == second_chunk.chunk_evidence.evidence_id
    assert first_chunk.evidence.text_quote_hash == second_chunk.evidence.text_quote_hash


@pytest.mark.asyncio
async def test_backfill_batch_rejects_invalid_batch_size():
    db = MagicMock()

    with pytest.raises(ValueError, match="batch_size"):
        await backfill_v02_m1_batch(db, batch_size=0)


def test_backfill_batch_result_shape():
    result = BackfillBatchResult(processed=2, failed=0, exhausted=False)

    assert result.processed == 2
    assert result.failed == 0
    assert result.exhausted is False
