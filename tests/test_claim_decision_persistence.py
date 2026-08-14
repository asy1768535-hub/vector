from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest

from app.models.claim_decision import GraphClaimDecision
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.claim_decision import CLAIM_DECISION_ID_NAMESPACE, deterministic_decision_id
from app.services import claim_decision_persistence as persistence
from app.services.raw_claim_persistence import _new_core
from tests.test_claim_decision import CREATED_AT, _build_production
from tests.test_raw_claim import _claim


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
    def __init__(self, claim):
        core = _new_core(claim)
        for field in ("library_id", "document_id", "document_revision_id"):
            setattr(core, field, UUID(getattr(core, field)))
        self.claims = {claim.claim_id: core}
        self.occurrences = {
            claim.extraction_occurrence_id: GraphRawClaimOccurrence(
                extraction_occurrence_id=claim.extraction_occurrence_id,
                claim_id=claim.claim_id,
            )
        }
        self.decisions = {}
        self.pending = []

    async def get(self, model, key):
        rows = {
            GraphRawClaim: self.claims,
            GraphRawClaimOccurrence: self.occurrences,
            GraphClaimDecision: self.decisions,
        }.get(model, {})
        return rows.get(key)

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        rows_by_entity = {
            GraphRawClaimOccurrence: self.occurrences,
            GraphClaimDecision: self.decisions,
        }
        rows = list(rows_by_entity.get(entity, {}).values())
        for key, value in params.items():
            if key.startswith("extraction_occurrence_id"):
                rows = [row for row in rows if row.extraction_occurrence_id == value]
            elif key.startswith("decision_fingerprint"):
                rows = [row for row in rows if row.decision_fingerprint == value]
            elif key.startswith("library_id"):
                rows = [row for row in rows if row.library_id == value]
            elif key.startswith("document_revision_id"):
                rows = [row for row in rows if row.document_revision_id == value]
            elif key.startswith("claim_id"):
                rows = [row for row in rows if row.claim_id == value]
        return _Result(rows)

    def add(self, row):
        self.pending.append(row)

    def begin_nested(self):
        return _Nested(self)

    async def flush(self):
        for row in self.pending:
            if isinstance(row, GraphClaimDecision):
                self.decisions[row.decision_id] = row
        self.pending.clear()


def _run(coro):
    return asyncio.run(coro)


def test_create_or_get_preserves_first_created_at_and_round_trips_proposal():
    claim = _claim()
    decision = _build_production(claim, extraction_occurrence_id=claim.extraction_occurrence_id)
    later = _build_production(
        claim,
        extraction_occurrence_id=claim.extraction_occurrence_id,
        created_at=CREATED_AT.replace(day=8),
    )
    db = _Db(claim)

    first = _run(persistence.create_or_get_claim_decision(db, decision))
    second = _run(persistence.create_or_get_claim_decision(db, later))
    read = _run(
        persistence.get_claim_decision(
            db,
            library_id=claim.library_id,
            document_revision_id=claim.document_revision_id,
            decision_id=decision.decision_id,
        )
    )

    assert first.decision_created is True
    assert second.decision_created is False
    assert first.decision.created_at == CREATED_AT
    assert read is not None
    assert read.proposal.raw_predicate == claim.raw_predicate
    assert read.created_at == CREATED_AT


def test_create_or_get_rejects_missing_or_cross_claim_occurrence():
    claim = _claim()
    decision = _build_production(claim, extraction_occurrence_id=claim.extraction_occurrence_id)
    db = _Db(claim)
    db.occurrences.clear()
    with pytest.raises(persistence.ClaimDecisionScopeError, match="occurrence"):
        _run(persistence.create_or_get_claim_decision(db, decision))
    assert not db.decisions

    other_claim = _claim(claim_id=uuid.uuid4(), extraction_occurrence_id=uuid.uuid4())
    db = _Db(claim)
    db.occurrences[other_claim.extraction_occurrence_id] = GraphRawClaimOccurrence(
        extraction_occurrence_id=other_claim.extraction_occurrence_id,
        claim_id=other_claim.claim_id,
    )
    cross_payload = decision.model_dump(mode="json")
    cross_payload.update(
        claim_id=str(claim.claim_id),
        extraction_occurrence_id=str(other_claim.extraction_occurrence_id),
        decision_fingerprint=None,
    )
    cross = type(decision).model_validate(cross_payload)
    cross = cross.model_copy(
        update={
            "decision_id": deterministic_decision_id(
                CLAIM_DECISION_ID_NAMESPACE,
                cross.decision_fingerprint or "",
            )
        }
    )
    with pytest.raises(persistence.ClaimDecisionScopeError, match="belong"):
        _run(persistence.create_or_get_claim_decision(db, cross))
    assert not db.decisions


def test_create_or_get_rejects_existing_identity_conflict_without_mutation():
    claim = _claim()
    decision = _build_production(claim)
    db = _Db(claim)
    row = persistence._new_decision(decision)
    row.producer_version = "tampered"
    db.decisions[row.decision_id] = row

    with pytest.raises(persistence.ClaimDecisionConflictError, match="existing decision"):
        _run(persistence.create_or_get_claim_decision(db, decision))
    assert db.decisions[row.decision_id].producer_version == "tampered"


def test_scoped_list_does_not_cross_revision():
    claim = _claim()
    decision = _build_production(claim)
    db = _Db(claim)
    _run(persistence.create_or_get_claim_decision(db, decision))

    assert len(
        _run(
            persistence.list_claim_decisions(
                db,
                library_id=claim.library_id,
                document_revision_id=claim.document_revision_id,
            )
        )
    ) == 1
    assert not _run(
        persistence.list_claim_decisions(
            db,
            library_id=claim.library_id,
            document_revision_id=uuid.uuid4(),
        )
    )


@pytest.mark.parametrize("construction", ["model_copy", "model_construct"])
def test_create_rejects_stale_production_decision_id_from_forged_instance(construction):
    claim = _claim()
    decision = _build_production(claim)
    stale_id = uuid.uuid4()
    if construction == "model_copy":
        forged = decision.model_copy(update={"decision_id": stale_id})
    else:
        payload = decision.model_dump(mode="python")
        payload["decision_id"] = stale_id
        payload["proposal"] = decision.proposal
        forged = type(decision).model_construct(**payload)

    with pytest.raises(persistence.ClaimDecisionPersistenceError, match="deterministic"):
        _run(persistence.create_or_get_claim_decision(_Db(claim), forged))
