from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.fact_foundation import StablePredicateIdentity
from app.services.graph_relation_fact_resolution import (
    _fact_resolution_lock_key,
    _lock_fact_resolution_subject,
    build_graph_relation_fact_plan,
    resolve_graph_relation_candidate_fact,
    source_occurrence_fingerprint_v1,
)


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
PREDICATE_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
SOURCE_CANONICAL_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
TARGET_CANONICAL_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")


def _policy(
    temporal_class: str = "state_fact",
    *,
    assertion_bearing: list[str] | None = None,
    measurement: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": "p2_v1",
        "temporal_class": temporal_class,
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
            "assertion_bearing": assertion_bearing or [],
            "evidence_only": [],
        },
        "polarity_policy": {"source": "fixed", "value": "affirmed"},
        "modality_policy": {"source": "fixed", "value": "confirmed"},
        "valid_time_policy": {"source": "none"},
        "effective_time_policy": {"source": "none"},
        "event_temporal_identity_policy": None,
    }


def _predicate(*, temporal_class: str = "state_fact", policy: dict[str, object] | None = None):
    return StablePredicateIdentity(
        id=PREDICATE_ID,
        library_id=LIBRARY_ID,
        namespace="v1",
        key="holds_equity",
        contract_version="v1",
        temporal_class=temporal_class,
        identity_policy_version="p2_identity_v1",
        resolution_status="resolved",
        resolution_policy=policy or _policy(temporal_class),
    )


def _candidate(*, properties: dict[str, object] | None = None, identifier: uuid.UUID | None = None):
    return SimpleNamespace(
        id=identifier or uuid.uuid4(),
        relation_type_key="holds_equity",
        proposed_properties=properties or {},
        final_confidence=0.95,
    )


def _entity(canonical_id: uuid.UUID):
    return SimpleNamespace(id=uuid.uuid4(), canonical_entity_id=canonical_id)


def _evidence(
    *,
    document_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    chunk_id: uuid.UUID | None = None,
    block_id: uuid.UUID | None = None,
    source_span: dict[str, int] | None = None,
    job_id: uuid.UUID | None = None,
):
    return SimpleNamespace(
        job_id=job_id or uuid.uuid4(),
        resolved_document_id=document_id or uuid.uuid4(),
        resolved_document_revision_id=revision_id or uuid.uuid4(),
        resolved_evidence_id=evidence_id or uuid.uuid4(),
        resolved_chunk_id=chunk_id or uuid.uuid4(),
        resolved_block_id=block_id,
        resolved_source_span=source_span or {"start": 10, "end": 20},
    )


def _plan(*, predicate, candidate, evidence):
    return build_graph_relation_fact_plan(
        library_id=LIBRARY_ID,
        predicate=predicate,
        candidate=candidate,
        source_entity=_entity(SOURCE_CANONICAL_ID),
        target_entity=_entity(TARGET_CANONICAL_ID),
        evidence=evidence,
    )


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
        self.flush_count = 0

    async def execute(self, _statement, _params=None):
        assert self.results, "unexpected fact-resolution query"
        return self.results.pop(0)

    async def get(self, model, identifier):
        return self.objects.get((model, identifier))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1


def _relation():
    return SimpleNamespace(
        id=uuid.uuid4(),
        relation_type_id=uuid.uuid4(),
        logical_fact_id=None,
    )


def _relation_evidence():
    return SimpleNamespace(fact_assertion_id=None)


def test_source_fingerprint_uses_persisted_occurrence_not_extraction_run_identity():
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    first = _evidence(
        document_id=document_id,
        revision_id=revision_id,
        evidence_id=evidence_id,
        chunk_id=chunk_id,
        job_id=uuid.uuid4(),
    )
    replay = _evidence(
        document_id=document_id,
        revision_id=revision_id,
        evidence_id=evidence_id,
        chunk_id=chunk_id,
        job_id=uuid.uuid4(),
    )
    different_occurrence = _evidence(
        document_id=document_id,
        revision_id=revision_id,
        evidence_id=evidence_id,
        chunk_id=chunk_id,
        source_span={"start": 21, "end": 31},
    )

    assert source_occurrence_fingerprint_v1(LIBRARY_ID, first) == source_occurrence_fingerprint_v1(
        LIBRARY_ID, replay
    )
    assert source_occurrence_fingerprint_v1(LIBRARY_ID, first) != source_occurrence_fingerprint_v1(
        LIBRARY_ID, different_occurrence
    )


def test_state_fact_reuses_fact_for_replay_but_keeps_distinct_source_assertions():
    predicate = _predicate()
    source = _evidence()
    replay = _evidence(
        document_id=source.resolved_document_id,
        revision_id=source.resolved_document_revision_id,
        evidence_id=source.resolved_evidence_id,
        chunk_id=source.resolved_chunk_id,
        source_span=source.resolved_source_span,
    )
    different_source = _evidence()

    first = _plan(predicate=predicate, candidate=_candidate(), evidence=source)
    same_occurrence = _plan(predicate=predicate, candidate=_candidate(), evidence=replay)
    other_occurrence = _plan(predicate=predicate, candidate=_candidate(), evidence=different_source)

    assert first.status == "resolved"
    assert first.source_snapshot["predicate"] == {
        "contract_version": "v1",
        "identity_policy_version": "p2_identity_v1",
        "key": "holds_equity",
        "namespace": "v1",
        "predicate_id": str(PREDICATE_ID),
        "temporal_class": "state_fact",
    }
    assert first.logical_fact.identity_fingerprint == same_occurrence.logical_fact.identity_fingerprint
    assert first.assertion.assertion_fingerprint == same_occurrence.assertion.assertion_fingerprint
    assert first.logical_fact.identity_fingerprint == other_occurrence.logical_fact.identity_fingerprint
    assert first.assertion.assertion_fingerprint != other_occurrence.assertion.assertion_fingerprint


def test_measurement_slot_keeps_20_and_30_in_one_logical_fact_but_separate_assertions():
    predicate = _predicate(
        temporal_class="measurement_slot",
        policy=_policy("measurement_slot", measurement=True),
    )
    twenty = _plan(
        predicate=predicate,
        candidate=_candidate(properties={"ownership_percentage": "20%"}),
        evidence=_evidence(),
    )
    thirty = _plan(
        predicate=predicate,
        candidate=_candidate(properties={"ownership_percentage": "30%"}),
        evidence=_evidence(),
    )

    assert twenty.status == thirty.status == "resolved"
    assert twenty.logical_fact.identity_fingerprint == thirty.logical_fact.identity_fingerprint
    assert twenty.assertion.asserted_value == {"kind": "decimal", "unit": "ratio", "value": "0.2"}
    assert thirty.assertion.asserted_value == {"kind": "decimal", "unit": "ratio", "value": "0.3"}
    assert twenty.assertion.assertion_fingerprint != thirty.assertion.assertion_fingerprint


def test_unclassified_candidate_property_fails_closed_to_pending():
    plan = _plan(
        predicate=_predicate(),
        candidate=_candidate(properties={"unclassified": "must not be silently ignored"}),
        evidence=_evidence(),
    )

    assert plan.status == "pending"
    assert plan.reason_code == "unclassified_property"
    assert plan.logical_fact is None
    assert plan.assertion is None


def test_resolver_creates_fact_assertion_decision_and_projection_bridges():
    predicate = _predicate()
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    candidate = _candidate()
    relation = _relation()
    relation_evidence = _relation_evidence()
    db = _ResolutionDb(
        results=[_Result([mapping]), _Result(), _Result(), _Result(), _Result()],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )

    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=candidate,
            relation=relation,
            relation_evidence=relation_evidence,
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "CREATE"
    assert result.decision.status == "resolved"
    assert result.logical_fact is not None
    assert result.assertion is not None
    assert isinstance(result.logical_fact.id, uuid.UUID)
    assert isinstance(result.assertion.id, uuid.UUID)
    assert relation.logical_fact_id == result.logical_fact.id
    assert relation_evidence.fact_assertion_id == result.assertion.id
    assert result.decision.candidate_snapshot["graph_relation_candidate_id"] == str(candidate.id)
    assert result.decision.candidate_snapshot["knowledge_relation_id"] == str(relation.id)
    assert [type(row).__name__ for row in db.added] == [
        "LogicalFact",
        "FactAssertion",
        "FactResolutionDecision",
    ]


def test_missing_or_ambiguous_predicate_mapping_stays_pending_without_fact_writes():
    candidate = _candidate()
    relation = _relation()
    evidence = _evidence()
    first_predicate = _predicate()
    second_predicate = _predicate()
    second_predicate.id = uuid.uuid4()
    for mappings, objects, expected_reason in (
        ([], {}, "predicate_mapping_pending"),
        (
            [
                SimpleNamespace(stable_predicate_identity_id=first_predicate.id),
                SimpleNamespace(stable_predicate_identity_id=second_predicate.id),
            ],
            {
                (StablePredicateIdentity, first_predicate.id): first_predicate,
                (StablePredicateIdentity, second_predicate.id): second_predicate,
            },
            "predicate_mapping_ambiguous",
        ),
    ):
        db = _ResolutionDb(
            results=[_Result(mappings), _Result(), _Result()],
            objects=objects,
        )
        result = __import__("asyncio").run(
            resolve_graph_relation_candidate_fact(
                db,
                library_id=LIBRARY_ID,
                candidate=candidate,
                relation=relation,
                relation_evidence=_relation_evidence(),
                source_entity=_entity(SOURCE_CANONICAL_ID),
                target_entity=_entity(TARGET_CANONICAL_ID),
                evidence=evidence,
            )
        )

        assert result.outcome == "PENDING"
        assert result.decision.reason_code == expected_reason
        assert result.logical_fact is None
        assert result.assertion is None
        assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]


def test_multiple_logical_fact_rows_with_one_identity_stays_pending_without_bridge_mutation():
    predicate = _predicate()
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    candidate = _candidate()
    relation = _relation()
    relation_evidence = _relation_evidence()
    duplicate_facts = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    db = _ResolutionDb(
        results=[
            _Result([mapping]),
            _Result(),
            _Result(duplicate_facts),
            _Result(),
            _Result(),
        ],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )

    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=candidate,
            relation=relation,
            relation_evidence=relation_evidence,
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "PENDING"
    assert result.decision.reason_code == "logical_fact_identity_ambiguous"
    assert relation.logical_fact_id is None
    assert relation_evidence.fact_assertion_id is None
    assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]


def test_single_existing_logical_fact_is_reused_while_a_new_source_assertion_is_created():
    predicate = _predicate()
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    existing_fact = SimpleNamespace(id=uuid.uuid4())
    relation = _relation()
    relation_evidence = _relation_evidence()
    db = _ResolutionDb(
        results=[
            _Result([mapping]),
            _Result(),
            _Result([existing_fact]),
            _Result(),
            _Result(),
        ],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )

    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=_candidate(),
            relation=relation,
            relation_evidence=relation_evidence,
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "CREATE"
    assert result.logical_fact is existing_fact
    assert result.assertion is not None
    assert relation.logical_fact_id == existing_fact.id
    assert relation_evidence.fact_assertion_id == result.assertion.id
    assert [type(row).__name__ for row in db.added] == [
        "FactAssertion",
        "FactResolutionDecision",
    ]


def test_same_source_replay_reuses_current_decision_assertion_and_bridges():
    predicate = _predicate()
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    logical_fact = SimpleNamespace(id=uuid.uuid4())
    assertion = SimpleNamespace(id=uuid.uuid4(), logical_fact_id=logical_fact.id)
    decision = SimpleNamespace(
        status="resolved",
        logical_fact_id=logical_fact.id,
        fact_assertion_id=assertion.id,
    )
    relation = _relation()
    relation_evidence = _relation_evidence()
    db = _ResolutionDb(
        results=[_Result([mapping]), _Result([decision])],
        objects={
            (StablePredicateIdentity, predicate.id): predicate,
            (type(logical_fact), logical_fact.id): logical_fact,
            (type(assertion), assertion.id): assertion,
        },
    )
    original_get = db.get

    async def get(model, identifier):
        if model.__name__ == "LogicalFact":
            return logical_fact
        if model.__name__ == "FactAssertion":
            return assertion
        return await original_get(model, identifier)

    db.get = get
    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=_candidate(),
            relation=relation,
            relation_evidence=relation_evidence,
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "REUSE"
    assert result.decision is decision
    assert relation.logical_fact_id == logical_fact.id
    assert relation_evidence.fact_assertion_id == assertion.id
    assert db.added == []


def test_rejected_predicate_persists_rejected_decision_without_fact_writes():
    predicate = _predicate()
    predicate.resolution_status = "rejected"
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    db = _ResolutionDb(
        results=[_Result([mapping]), _Result(), _Result()],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )

    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=_candidate(),
            relation=_relation(),
            relation_evidence=_relation_evidence(),
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "REJECT"
    assert result.decision.status == "rejected"
    assert result.decision.reason_code == "predicate_rejected"
    assert [type(row).__name__ for row in db.added] == ["FactResolutionDecision"]


def test_new_decision_supersedes_the_previous_current_decision_for_the_same_candidate():
    predicate = _predicate()
    mapping = SimpleNamespace(stable_predicate_identity_id=predicate.id)
    candidate = _candidate()
    previous = SimpleNamespace(id=uuid.uuid4(), status="pending")
    db = _ResolutionDb(
        results=[
            _Result([mapping]),
            _Result(),
            _Result(),
            _Result(),
            _Result([previous]),
        ],
        objects={(StablePredicateIdentity, predicate.id): predicate},
    )

    result = __import__("asyncio").run(
        resolve_graph_relation_candidate_fact(
            db,
            library_id=LIBRARY_ID,
            candidate=candidate,
            relation=_relation(),
            relation_evidence=_relation_evidence(),
            source_entity=_entity(SOURCE_CANONICAL_ID),
            target_entity=_entity(TARGET_CANONICAL_ID),
            evidence=_evidence(),
        )
    )

    assert result.outcome == "CREATE"
    assert previous.status == "superseded"
    assert result.decision.supersedes_decision_id == previous.id


def test_postgresql_fact_resolution_uses_a_transaction_advisory_lock():
    class _PostgresDb:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement, params):
            self.calls.append((statement, params))

    db = _PostgresDb()
    subject_fingerprint = "a" * 64

    __import__("asyncio").run(
        _lock_fact_resolution_subject(
            db,
            library_id=LIBRARY_ID,
            subject_fingerprint=subject_fingerprint,
        )
    )

    assert "pg_advisory_xact_lock" in str(db.calls[0][0]).lower()
    assert db.calls[0][1]["lock_key"] == _fact_resolution_lock_key(LIBRARY_ID, subject_fingerprint)
