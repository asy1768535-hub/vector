from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.raw_claim import RawClaimV1
from app.services import raw_claim_persistence as persistence


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000101")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000101")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000101")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000101")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000101")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000101")
BLOCK_ID = uuid.UUID("60000000-0000-0000-0000-000000000102")
CHUNK_ID = uuid.UUID("60000000-0000-0000-0000-000000000103")
CLAIM_ID = uuid.UUID("70000000-0000-0000-0000-000000000101")
OCCURRENCE_ID = uuid.UUID("80000000-0000-0000-0000-000000000101")
HASH = "a" * 64


def _claim(
    *,
    revision_id: uuid.UUID = REVISION_ID,
    job_id: uuid.UUID = JOB_ID,
    unit_id: uuid.UUID = UNIT_ID,
    claim_id: uuid.UUID = CLAIM_ID,
    occurrence_id: uuid.UUID = OCCURRENCE_ID,
    model_name: str = "fixture-model",
) -> RawClaimV1:
    quote = "Source supports target."
    return RawClaimV1(
        claim_id=claim_id,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=revision_id,
        revision_no=1,
        job_id=job_id,
        extraction_unit_id=unit_id,
        source_mention={
            "local_id": "source-1",
            "surface": "Source",
            "entity_type_hint": "source_type",
            "evidence_ref": "e1",
        },
        raw_predicate="supports",
        target_mention={
            "local_id": "target-1",
            "surface": "Target",
            "entity_type_hint": "target_type",
            "evidence_ref": "e1",
        },
        surface_direction="source_to_target",
        negation={"value": False, "evidence_ref": "e1"},
        modality={"value": "asserted", "evidence_ref": "e1"},
        qualifiers=[{"key": "basis", "value": "fixture", "evidence_ref": "e1"}],
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "e1",
        },
        effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "e1"},
        evidence_refs=[
            {
                "ref_id": "e1",
                "evidence_id": EVIDENCE_ID,
                "library_id": LIBRARY_ID,
                "document_id": DOCUMENT_ID,
                "document_revision_id": revision_id,
                "revision_no": 1,
                "job_id": job_id,
                "extraction_unit_id": unit_id,
                "unit_id": EVIDENCE_ID,
                "chunk_id": CHUNK_ID,
                "block_id": BLOCK_ID,
                "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "unit_text_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "source_span": {"start": 0, "end": len(quote)},
            }
        ],
        extractor_version="extractor-v1",
        prompt_version="prompt-v1",
        model_provider="fixture-provider",
        model_name=model_name,
        model_config_hash=HASH,
        prompt_content_hash=HASH,
        parser_version="parser-v1",
        normalization_rule_version="normalization-v1",
        ontology_snapshot_hash=HASH,
        extraction_occurrence_id=occurrence_id,
    )


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalar_one_or_none(self):
        if len(self.rows) > 1:
            raise AssertionError("fixture query unexpectedly returned multiple rows")
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _Nested:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self.db.pending.clear()
        return False


class _Db:
    def __init__(self, *claims: RawClaimV1):
        self.revisions = {}
        self.jobs = {}
        self.units = {}
        self.evidence_units = {}
        self.chunks = {}
        self.blocks = {}
        self.claims = {}
        self.occurrences = {}
        self.pending = []
        self.fail_flush = False
        for claim in claims:
            self.add_scope(claim)

    def add_scope(self, claim: RawClaimV1):
        self.revisions[claim.document_revision_id] = SimpleNamespace(
            id=claim.document_revision_id,
            library_id=claim.library_id,
            document_id=claim.document_id,
            revision_no=claim.revision_no,
        )
        self.jobs[claim.job_id] = SimpleNamespace(
            id=claim.job_id,
            library_id=claim.library_id,
            document_id=claim.document_id,
            document_revision_id=claim.document_revision_id,
        )
        self.units[claim.extraction_unit_id] = SimpleNamespace(
            id=claim.extraction_unit_id,
            job_id=claim.job_id,
            library_id=claim.library_id,
            document_revision_id=claim.document_revision_id,
        )
        quote_hash = claim.evidence_refs[0].quote_sha256
        quote = "Source supports target."
        self.evidence_units[claim.evidence_refs[0].evidence_id] = SimpleNamespace(
            id=claim.evidence_refs[0].evidence_id,
            library_id=claim.library_id,
            document_id=claim.document_id,
            document_revision_id=claim.document_revision_id,
            document_block_id=claim.evidence_refs[0].block_id,
            text_quote_hash=quote_hash,
            text_quote=quote,
            source_start=claim.evidence_refs[0].source_span.start,
            source_end=claim.evidence_refs[0].source_span.end,
            status="active",
        )
        chunk_id = claim.evidence_refs[0].chunk_id
        if chunk_id is not None:
            self.chunks[chunk_id] = SimpleNamespace(
                id=chunk_id,
                library_id=claim.library_id,
                document_id=claim.document_id,
                document_revision_id=claim.document_revision_id,
                evidence_id=claim.evidence_refs[0].evidence_id,
                block_id=claim.evidence_refs[0].block_id,
                text=quote,
            )
        block_id = claim.evidence_refs[0].block_id
        if block_id is not None:
            self.blocks[block_id] = SimpleNamespace(
                id=block_id,
                library_id=claim.library_id,
                document_id=claim.document_id,
                document_revision_id=claim.document_revision_id,
                text=quote,
            )

    async def get(self, model, key):
        rows = {
            DocumentRevision: self.revisions,
            GraphExtractionJob: self.jobs,
            GraphExtractionUnit: self.units,
            EvidenceUnit: self.evidence_units,
            Chunk: self.chunks,
            DocumentBlock: self.blocks,
            GraphRawClaim: self.claims,
        }.get(model, {})
        return rows.get(key)

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        rows_by_entity = {
            GraphRawClaim: self.claims,
            GraphRawClaimOccurrence: self.occurrences,
            EvidenceUnit: self.evidence_units,
            Chunk: self.chunks,
            DocumentBlock: self.blocks,
        }
        rows = list(rows_by_entity.get(entity, {}).values())
        for key, value in params.items():
            if key.startswith("content_scoped_claim_fingerprint"):
                rows = [row for row in rows if row.content_scoped_claim_fingerprint == value]
            elif key.startswith("extraction_occurrence_id"):
                rows = [row for row in rows if row.extraction_occurrence_id == value]
            elif key.startswith("extraction_occurrence_fingerprint"):
                rows = [row for row in rows if row.extraction_occurrence_fingerprint == value]
            elif key.startswith("id"):
                values = value if isinstance(value, (list, tuple, set)) else (value,)
                rows = [row for row in rows if row.id in values]
        return _Result(rows)

    def add(self, row):
        self.pending.append(row)

    def begin_nested(self):
        return _Nested(self)

    async def flush(self):
        if self.fail_flush:
            raise RuntimeError("fixture flush failure")
        for row in self.pending:
            if isinstance(row, GraphRawClaim):
                self.claims[row.id] = row
            else:
                if row.id is None:
                    row.id = uuid.uuid4()
                self.occurrences[row.id] = row
        self.pending.clear()


def _run(coro):
    return asyncio.run(coro)


def _claim_with_reference_update(claim: RawClaimV1, **updates) -> RawClaimV1:
    data = claim.model_dump(mode="json")
    data["evidence_refs"][0].update(updates)
    data.pop("content_scoped_claim_fingerprint", None)
    data.pop("extraction_occurrence_fingerprint", None)
    return RawClaimV1.model_validate(data)


def test_models_expose_two_immutable_storage_boundaries_and_no_mutation_api():
    assert GraphRawClaim.__tablename__ == "graph_raw_claims"
    assert GraphRawClaimOccurrence.__tablename__ == "graph_raw_claim_occurrences"
    assert GraphRawClaim.__table__.c.claim_schema_version is not None
    assert GraphRawClaim.__table__.c.content_scoped_claim_fingerprint is not None
    assert GraphRawClaimOccurrence.__table__.c.extraction_occurrence_fingerprint is not None
    assert not hasattr(persistence, "update_raw_claim")
    assert not hasattr(persistence, "delete_raw_claim")
    assert all(
        fk.ondelete == "RESTRICT"
        for table in (GraphRawClaim.__table__, GraphRawClaimOccurrence.__table__)
        for column in table.columns
        for fk in column.foreign_keys
    )


def test_first_write_and_idempotent_retry_reuse_core_and_occurrence():
    claim = _claim()
    db = _Db(claim)
    first = _run(persistence.create_or_get_raw_claim(db, claim))
    second = _run(persistence.create_or_get_raw_claim(db, claim))

    assert (first.claim_created, first.occurrence_created) == (True, True)
    assert (second.claim_created, second.occurrence_created) == (False, False)
    assert len(db.claims) == 1
    assert len(db.occurrences) == 1
    assert "quote" not in first.claim.evidence_refs[0]
    assert "job_id" not in first.claim.evidence_refs[0]
    assert "extraction_unit_id" not in first.claim.evidence_refs[0]
    assert first.occurrence.evidence_refs[0]["quote_sha256"] == claim.evidence_refs[0].quote_sha256


def test_same_content_different_provenance_creates_one_core_and_two_occurrences():
    first_claim = _claim()
    second_claim = _claim(
        job_id=uuid.UUID("40000000-0000-0000-0000-000000000102"),
        unit_id=uuid.UUID("50000000-0000-0000-0000-000000000102"),
        occurrence_id=uuid.UUID("80000000-0000-0000-0000-000000000102"),
        model_name="other-model",
    )
    db = _Db(first_claim, second_claim)
    first = _run(persistence.create_or_get_raw_claim(db, first_claim))
    second = _run(persistence.create_or_get_raw_claim(db, second_claim))

    assert first.claim.id == second.claim.id
    assert first_claim.content_scoped_claim_fingerprint == second_claim.content_scoped_claim_fingerprint
    assert first_claim.extraction_occurrence_fingerprint != second_claim.extraction_occurrence_fingerprint
    assert second.claim_created is False
    assert second.occurrence_created is True
    assert len(db.claims) == 1
    assert len(db.occurrences) == 2


def test_evidence_ref_token_rename_reuses_core_but_keeps_occurrence_evidence_trace():
    first_claim = _claim()
    data = first_claim.model_dump(mode="json")
    data["claim_id"] = str(uuid.UUID("70000000-0000-0000-0000-000000000102"))
    data["extraction_occurrence_id"] = str(uuid.UUID("80000000-0000-0000-0000-000000000102"))
    data["model_name"] = "other-model"
    data["source_mention"]["evidence_ref"] = "renamed"
    data["target_mention"]["evidence_ref"] = "renamed"
    data["negation"]["evidence_ref"] = "renamed"
    data["modality"]["evidence_ref"] = "renamed"
    data["qualifiers"][0]["evidence_ref"] = "renamed"
    data["valid_time"]["evidence_ref"] = "renamed"
    data["effective_time"]["evidence_ref"] = "renamed"
    data["evidence_refs"][0]["ref_id"] = "renamed"
    data.pop("content_scoped_claim_fingerprint")
    data.pop("extraction_occurrence_fingerprint")
    second_claim = RawClaimV1.model_validate(data)

    db = _Db(first_claim, second_claim)
    first = _run(persistence.create_or_get_raw_claim(db, first_claim))
    second = _run(persistence.create_or_get_raw_claim(db, second_claim))

    assert first_claim.content_scoped_claim_fingerprint == second_claim.content_scoped_claim_fingerprint
    assert first.claim.id == second.claim.id
    assert second.occurrence_created is True
    assert second.occurrence.evidence_refs[0]["ref_id"] == "renamed"


def test_revision_scope_and_duplicate_fingerprint_payload_fail_closed():
    claim = _claim()
    db = _Db(claim)
    _run(persistence.create_or_get_raw_claim(db, claim))

    different_revision = _claim(
        revision_id=uuid.UUID("30000000-0000-0000-0000-000000000102"),
        claim_id=uuid.UUID("70000000-0000-0000-0000-000000000102"),
        occurrence_id=uuid.UUID("80000000-0000-0000-0000-000000000102"),
    )
    db.add_scope(different_revision)
    second = _run(persistence.create_or_get_raw_claim(db, different_revision))
    assert second.claim_created is True
    assert len(db.claims) == 2

    conflicting = claim.model_copy(
        update={
            "raw_predicate": "changed",
            "content_scoped_claim_fingerprint": claim.content_scoped_claim_fingerprint,
        }
    )
    with pytest.raises(ValidationError):
        _run(persistence.create_or_get_raw_claim(db, conflicting))


def test_invalid_input_and_transaction_failure_leave_no_half_written_pair():
    claim = _claim()
    db = _Db(claim)
    db.fail_flush = True
    with pytest.raises(RuntimeError, match="fixture flush failure"):
        _run(persistence.create_or_get_raw_claim(db, claim))
    assert db.claims == {}
    assert db.occurrences == {}

    with pytest.raises(TypeError):
        _run(persistence.create_or_get_raw_claim(db, claim.model_dump(mode="json")))

    malformed = claim.model_dump(mode="json")
    malformed["raw_predicate"] = "x" * 300
    malformed.pop("content_scoped_claim_fingerprint")
    malformed.pop("extraction_occurrence_fingerprint")
    with pytest.raises(ValidationError):
        RawClaimV1.model_validate(malformed)

    sensitive = claim.model_dump(mode="json")
    sensitive["qualifiers"] = [
        {"key": "storage-path", "value": "secret", "evidence_ref": "e1"}
    ]
    sensitive.pop("content_scoped_claim_fingerprint")
    sensitive.pop("extraction_occurrence_fingerprint")
    with pytest.raises(ValidationError):
        RawClaimV1.model_validate(sensitive)


def test_scope_reads_require_both_library_and_revision_and_round_trip_evidence_hashes():
    claim = _claim()
    db = _Db(claim)
    result = _run(persistence.create_or_get_raw_claim(db, claim))
    read_claim = _run(
        persistence.get_raw_claim(
            db,
            library_id=LIBRARY_ID,
            document_revision_id=REVISION_ID,
            claim_id=result.claim.id,
        )
    )
    read_occurrence = _run(
        persistence.get_raw_claim_occurrence(
            db,
            library_id=LIBRARY_ID,
            document_revision_id=REVISION_ID,
            extraction_occurrence_id=OCCURRENCE_ID,
        )
    )
    assert read_claim is result.claim
    assert read_occurrence is result.occurrence
    assert read_claim.evidence_refs[0]["unit_text_sha256"] == claim.evidence_refs[0].unit_text_sha256


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda db: db.evidence_units.pop(EVIDENCE_ID), "missing EvidenceUnit"),
        (lambda db: setattr(db.evidence_units[EVIDENCE_ID], "library_id", uuid.uuid4()), "crosses"),
        (lambda db: setattr(db.evidence_units[EVIDENCE_ID], "text_quote_hash", "b" * 64), "quote hash"),
        (lambda db: setattr(db.evidence_units[EVIDENCE_ID], "status", "deleted"), "claimable"),
        (lambda db: db.chunks.pop(CHUNK_ID), "missing Chunk"),
        (lambda db: setattr(db.chunks[CHUNK_ID], "evidence_id", uuid.uuid4()), "Chunk evidence"),
        (lambda db: setattr(db.chunks[CHUNK_ID], "block_id", uuid.uuid4()), "blocks"),
        (lambda db: setattr(db.evidence_units[EVIDENCE_ID], "source_end", 1), "source span"),
    ],
)
def test_evidence_database_validation_fails_closed(mutation, message):
    claim = _claim()
    db = _Db(claim)
    mutation(db)

    with pytest.raises(persistence.RawClaimScopeError, match=message):
        _run(persistence.create_or_get_raw_claim(db, claim))
    assert db.claims == {}
    assert db.occurrences == {}


class _UniqueRaceDb(_Db):
    """Make the first flush look like a concurrent unique-key winner."""

    def __init__(self, claim):
        super().__init__(claim)
        self.raced = False

    async def flush(self):
        if not self.raced:
            self.raced = True
            for row in self.pending:
                if isinstance(row, GraphRawClaim):
                    self.claims[row.id] = row
                else:
                    if row.id is None:
                        row.id = uuid.uuid4()
                    self.occurrences[row.id] = row
            self.pending.clear()
            raise IntegrityError("insert", {}, RuntimeError("unique race"))
        await super().flush()


def test_unique_constraint_race_reloads_matching_core_and_occurrence():
    claim = _claim()
    db = _UniqueRaceDb(claim)

    result = _run(persistence.create_or_get_raw_claim(db, claim))

    assert result.claim_created is False
    assert result.occurrence_created is False
    assert result.claim.id == claim.claim_id
    assert result.occurrence.extraction_occurrence_id == claim.extraction_occurrence_id
    assert len(db.claims) == 1
    assert len(db.occurrences) == 1


def test_chunk_unit_text_hash_must_match_actual_chunk_text():
    claim = _claim()
    wrong_hash = "b" * 64
    wrong_hash_claim = _claim_with_reference_update(claim, unit_text_sha256=wrong_hash)
    db = _Db(wrong_hash_claim)
    with pytest.raises(persistence.RawClaimScopeError, match="Chunk unit text hash"):
        _run(persistence.create_or_get_raw_claim(db, wrong_hash_claim))

    db = _Db(claim)
    db.chunks[CHUNK_ID].text = "tampered chunk"
    with pytest.raises(persistence.RawClaimScopeError, match="Chunk unit text hash"):
        _run(persistence.create_or_get_raw_claim(db, claim))


def test_document_block_unit_text_hash_must_match_actual_block_text():
    claim = _claim()
    data = claim.model_dump(mode="json")
    reference = data["evidence_refs"][0]
    reference.pop("chunk_id")
    reference["unit_id"] = str(BLOCK_ID)
    data.pop("content_scoped_claim_fingerprint", None)
    data.pop("extraction_occurrence_fingerprint", None)
    block_claim = RawClaimV1.model_validate(data)

    db = _Db(block_claim)
    db.blocks[BLOCK_ID].text = "tampered block"
    with pytest.raises(persistence.RawClaimScopeError, match="DocumentBlock unit text hash"):
        _run(persistence.create_or_get_raw_claim(db, block_claim))

    wrong_hash_claim = _claim_with_reference_update(
        block_claim, unit_text_sha256="b" * 64
    )
    db = _Db(wrong_hash_claim)
    with pytest.raises(persistence.RawClaimScopeError, match="DocumentBlock unit text hash"):
        _run(persistence.create_or_get_raw_claim(db, wrong_hash_claim))


def test_evidence_unit_minimum_requires_unit_hash_equal_to_quote_hash_and_real_quote():
    claim = _claim()
    data = claim.model_dump(mode="json")
    reference = data["evidence_refs"][0]
    reference.pop("chunk_id")
    reference.pop("block_id")
    data.pop("content_scoped_claim_fingerprint", None)
    data.pop("extraction_occurrence_fingerprint", None)
    evidence_claim = RawClaimV1.model_validate(data)

    db = _Db(evidence_claim)
    result = _run(persistence.create_or_get_raw_claim(db, evidence_claim))
    assert result.claim_created and result.occurrence_created

    wrong_hash_claim = _claim_with_reference_update(
        evidence_claim, unit_text_sha256="b" * 64
    )
    db = _Db(wrong_hash_claim)
    with pytest.raises(persistence.RawClaimScopeError, match="minimum unit text hash"):
        _run(persistence.create_or_get_raw_claim(db, wrong_hash_claim))

    db = _Db(evidence_claim)
    db.evidence_units[EVIDENCE_ID].text_quote = "tampered quote"
    with pytest.raises(persistence.RawClaimScopeError, match="quote text hash"):
        _run(persistence.create_or_get_raw_claim(db, evidence_claim))


def test_unclassifiable_unit_without_chunk_or_block_fails_closed():
    claim = _claim()
    data = claim.model_dump(mode="json")
    reference = data["evidence_refs"][0]
    reference.pop("chunk_id")
    reference.pop("block_id")
    reference["unit_id"] = str(uuid.UUID("90000000-0000-0000-0000-000000000101"))
    data.pop("content_scoped_claim_fingerprint", None)
    data.pop("extraction_occurrence_fingerprint", None)
    unclassifiable = RawClaimV1.model_validate(data)

    with pytest.raises(persistence.RawClaimScopeError, match="cannot be verified"):
        _run(persistence.create_or_get_raw_claim(_Db(unclassifiable), unclassifiable))
