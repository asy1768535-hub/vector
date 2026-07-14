from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import cleanup
from app.services.graph_extraction_purge import (
    GraphExtractionPurgeResult,
    purge_document_graph_extraction_payloads,
    purge_expired_graph_extraction_payloads,
)


NOW = datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc)
LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


class _Result:
    def __init__(self, rows=(), *, rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected purge query"
        return self.results.pop(0)


def test_immediate_document_purge_cancels_first_and_clears_every_payload_class():
    db = FakeDB(
        [
            _Result([JOB_ID]),
            *[_Result(rowcount=1) for _ in range(15)],
        ]
    )

    result = asyncio.run(
        purge_document_graph_extraction_payloads(
            db,
            library_id=LIB_ID,
            document_id=DOC_ID,
            now=NOW,
        )
    )

    assert result.job_count == 1
    assert result.cancelled_job_count == 1
    assert "update extraction_raw_output_attempts" in str(db.statements[1]).lower()
    assert "update graph_extraction_units" in str(db.statements[2]).lower()
    assert "update graph_extraction_jobs" in str(db.statements[3]).lower()
    sql = "\n".join(str(statement).lower() for statement in db.statements[4:])
    for table in (
        "extraction_context_snapshots",
        "extraction_raw_output_attempts",
        "graph_entity_occurrences",
        "graph_relation_occurrences",
        "graph_entity_candidates",
        "graph_relation_candidates",
        "graph_entity_candidate_evidence",
        "graph_relation_candidate_evidence",
        "graph_entity_merge_candidates",
        "graph_extraction_conflicts",
        "graph_extraction_jobs",
    ):
        assert f"update {table}" in sql
    for field in (
        "context_json",
        "context_text",
        "document_metadata",
        "chunk_title_path",
        "block_title_path",
        "effective_title_path",
        "raw_response",
        "parsed_response",
        "parse_error",
        "raw_payload",
        "canonical_name",
        "normalized_name",
        "proposed_aliases",
        "proposed_properties",
        "external_mapping_hints",
        "review_reason",
        "validation_errors",
        "quote_text",
        "resolved_source_span",
        "candidate_matches",
        "details",
        "description",
        "evidence",
        "sensitive_payload_purged_at",
    ):
        assert field in sql
    evidence_statements = [
        statement
        for statement in db.statements
        if "candidate_evidence" in str(statement).lower()
    ]
    assert evidence_statements
    for statement in evidence_statements:
        assert [] in statement.compile().params.values()


def test_immediate_purge_is_idempotent_when_scope_has_no_jobs():
    db = FakeDB([_Result()])
    result = asyncio.run(
        purge_document_graph_extraction_payloads(
            db,
            library_id=LIB_ID,
            document_id=DOC_ID,
            now=NOW,
        )
    )
    assert result == GraphExtractionPurgeResult(0, 0, 0)
    assert len(db.statements) == 1


def test_retention_scan_is_bounded_and_uses_separate_payload_ages(monkeypatch):
    context_only = SimpleNamespace(id=uuid.uuid4(), created_at=NOW - timedelta(days=31))
    full = SimpleNamespace(id=uuid.uuid4(), created_at=NOW - timedelta(days=181))
    db = FakeDB([_Result([context_only, full])])
    expected = GraphExtractionPurgeResult(2, 0, 8)
    with patch(
        "app.services.graph_extraction_purge._purge_job_payloads",
        new=AsyncMock(return_value=expected),
    ) as purge:
        result = asyncio.run(
            purge_expired_graph_extraction_payloads(
                db,
                batch_limit=25,
                now=NOW,
            )
        )

    assert result is expected
    statement = db.statements[0]
    assert statement._limit_clause.value == 25
    assert statement._for_update_arg.skip_locked is True
    assert purge.await_args.kwargs["context_job_ids"] == {
        context_only.id,
        full.id,
    }
    assert purge.await_args.kwargs["attempt_job_ids"] == {
        context_only.id,
        full.id,
    }
    assert purge.await_args.kwargs["candidate_job_ids"] == {full.id}


def test_document_and_revision_cleanup_call_graph_purge_in_the_same_write_path():
    db = AsyncMock()
    library = SimpleNamespace(id=LIB_ID, qdrant_collection="collection")
    revision_id = uuid.uuid4()
    with (
        patch(
            "app.services.graph_evidence.mark_document_graph_evidence_stale",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_evidence.mark_document_revision_graph_evidence_stale",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_purge.purge_document_graph_extraction_payloads",
            new=AsyncMock(),
        ) as purge_document,
        patch(
            "app.services.graph_extraction_purge.purge_revision_graph_extraction_payloads",
            new=AsyncMock(),
        ) as purge_revision,
        patch("app.services.cleanup._enqueue", new=AsyncMock()),
    ):
        asyncio.run(cleanup.enqueue_delete_document(db, library, DOC_ID))
        asyncio.run(
            cleanup.enqueue_delete_document_revision(
                db,
                library,
                DOC_ID,
                revision_id,
            )
        )

    purge_document.assert_awaited_once_with(
        db,
        library_id=LIB_ID,
        document_id=DOC_ID,
    )
    purge_revision.assert_awaited_once_with(
        db,
        library_id=LIB_ID,
        document_revision_id=revision_id,
    )
