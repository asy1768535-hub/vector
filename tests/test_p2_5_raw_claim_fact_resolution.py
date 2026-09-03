from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.models.fact_foundation import StablePredicateIdentity
from app.services.graph_relation_fact_resolution import (
    FactResolutionSource,
    _decision,
    build_graph_relation_fact_plan,
    finalize_resolved_graph_relation_fact,
    materialize_resolved_graph_relation_fact,
    preflight_graph_relation_candidate_fact,
    with_fact_resolution_source,
)
from app.services.raw_claim_fact_resolution import (
    build_raw_claim_fact_adapter_plan,
    record_raw_claim_projection_pending_fact,
)

LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000101")
PREDICATE_ID = uuid.UUID("20000000-0000-0000-0000-000000000101")
SOURCE_CANONICAL_ID = uuid.UUID("30000000-0000-0000-0000-000000000101")
TARGET_CANONICAL_ID = uuid.UUID("40000000-0000-0000-0000-000000000101")


def _policy(*, measurement: bool = True, valid_time: bool = False) -> dict[str, object]:
    return {
        "schema_version": "p2_v1",
        "temporal_class": "measurement_slot",
        "object_policy": {"source": "target_entity"},
        "measurement_policy": (
            {
                "value_property_key": "ownership_percentage",
                "value_type": "ratio",
                "unit_policy": {"source": "fixed", "value": "ratio"},
                "currency_policy": {"source": "none"},
            }
            if measurement
            else None
        ),
        "qualifier_policy": {
            "identity_bearing": [],
            "assertion_bearing": ["ownership_percentage"],
            "evidence_only": [],
        },
        "polarity_policy": {"source": "fixed", "value": "affirmed"},
        "modality_policy": {"source": "fixed", "value": "confirmed"},
        "valid_time_policy": (
            {"source": "instant_property", "property_key": "observed_valid_time"}
            if valid_time
            else {"source": "none"}
        ),
        "effective_time_policy": {"source": "none"},
        "event_temporal_identity_policy": None,
    }


def _predicate(*, policy: dict[str, object] | None = None) -> StablePredicateIdentity:
    return StablePredicateIdentity(
        id=PREDICATE_ID,
        library_id=LIBRARY_ID,
        namespace="v1",
        key="holds_equity",
        contract_version="v1",
        temporal_class="measurement_slot",
        identity_policy_version="p2_identity_v1",
        resolution_status="resolved",
        resolution_policy=policy or _policy(),
    )


def _candidate(*, properties: dict[str, object] | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        relation_type_key="holds_equity",
        proposed_properties=properties or {"ownership_percentage": "20%"},
        final_confidence=0.95,
        evidence_support_mode="single_evidence",
    )


def _raw_claim(**updates):
    value = {
        "id": uuid.uuid4(),
        "content_scoped_claim_fingerprint": "a" * 64,
        "qualifiers": [{"key": "ownership_percentage", "value": "20%", "evidence_ref": "e1"}],
        "negation": {"value": False, "evidence_ref": "e1"},
        "modality": {"value": "confirmed", "evidence_ref": "e1"},
        "valid_time": None,
        "effective_time": None,
    }
    value.update(updates)
    return SimpleNamespace(**value)


def _evidence():
    return SimpleNamespace(
        resolved_document_id=uuid.uuid4(),
        resolved_document_revision_id=uuid.uuid4(),
        resolved_evidence_id=uuid.uuid4(),
        resolved_chunk_id=uuid.uuid4(),
        resolved_block_id=None,
        resolved_source_span={"start": 2, "end": 8},
    )


def _entity(canonical_entity_id: uuid.UUID):
    return SimpleNamespace(id=uuid.uuid4(), canonical_entity_id=canonical_entity_id)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _ResolutionDb:
    def __init__(self, *, results, objects=None):
        self.results = list(results)
        self.objects = objects or {}
        self.added = []

    async def execute(self, _statement, _params=None):
        assert self.results, "unexpected fact-resolution query"
        return self.results.pop(0)

    async def get(self, model, identifier):
        return self.objects.get((model, identifier))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def test_exact_raw_measurement_reuses_the_candidate_assertion_fingerprint() -> None:
    predicate = _predicate()
    projection = _candidate()
    raw_plan = build_raw_claim_fact_adapter_plan(
        raw_claim=_raw_claim(),
        projection_candidate=projection,
        predicate=predicate,
    )
    evidence = _evidence()

    assert raw_plan.status == "ready"
    raw_fact_plan = build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=raw_plan.candidate,
        source_entity=_entity(SOURCE_CANONICAL_ID),
        target_entity=_entity(TARGET_CANONICAL_ID),
        evidence=evidence,
    )
    candidate_fact_plan = build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=projection,
        source_entity=_entity(SOURCE_CANONICAL_ID),
        target_entity=_entity(TARGET_CANONICAL_ID),
        evidence=evidence,
    )

    assert raw_fact_plan.status == candidate_fact_plan.status == "resolved"
    assert raw_fact_plan.logical_fact is not None
    assert raw_fact_plan.assertion is not None
    assert raw_fact_plan.logical_fact.identity_fingerprint == candidate_fact_plan.logical_fact.identity_fingerprint
    assert raw_fact_plan.assertion.assertion_fingerprint == candidate_fact_plan.assertion.assertion_fingerprint
    assert raw_fact_plan.assertion.asserted_value == {"kind": "decimal", "unit": "ratio", "value": "0.2"}


def test_raw_adapter_fails_closed_for_unclassified_qualifier() -> None:
    plan = build_raw_claim_fact_adapter_plan(
        raw_claim=_raw_claim(
            qualifiers=[
                {"key": "ownership_percentage", "value": "20%"},
                {"key": "unclassified", "value": "must not be ignored"},
            ]
        ),
        projection_candidate=_candidate(),
        predicate=_predicate(),
    )

    assert plan.status == "pending"
    assert plan.reason_code == "unclassified_relation_property"


def test_raw_adapter_rejects_candidate_policy_polarity_disagreement() -> None:
    plan = build_raw_claim_fact_adapter_plan(
        raw_claim=_raw_claim(negation={"value": True, "evidence_ref": "e1"}),
        projection_candidate=_candidate(),
        predicate=_predicate(),
    )

    assert plan.status == "pending"
    assert plan.reason_code == "raw_claim_policy_semantic_mismatch"


def test_raw_adapter_rejects_unsupported_modality() -> None:
    plan = build_raw_claim_fact_adapter_plan(
        raw_claim=_raw_claim(modality={"value": "maybe", "evidence_ref": "e1"}),
        projection_candidate=_candidate(),
        predicate=_predicate(),
    )

    assert plan.status == "pending"
    assert plan.reason_code == "raw_claim_modality_invalid"


def test_raw_adapter_uses_typed_valid_time_only_when_the_policy_declares_it() -> None:
    plan = build_raw_claim_fact_adapter_plan(
        raw_claim=_raw_claim(
            valid_time={"start": "2026-01-01", "end": "2026-01-01", "evidence_ref": "e1"}
        ),
        projection_candidate=_candidate(),
        predicate=_predicate(policy=_policy(valid_time=True)),
    )

    assert plan.status == "ready"
    assert plan.candidate.proposed_properties["observed_valid_time"] == "2026-01-01"


def test_raw_claim_uses_an_independent_decision_identity_but_not_an_assertion_identity() -> None:
    predicate = _predicate()
    projection = _candidate()
    raw_claim = _raw_claim()
    adapted = build_raw_claim_fact_adapter_plan(
        raw_claim=raw_claim,
        projection_candidate=projection,
        predicate=predicate,
    )
    assert adapted.candidate is not None
    plan = build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=adapted.candidate,
        source_entity=_entity(SOURCE_CANONICAL_ID),
        target_entity=_entity(TARGET_CANONICAL_ID),
        evidence=_evidence(),
    )
    source = FactResolutionSource.raw_claim(raw_claim.id)
    raw_plan = with_fact_resolution_source(plan, source=source)
    decision = _decision(
        library_id=LIBRARY_ID,
        candidate=adapted.candidate,
        plan=raw_plan,
        status="pending",
        source=source,
    )

    assert raw_plan.decision_fingerprint != plan.decision_fingerprint
    assert decision.source_kind == "raw_claim"
    assert decision.raw_claim_id == raw_claim.id
    assert decision.graph_relation_candidate_id is None


def test_same_source_raw_claim_reuses_the_candidate_assertion_and_records_raw_decision() -> None:
    predicate = _predicate()
    projection = _candidate()
    raw_claim = _raw_claim()
    adapted = build_raw_claim_fact_adapter_plan(
        raw_claim=raw_claim,
        projection_candidate=projection,
        predicate=predicate,
    )
    assert adapted.candidate is not None
    source_entity = _entity(SOURCE_CANONICAL_ID)
    target_entity = _entity(TARGET_CANONICAL_ID)
    evidence = _evidence()
    candidate_plan = build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=projection,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence=evidence,
    )
    assert candidate_plan.logical_fact is not None and candidate_plan.assertion is not None
    logical_fact = SimpleNamespace(
        id=uuid.uuid4(), identity_fingerprint=candidate_plan.logical_fact.identity_fingerprint
    )
    assertion = SimpleNamespace(
        id=uuid.uuid4(),
        logical_fact_id=logical_fact.id,
        assertion_fingerprint=candidate_plan.assertion.assertion_fingerprint,
    )
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    db = _ResolutionDb(
        results=[_Result([mapping]), _Result(), _Result([logical_fact]), _Result([assertion]), _Result()],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )
    relation = SimpleNamespace(id=uuid.uuid4(), relation_type_id=uuid.uuid4(), logical_fact_id=None)
    relation_evidence = SimpleNamespace(fact_assertion_id=None)
    source = FactResolutionSource.raw_claim(raw_claim.id)
    preflight = asyncio.run(
        preflight_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=projection,
            relation_type_id=relation.relation_type_id,
            source_entity=source_entity,
            target_entity=target_entity,
            evidence_rows=[evidence],
            source=source,
            expected_logical_fact=logical_fact,
            expected_assertion=assertion,
            candidate_adapter=lambda _predicate: adapted,
        )
    )
    materialization = asyncio.run(
        materialize_resolved_graph_relation_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=adapted.candidate,
            relation=relation,
            source_entity=source_entity,
            preflight=preflight,
            source=source,
        )
    )
    result = asyncio.run(
        finalize_resolved_graph_relation_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=adapted.candidate,
            relation=relation,
            relation_evidence=relation_evidence,
            preflight=preflight,
            materialization=materialization,
            source=source,
        )
    )

    assert result.outcome == "REUSE"
    assert result.assertion is assertion
    assert relation.logical_fact_id == logical_fact.id
    assert relation_evidence.fact_assertion_id == assertion.id
    assert result.decision.source_kind == "raw_claim"
    assert result.decision.raw_claim_id == raw_claim.id
    assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]


def test_same_source_raw_measurement_disagreement_stays_pending_without_a_second_assertion() -> None:
    predicate = _predicate()
    projection = _candidate()
    raw_claim = _raw_claim(qualifiers=[{"key": "ownership_percentage", "value": "30%"}])
    source_entity = _entity(SOURCE_CANONICAL_ID)
    target_entity = _entity(TARGET_CANONICAL_ID)
    evidence = _evidence()
    candidate_plan = build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=projection,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence=evidence,
    )
    assert candidate_plan.logical_fact is not None and candidate_plan.assertion is not None
    logical_fact = SimpleNamespace(
        id=uuid.uuid4(), identity_fingerprint=candidate_plan.logical_fact.identity_fingerprint
    )
    assertion = SimpleNamespace(
        id=uuid.uuid4(),
        logical_fact_id=logical_fact.id,
        assertion_fingerprint=candidate_plan.assertion.assertion_fingerprint,
    )
    adapted = build_raw_claim_fact_adapter_plan(
        raw_claim=raw_claim,
        projection_candidate=projection,
        predicate=predicate,
    )
    db = _ResolutionDb(
        results=[
            _Result([SimpleNamespace(stable_predicate_identity_id=predicate.id)]),
            _Result(),
            _Result(),
        ],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )
    preflight = asyncio.run(
        preflight_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=projection,
            relation_type_id=uuid.uuid4(),
            source_entity=source_entity,
            target_entity=target_entity,
            evidence_rows=[evidence],
            source=FactResolutionSource.raw_claim(raw_claim.id),
            expected_logical_fact=logical_fact,
            expected_assertion=assertion,
            candidate_adapter=lambda _predicate: adapted,
        )
    )

    assert preflight.is_resolved is False
    assert preflight.decision is not None
    assert preflight.decision.reason_code == "raw_claim_projection_semantic_conflict"
    assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]


def test_raw_claim_projection_ambiguity_is_pending_and_replays_by_source_semantics() -> None:
    raw_claim = _raw_claim()
    evidence = _evidence()
    projections = [
        {
            "relation_type_key": "holds_equity",
            "proposed_properties": {"ownership_percentage": "20%"},
            "source_canonical_entity_id": str(SOURCE_CANONICAL_ID),
            "target_canonical_entity_id": str(TARGET_CANONICAL_ID),
        },
        {
            "relation_type_key": "has_control",
            "proposed_properties": {},
            "source_canonical_entity_id": str(SOURCE_CANONICAL_ID),
            "target_canonical_entity_id": str(TARGET_CANONICAL_ID),
        },
    ]
    db = _ResolutionDb(results=[_Result(), _Result()])

    preflight = asyncio.run(
        record_raw_claim_projection_pending_fact(
            db,
            library_id=LIBRARY_ID,
            raw_claim=raw_claim,
            evidence=evidence,
            reason_code="raw_claim_projection_ambiguity",
            projection_contexts=projections,
        )
    )

    assert preflight.is_resolved is False
    assert preflight.decision is not None
    assert preflight.decision.status == "pending"
    assert preflight.decision.reason_code == "raw_claim_projection_ambiguity"
    assert preflight.decision.source_kind == "raw_claim"
    assert preflight.decision.raw_claim_id == raw_claim.id
    assert preflight.decision.logical_fact_id is None
    assert preflight.decision.fact_assertion_id is None
    assert {
        context["relation_type_key"]
        for context in preflight.decision.candidate_snapshot["projection_contexts"]
    } == {"has_control", "holds_equity"}
    assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]

    replay_db = _ResolutionDb(results=[_Result([preflight.decision])])
    replay = asyncio.run(
        record_raw_claim_projection_pending_fact(
            replay_db,
            library_id=LIBRARY_ID,
            raw_claim=_raw_claim(id=uuid.uuid4()),
            evidence=evidence,
            reason_code="raw_claim_projection_ambiguity",
            projection_contexts=projections,
        )
    )

    assert replay.decision is preflight.decision
    assert replay_db.added == []
