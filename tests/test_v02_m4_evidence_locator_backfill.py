from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
import pytest

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.evidence_unit import EvidenceUnit
from app.schemas.evidence_locator import sha256_text
from app.services.evidence_locator_backfill import (
    BACKFILL_MARKER_KEY,
    RevisionCursor,
    backfill_evidence_locators,
    decode_cursor,
    encode_cursor,
    rollback_evidence_locators,
)


LIBRARY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
REVISION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
BLOCK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")
CHUNK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b5")
EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b6")
FILE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b7")
RAW_HASH = "a" * 64
TEXT = "alpha"


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Savepoint:
    def __init__(self):
        self.is_active = True
        self.rollback_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.is_active = False
        return False

    async def rollback(self):
        self.rollback_count += 1
        self.is_active = False


class _Db:
    def __init__(
        self,
        revision,
        *,
        revision_file=True,
        revision_results=None,
        fail_flush=False,
        fail_execute_at=None,
    ):
        self.revision = revision
        self.revision_file = _file(revision) if revision_file else None
        self.rows = _rows(revision)
        self.revision_results = list(revision_results or [[revision]])
        self.flush_count = 0
        self.commit_count = 0
        self.savepoint_count = 0
        self.fail_flush = fail_flush
        self.execute_count = 0
        self.fail_execute_at = fail_execute_at
        self.statements = []

    async def execute(self, statement):
        self.execute_count += 1
        self.statements.append(statement)
        if self.execute_count == self.fail_execute_at:
            raise RuntimeError("fixture query failed")
        entity = statement.column_descriptions[0]["entity"]
        if entity is DocumentRevision:
            return _Result(self.revision_results.pop(0) if self.revision_results else [])
        if entity is DocumentRevisionFile:
            return _Result([self.revision_file] if self.revision_file else [])
        return _Result(self.rows.get(entity, []))

    def begin_nested(self):
        self.savepoint_count += 1
        self.savepoint = _Savepoint()
        return self.savepoint

    async def flush(self):
        self.flush_count += 1
        if self.fail_flush:
            self.fail_flush = False
            raise RuntimeError("database flush failed")

    async def commit(self):
        self.commit_count += 1


def _revision(revision_id=REVISION_ID, *, created_at=None):
    return DocumentRevision(
        id=revision_id,
        document_id=DOCUMENT_ID,
        library_id=LIBRARY_ID,
        revision_no=1,
        title="fixture.pdf",
        content_hash=sha256_text(TEXT),
        normalized_text=TEXT,
        parser_name="fixture-parser",
        parser_version="v1",
        parser_config={"mode": "fixture"},
        chunking_strategy="text",
        chunking_strategy_version="v1",
        status="ready",
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _file(revision):
    return DocumentRevisionFile(
        id=FILE_ID,
        document_revision_id=revision.id,
        document_id=DOCUMENT_ID,
        library_id=LIBRARY_ID,
        file_name="fixture.pdf",
        sha256=RAW_HASH,
    )


def _rows(revision):
    block = DocumentBlock(
        id=BLOCK_ID,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=revision.id,
        seq=0,
        block_kind="paragraph",
        text=TEXT,
        source_start=0,
        source_end=len(TEXT),
        page_start=1,
        page_end=1,
        position={"type": "page", "page": 1},
        content={"legacy": "preserve"},
        parser_name="fixture-parser",
        parser_version="v1",
    )
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        library_id=LIBRARY_ID,
        document_revision_id=revision.id,
        block_id=BLOCK_ID,
        seq=0,
        chunk_kind="text",
        text=TEXT,
        token_count=1,
        source_start=0,
        source_end=len(TEXT),
        page_start=1,
        page_end=1,
        position={"type": "page", "page": 1},
        chunk_metadata={"legacy": "preserve"},
    )
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=revision.id,
        document_block_id=BLOCK_ID,
        evidence_kind="chunk",
        source_start=0,
        source_end=len(TEXT),
        page_start=1,
        page_end=1,
        position={"type": "page", "page": 1},
        text_quote=TEXT,
        text_quote_hash=sha256_text(TEXT),
        evidence_metadata={"legacy": "preserve"},
        status="active",
    )
    return {
        DocumentBlock: [block],
        Chunk: [chunk],
        EvidenceUnit: [evidence],
    }


def _run(db, **kwargs):
    return asyncio.run(backfill_evidence_locators(db, **kwargs))


def test_dry_run_is_bounded_and_has_zero_mutation():
    revision = _revision()
    db = _Db(revision)
    result = _run(db, batch_size=1, run_id="run-dry")

    assert (result.scanned, result.updated, result.failed) == (3, 3, 0)
    assert db.flush_count == 0
    assert db.savepoint_count == 1
    assert db.rows[DocumentBlock][0].content == {"legacy": "preserve"}
    assert result.run_id == "run-dry"


def test_apply_is_verified_additive_and_idempotent():
    revision = _revision()
    db = _Db(revision, revision_results=[[revision], [revision]])
    first = _run(db, apply=True, run_id="run-apply")
    assert (first.updated, first.failed) == (3, 0)
    assert db.commit_count == 1
    for model, field in (
        (DocumentBlock, "content"),
        (Chunk, "chunk_metadata"),
        (EvidenceUnit, "evidence_metadata"),
    ):
        metadata = getattr(db.rows[model][0], field)
        assert metadata["legacy"] == "preserve"
        assert metadata["evidence_locator_v1"]["provenance_status"] == "verified"
        assert metadata["evidence_locator_v1"]["unit_id"] == str(db.rows[model][0].id)
        assert BACKFILL_MARKER_KEY in metadata

    second = _run(db, apply=True, run_id="run-apply")
    assert (second.updated, second.skipped, second.failed) == (0, 3, 0)


def test_missing_file_is_explicitly_legacy_unverified():
    revision = _revision()
    db = _Db(revision, revision_file=False)
    result = _run(db, apply=True, run_id="run-legacy")
    assert result.updated == 3
    assert db.rows[Chunk][0].chunk_metadata["evidence_locator_v1"]["provenance_status"] == "legacy_unverified"
    assert "document_revision_file_id" not in db.rows[Chunk][0].chunk_metadata["evidence_locator_v1"]


def test_existing_conflict_aborts_only_revision_without_partial_updates():
    revision = _revision()
    db = _Db(revision)
    db.rows[Chunk][0].chunk_metadata = {"evidence_locator_v1": {"bad": True}}
    result = _run(db, apply=True, run_id="run-conflict")

    assert result.failed == 1
    assert result.reason_counts == {"existing_locator_invalid": 1}
    assert db.flush_count == 0
    assert "evidence_locator_v1" not in (db.rows[DocumentBlock][0].content or {})


def test_existing_hash_conflict_is_not_overwritten():
    revision = _revision()
    db = _Db(revision)
    db.rows[Chunk][0].chunk_metadata = {
        "evidence_locator_v1": {
            "document_id": str(DOCUMENT_ID),
            "document_revision_id": str(revision.id),
            "revision_no": 1,
            "unit_id": str(CHUNK_ID),
            "parent_unit_id": str(BLOCK_ID),
            "unit_kind": "chunk",
            "ordinal": 1,
            "parser": {"name": "fixture", "version": "v1"},
            "source": {
                "kind": "pdf",
                "text": {
                    "start": 0,
                    "end": len(TEXT),
                    "ranges": [{
                        "start": 0,
                        "end": len(TEXT),
                        "sha256": sha256_text(TEXT),
                    }],
                },
            },
            "normalized_content_hash": "b" * 64,
            "unit_text_sha256": sha256_text(TEXT),
            "quote_sha256": sha256_text(TEXT),
            "provenance_status": "legacy_unverified",
        }
    }
    result = _run(db, apply=True, run_id="run-hash-conflict")

    assert result.failed == 1
    assert result.reason_counts == {"existing_locator_content_hash_conflict": 1}
    assert db.flush_count == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda locator: (
                locator["source"]["text"].update(start=1, end=4),
                locator["source"]["text"]["ranges"][0].update(
                    start=1, end=4, sha256=sha256_text("lph")
                ),
            ),
            "existing_locator_source_range_conflict",
        ),
        (
            lambda locator: locator["source"]["text"]["ranges"][0].update(
                sha256="b" * 64
            ),
            "existing_locator_source_hash_conflict",
        ),
    ],
)
def test_existing_locator_source_range_or_hash_conflict_is_not_skipped(mutation, reason):
    revision = _revision()
    db = _Db(revision, revision_results=[[revision], [revision]])
    _run(db, apply=True, run_id="run-source-seed")
    locator = db.rows[Chunk][0].chunk_metadata["evidence_locator_v1"]
    mutation(locator)

    result = _run(db, apply=True, run_id="run-source-recheck")

    assert result.failed == 1
    assert result.reason_counts == {reason: 1}
    assert db.flush_count == 1


def test_normalized_content_hash_mismatch_never_marks_locator_verified():
    revision = _revision()
    revision.content_hash = "b" * 64
    db = _Db(revision)

    result = _run(db, apply=True, run_id="run-bad-content-hash")

    assert result.updated == 3
    assert result.failed == 0
    for model, field in (
        (DocumentBlock, "content"),
        (Chunk, "chunk_metadata"),
        (EvidenceUnit, "evidence_metadata"),
    ):
        locator = getattr(db.rows[model][0], field)["evidence_locator_v1"]
        assert locator["provenance_status"] == "legacy_unverified"
        assert "normalized_content_hash" not in locator


def test_flush_failure_is_isolated_and_restores_in_memory_metadata():
    revision = _revision()
    db = _Db(revision, fail_flush=True)
    result = _run(db, apply=True, run_id="run-flush-failure")

    assert result.failed == 1
    assert result.reason_counts == {"revision_processing_failed": 1}
    assert db.rows[DocumentBlock][0].content == {"legacy": "preserve"}
    assert db.rows[Chunk][0].chunk_metadata == {"legacy": "preserve"}
    assert db.savepoint.rollback_count == 1


def test_parent_order_uses_block_seq_not_cross_type_locator_ordinal():
    revision = _revision()
    db = _Db(revision)
    parent = DocumentBlock(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000c4"),
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=revision.id,
        seq=10,
        block_kind="section",
        text="parent",
        parser_name="fixture",
        parser_version="v1",
    )
    child = db.rows[DocumentBlock][0]
    child.seq = 20
    child.parent_block_id = parent.id
    db.rows[DocumentBlock] = [parent, child]

    result = _run(db, apply=True, run_id="run-parent-seq")

    assert result.failed == 0
    assert result.updated == 4


def test_parent_revision_mismatch_is_rejected_before_mutation():
    revision = _revision()
    db = _Db(revision)
    parent = DocumentBlock(
        id=uuid.uuid4(), library_id=LIBRARY_ID, document_id=DOCUMENT_ID,
        document_revision_id=uuid.uuid4(), seq=0, block_kind="section",
        text="parent", parser_name="fixture", parser_version="v1",
    )
    child = db.rows[DocumentBlock][0]
    child.parent_block_id = parent.id
    db.rows[DocumentBlock] = [parent, child]
    result = _run(db, apply=True, run_id="run-parent")

    assert result.failed == 1
    assert result.reason_counts == {"row_scope_mismatch": 1}
    assert db.flush_count == 0
    assert all(
        not isinstance(row.content, dict) or "evidence_locator_v1" not in row.content
        for row in db.rows[DocumentBlock]
    )


def test_cursor_round_trip_and_resume_are_stable():
    first_revision = _revision(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second_revision = _revision(
        revision_id=uuid.UUID("00000000-0000-0000-0000-0000000000c3"),
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    db = _Db(first_revision, revision_results=[[first_revision], [second_revision]])
    first = _run(db, batch_size=1, max_revisions=1, run_id="run-cursor")
    cursor = decode_cursor(first.next_cursor)
    assert cursor == RevisionCursor(first_revision.created_at, first_revision.id)

    second = _run(db, batch_size=1, max_revisions=1, cursor=first.next_cursor, run_id="run-cursor")
    assert second.next_cursor is not None
    assert decode_cursor(second.next_cursor).revision_id == second_revision.id
    encoded = encode_cursor(cursor)
    assert decode_cursor(encoded) == cursor


def test_rollback_requires_marker_identity_and_preserves_other_metadata():
    revision = _revision()
    db = _Db(revision, revision_results=[[revision], [revision], [revision]])
    _run(db, apply=True, run_id="run-rollback")

    dry = asyncio.run(
        rollback_evidence_locators(db, run_id="run-rollback", batch_size=1)
    )
    assert (dry.candidates, dry.removed) == (3, 0)
    assert "evidence_locator_v1" in db.rows[Chunk][0].chunk_metadata

    applied = asyncio.run(
        rollback_evidence_locators(db, run_id="run-rollback", batch_size=1, apply=True)
    )
    assert (applied.candidates, applied.removed, applied.refused) == (3, 3, 0)
    assert db.rows[Chunk][0].chunk_metadata == {"legacy": "preserve"}


def test_rollback_rejects_wrong_run_identity():
    revision = _revision()
    db = _Db(revision, revision_results=[[revision], [revision]])
    _run(db, apply=True, run_id="run-owned")

    result = asyncio.run(
        rollback_evidence_locators(db, run_id="run-other", batch_size=1)
    )

    assert result.candidates == 0
    assert result.removed == 0
    assert result.refused == 3
    assert result.reason_counts == {"marker_run_mismatch": 3}
    assert "evidence_locator_v1" in db.rows[Chunk][0].chunk_metadata


def test_superseded_revision_can_be_guarded_rollback():
    revision = _revision()
    db = _Db(revision, revision_results=[[revision], [revision]])
    _run(db, apply=True, run_id="run-superseded")
    revision.status = "superseded"

    result = asyncio.run(
        rollback_evidence_locators(db, run_id="run-superseded", batch_size=1, apply=True)
    )

    assert (result.candidates, result.removed, result.refused) == (3, 3, 0)
    compiled = [
        str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in db.statements
    ]
    assert any("superseded" in statement for statement in compiled)


def test_rollback_query_failure_isolated_to_one_revision():
    first = _revision()
    db = _Db(first, revision_results=[[first]])
    _run(db, apply=True, run_id="run-isolated")
    second = _revision(
        revision_id=uuid.UUID("00000000-0000-0000-0000-0000000000c8"),
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    db.revision_results = [[first, second]]
    db.execute_count = 0
    db.fail_execute_at = 7

    result = asyncio.run(
        rollback_evidence_locators(db, run_id="run-isolated", batch_size=2, apply=True)
    )

    assert result.removed == 3
    assert result.refused == 1
    assert result.reason_counts == {"rollback_revision_failed": 1}
    assert "evidence_locator_v1" not in db.rows[Chunk][0].chunk_metadata


def test_invalid_limits_and_run_id_are_rejected():
    revision = _revision()
    db = _Db(revision)
    with pytest.raises(ValueError):
        _run(db, batch_size=101)
    with pytest.raises(ValueError):
        _run(db, run_id="x" * 129)
