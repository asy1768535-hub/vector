from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
CHUNK_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
ENTITY_CANDIDATE_ID = uuid.UUID("70000000-0000-0000-0000-000000000001")
RELATION_CANDIDATE_ID = uuid.UUID("80000000-0000-0000-0000-000000000001")


def _id(prefix: int, suffix: int) -> uuid.UUID:
    return uuid.UUID(f"{prefix:08x}-0000-0000-0000-{suffix:012x}")


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, *, evidence=(), objects=None, existing=None):
        self.evidence = list(evidence)
        self.objects = objects or {}
        self.existing = existing or {}
        self.added = []
        self.flush_count = 0
        self.queried_models = []

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, statement):
        model = statement.column_descriptions[0].get("entity")
        self.queried_models.append(model)
        if model is EvidenceUnit:
            return _Result(self.evidence)
        if model in (GraphEntityCandidateEvidence, GraphRelationCandidateEvidence):
            return _Result(self.existing.get(model, []))
        raise AssertionError(f"unexpected query for {model}")

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        for value in self.added:
            if value.id is None:
                value.id = uuid.uuid4()


def _evidence(
    suffix: int,
    text_quote: str | None,
    *,
    status: str = "active",
    kind: str = "chunk",
    block_id: uuid.UUID | None = None,
    library_id: uuid.UUID = LIB_ID,
    document_id: uuid.UUID = DOC_ID,
    revision_id: uuid.UUID = REV_ID,
    source_start: int | None = None,
    source_end: int | None = None,
):
    return SimpleNamespace(
        id=_id(0x90000000, suffix),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        document_block_id=block_id,
        evidence_kind=kind,
        source_start=source_start,
        source_end=source_end,
        text_quote=text_quote,
        status=status,
    )


def _fixture(*evidence, normalized_text="fallback quote text"):
    job = SimpleNamespace(
        id=JOB_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        ontology_version_id=uuid.uuid4(),
        status="processing",
    )
    unit = SimpleNamespace(
        id=UNIT_ID,
        job_id=JOB_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        status="processing",
    )
    snapshot = SimpleNamespace(
        job_id=JOB_ID,
        extraction_unit_id=UNIT_ID,
        purged_at=None,
        context_mapping={
            "c0": {
                "chunk_id": str(CHUNK_ID),
                "primary_evidence_id": str(evidence[0].id) if evidence else None,
                "evidence_ids": [str(item.id) for item in evidence],
                "role": "current",
            }
        },
    )
    entity_candidate = SimpleNamespace(
        id=ENTITY_CANDIDATE_ID,
        job_id=JOB_ID,
        library_id=LIB_ID,
        ontology_version_id=job.ontology_version_id,
        status="extracted",
    )
    relation_candidate = SimpleNamespace(
        id=RELATION_CANDIDATE_ID,
        job_id=JOB_ID,
        library_id=LIB_ID,
        ontology_version_id=job.ontology_version_id,
        status="extracted",
    )
    chunk = SimpleNamespace(
        id=CHUNK_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
    )
    revision = SimpleNamespace(
        id=REV_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        normalized_text=normalized_text,
    )
    objects = {
        (Chunk, CHUNK_ID): chunk,
        (DocumentRevision, REV_ID): revision,
    }
    db = FakeDB(evidence=evidence, objects=objects)
    return db, job, unit, snapshot, entity_candidate, relation_candidate


def _resolve(db, job, unit, snapshot, *, context_ref="c0", quote="fact"):
    from app.services.graph_candidate_evidence import resolve_evidence_claim

    return asyncio.run(
        resolve_evidence_claim(
            db,
            job=job,
            unit=unit,
            snapshot=snapshot,
            context_ref=context_ref,
            quote=quote,
        )
    )


def test_exact_case_sensitive_unique_match_is_valid():
    first = _evidence(1, "A fact is here. A Fact is different.")
    db, job, unit, snapshot, _, _ = _fixture(first)

    result = _resolve(db, job, unit, snapshot, quote="fact")

    assert result.validation_status == "valid"
    assert result.resolved_evidence_id == first.id
    assert result.resolved_chunk_id == CHUNK_ID
    assert result.resolved_block_id is None
    assert result.resolved_source_span == {
        "start": 2,
        "end": 6,
        "coordinate_system": "evidence_text_v1",
    }
    assert result.evidence_type == "direct_statement"
    assert result.evidence_quality_score == 1.0


def test_all_mapped_evidence_and_overlapping_occurrences_make_ambiguous():
    primary_without_match = _evidence(1, "nothing")
    overlapping = _evidence(2, "ababa")
    db, job, unit, snapshot, _, _ = _fixture(
        primary_without_match,
        overlapping,
    )

    result = _resolve(db, job, unit, snapshot, quote="aba")

    assert result.validation_status == "ambiguous"
    assert [match["source_span"]["start"] for match in result.candidate_matches] == [
        0,
        2,
    ]
    assert result.resolved_evidence_id is None
    assert result.evidence_type is None


def test_matches_are_sorted_by_evidence_id_then_span_not_mapping_order():
    later_id = _evidence(9, "fact fact")
    earlier_id = _evidence(2, "x fact")
    db, job, unit, snapshot, _, _ = _fixture(later_id, earlier_id)

    result = _resolve(db, job, unit, snapshot, quote="fact")

    assert result.validation_status == "ambiguous"
    assert [item["evidence_id"] for item in result.candidate_matches] == [
        str(earlier_id.id),
        str(later_id.id),
        str(later_id.id),
    ]
    assert [item["source_span"]["start"] for item in result.candidate_matches] == [
        2,
        0,
        5,
    ]


def test_inactive_and_non_materializable_evidence_do_not_match():
    stale = _evidence(1, "fact", status="stale")
    heading = _evidence(2, "fact", kind="heading")
    db, job, unit, snapshot, _, _ = _fixture(stale, heading)

    result = _resolve(db, job, unit, snapshot)

    assert result.validation_status == "invalid"
    assert result.candidate_matches == []
    assert result.validation_error == "quote_not_found"


def test_fallback_revision_slice_is_validated_before_searching():
    fallback = _evidence(1, None, source_start=9, source_end=19)
    db, job, unit, snapshot, _, _ = _fixture(
        fallback,
        normalized_text="prefix---fallback quote text---suffix",
    )
    result = _resolve(db, job, unit, snapshot, quote="fallback")
    assert result.validation_status == "valid"
    assert result.resolved_source_span["start"] == 0

    fallback.source_end = 10_000
    result = _resolve(db, job, unit, snapshot, quote="fallback")
    assert result.validation_status == "invalid"


def test_table_evidence_type_comes_from_evidence_or_real_block():
    table_row = _evidence(1, "fact", kind="table_row")
    db, job, unit, snapshot, _, _ = _fixture(table_row)
    assert _resolve(db, job, unit, snapshot).evidence_type == "table_cell"

    block_id = _id(0xA0000000, 1)
    block_backed = _evidence(2, "fact", block_id=block_id)
    db, job, unit, snapshot, _, _ = _fixture(block_backed)
    db.objects[(DocumentBlock, block_id)] = SimpleNamespace(
        id=block_id,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        block_kind="sheet_row",
    )
    result = _resolve(db, job, unit, snapshot)
    assert result.evidence_type == "table_cell"
    assert result.evidence_quality_score == 0.95
    assert result.resolved_block_id == block_id


def test_unknown_context_ref_is_protocol_error():
    from app.services.graph_candidate_evidence import EvidenceClaimProtocolError

    first = _evidence(1, "fact")
    db, job, unit, snapshot, _, _ = _fixture(first)
    with pytest.raises(EvidenceClaimProtocolError) as caught:
        _resolve(db, job, unit, snapshot, context_ref="made-up")
    assert caught.value.code == "unknown_context_ref"
    assert EvidenceUnit not in db.queried_models


@pytest.mark.parametrize("row", ["unit", "snapshot", "chunk", "evidence", "block"])
def test_cross_scope_rows_are_security_errors(row):
    from app.services.graph_candidate_evidence import EvidenceClaimSecurityError

    block_id = _id(0xA0000000, 1) if row == "block" else None
    first = _evidence(1, "fact", block_id=block_id)
    db, job, unit, snapshot, _, _ = _fixture(first)
    if block_id is not None:
        db.objects[(DocumentBlock, block_id)] = SimpleNamespace(
            id=block_id,
            library_id=uuid.uuid4(),
            document_id=DOC_ID,
            document_revision_id=REV_ID,
            block_kind="paragraph",
        )
    if row == "unit":
        unit.library_id = uuid.uuid4()
    elif row == "snapshot":
        snapshot.job_id = uuid.uuid4()
    elif row == "chunk":
        db.objects[(Chunk, CHUNK_ID)].document_id = uuid.uuid4()
    elif row == "evidence":
        first.document_revision_id = uuid.uuid4()

    with pytest.raises(EvidenceClaimSecurityError) as caught:
        _resolve(db, job, unit, snapshot)
    assert caught.value.code == "evidence_scope_mismatch"


def test_quote_and_claim_hashes_use_verbatim_utf8_and_canonical_json():
    from app.services.graph_candidate_evidence import create_entity_candidate_evidence

    first = _evidence(1, " 事实 Quote ")
    db, job, unit, snapshot, candidate, _ = _fixture(first)
    quote = "事实 Quote"
    row = asyncio.run(
        create_entity_candidate_evidence(
            db,
            job=job,
            unit=unit,
            candidate=candidate,
            snapshot=snapshot,
            context_ref="c0",
            quote=quote,
        )
    )
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    expected_claim = hashlib.sha256(
        json.dumps(
            {
                "candidate_id": str(candidate.id),
                "context_ref": "c0",
                "extraction_unit_id": str(unit.id),
                "quote_hash": quote_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert row.quote_text == quote
    assert row.quote_hash == quote_hash
    assert row.claim_key == expected_claim


def test_entity_and_relation_persistence_are_typed_and_do_not_change_states():
    from app.services.graph_candidate_evidence import (
        create_entity_candidate_evidence,
        create_relation_candidate_evidence,
    )

    first = _evidence(1, "fact")
    db, job, unit, snapshot, entity, relation = _fixture(first)
    states = deepcopy((job.status, unit.status, entity.status, relation.status))

    entity_row = asyncio.run(
        create_entity_candidate_evidence(
            db,
            job=job,
            unit=unit,
            candidate=entity,
            snapshot=snapshot,
            context_ref="c0",
            quote="fact",
        )
    )
    relation_row = asyncio.run(
        create_relation_candidate_evidence(
            db,
            job=job,
            unit=unit,
            candidate=relation,
            snapshot=snapshot,
            context_ref="c0",
            quote="fact",
        )
    )

    assert isinstance(entity_row, GraphEntityCandidateEvidence)
    assert isinstance(relation_row, GraphRelationCandidateEvidence)
    assert (job.status, unit.status, entity.status, relation.status) == states
    assert db.flush_count == 2


def test_idempotent_replay_returns_existing_and_purged_replay_is_rejected():
    from app.services.graph_candidate_evidence import (
        CandidateEvidenceReplayError,
        create_entity_candidate_evidence,
    )

    first = _evidence(1, "fact")
    db, job, unit, snapshot, candidate, _ = _fixture(first)
    created = asyncio.run(
        create_entity_candidate_evidence(
            db,
            job=job,
            unit=unit,
            candidate=candidate,
            snapshot=snapshot,
            context_ref="c0",
            quote="fact",
        )
    )

    replay_db, job, unit, snapshot, candidate, _ = _fixture(first)
    replay_db.existing[GraphEntityCandidateEvidence] = [created]
    assert (
        asyncio.run(
            create_entity_candidate_evidence(
                replay_db,
                job=job,
                unit=unit,
                candidate=candidate,
                snapshot=snapshot,
                context_ref="c0",
                quote="fact",
            )
        )
        is created
    )
    assert EvidenceUnit not in replay_db.queried_models
    assert not replay_db.added

    created.purged_at = object()
    with pytest.raises(CandidateEvidenceReplayError, match="purged"):
        asyncio.run(
            create_entity_candidate_evidence(
                replay_db,
                job=job,
                unit=unit,
                candidate=candidate,
                snapshot=snapshot,
                context_ref="c0",
                quote="fact",
            )
        )


def test_idempotent_replay_rejects_a_mismatched_stored_row():
    from app.services.graph_candidate_evidence import (
        CandidateEvidenceReplayError,
        create_entity_candidate_evidence,
    )

    first = _evidence(1, "fact")
    db, job, unit, snapshot, candidate, _ = _fixture(first)
    created = asyncio.run(
        create_entity_candidate_evidence(
            db,
            job=job,
            unit=unit,
            candidate=candidate,
            snapshot=snapshot,
            context_ref="c0",
            quote="fact",
        )
    )
    created.quote_text = "tampered"

    replay_db, job, unit, snapshot, candidate, _ = _fixture(first)
    replay_db.existing[GraphEntityCandidateEvidence] = [created]
    with pytest.raises(CandidateEvidenceReplayError, match="does not match"):
        asyncio.run(
            create_entity_candidate_evidence(
                replay_db,
                job=job,
                unit=unit,
                candidate=candidate,
                snapshot=snapshot,
                context_ref="c0",
                quote="fact",
            )
        )
    assert EvidenceUnit not in replay_db.queried_models
