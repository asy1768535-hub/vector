from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.canonical_entity import CanonicalEntity
from app.models.fact_foundation import (
    FactAssertion,
    FactResolutionDecision,
    LogicalFact,
    StablePredicateIdentity,
    StablePredicateMapping,
)
from app.models.fact_reconciliation import (
    FactReconciliationAssertionAssignment,
    FactReconciliationDecision,
    FactReconciliationSource,
    FactReconciliationSourceTargetEdge,
    FactReconciliationTargetSlot,
)
from app.models.fact_reconciliation import (
    FactReconciliationCommand as FactReconciliationCommandRow,
)
from app.models.relation_evidence import RelationEvidence
from app.services import fact_lifecycle, fact_reconciliation
from app.services import graph_relation_fact_resolution as fact_resolution
from app.services.fact_lifecycle import recalculate_current_reconciled_projection_for_assertions
from app.services.fact_reconciliation import (
    FactReconciliationAssignmentInput,
    FactReconciliationCancellation,
    FactReconciliationCommand,
    FactReconciliationTargetSlotInput,
    FactReconciliationTargetSpec,
    FactReconciliationTopology,
    apply_fact_reconciliation,
    build_fact_reconciliation_precondition_fingerprint,
    cancel_pending_fact_reconciliation,
    fact_reconciliation_command_fingerprint,
    fact_reconciliation_command_json_bytes,
    planned_target_logical_fact_id,
    resolve_current_logical_fact,
)
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    LOGICAL_FACT_LOCK_SCOPE,
    STABLE_PREDICATE_LOCK_SCOPE,
)


def _run(coro):
    return asyncio.run(coro)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def scalar_one(self):
        return self.rows[0]


class _Db:
    def __init__(self, *rows):
        self.rows: dict[type, dict[uuid.UUID, object]] = {}
        for row in rows:
            self.rows.setdefault(type(row), {})[row.id] = row

    async def get(self, model, identifier):
        return self.rows.get(model, {}).get(identifier)

    async def execute(self, statement, _params=None):
        model = statement.column_descriptions[0].get("entity")
        return _Result(self.rows.get(model, {}).values())

    def add(self, row):
        self.rows.setdefault(type(row), {})[row.id] = row

    async def flush(self):
        return None


class _LockOrderDb(_Db):
    def __init__(self, *rows):
        super().__init__(*rows)
        self.events: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            self.events.append("for_update")
        return await super().execute(statement, *_args, **_kwargs)


def _predicate(library_id, *, predicate_id=None):
    now = datetime.now(timezone.utc)
    return StablePredicateIdentity(
        id=predicate_id or uuid.uuid4(),
        library_id=library_id,
        namespace="p3-test",
        key=uuid.uuid4().hex,
        contract_version="v1",
        temporal_class="static_fact",
        identity_policy_version="p3-test",
        resolution_status="resolved",
        resolution_policy=None,
        created_at=now,
        updated_at=now,
    )


def _fact(library_id, *, fact_id=None, predicate_id=None, status="active"):
    return LogicalFact(
        id=fact_id or uuid.uuid4(),
        library_id=library_id,
        stable_predicate_identity_id=predicate_id or uuid.uuid4(),
        subject_canonical_entity_id=uuid.uuid4(),
        object_kind=None,
        object_canonical_entity_id=None,
        object_value=None,
        identity_qualifiers={},
        temporal_identity_key=None,
        identity_policy_version="p3-test",
        identity_fingerprint=uuid.uuid4().hex * 2,
        status=status,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _assertion(library_id, fact_id, *, assertion_id=None, status="active"):
    now = datetime.now(timezone.utc)
    return FactAssertion(
        id=assertion_id or uuid.uuid4(),
        library_id=library_id,
        logical_fact_id=fact_id,
        knowledge_relation_id=None,
        assertion_fingerprint=uuid.uuid4().hex * 2,
        asserted_object_kind=None,
        asserted_object_canonical_entity_id=None,
        asserted_value=None,
        polarity="affirmed",
        modality="confirmed",
        qualifiers={},
        valid_time=None,
        effective_time=None,
        status=status,
        confidence=None,
        source_kind="manual",
        raw_claim_id=None,
        graph_relation_candidate_id=None,
        created_at=now,
        updated_at=now,
    )


def _evidence(library_id, assertion_id):
    return RelationEvidence(
        id=uuid.uuid4(),
        library_id=library_id,
        relation_id=uuid.uuid4(),
        fact_assertion_id=assertion_id,
        evidence_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        support_type="supports",
        quote_text=None,
        evidence_text_snapshot=None,
        source_span={"end": 2, "start": 1},
        confidence=None,
        status="active",
        created_by_job_id=None,
    )


def _canonical(library_id, canonical_id):
    now = datetime.now(timezone.utc)
    return CanonicalEntity(
        id=canonical_id,
        library_id=library_id,
        canonical_name=f"entity-{canonical_id}",
        normalized_name=f"entity-{canonical_id}",
        status="active",
        created_at=now,
        updated_at=now,
    )


def _target_spec(predicate, *, subject=None, object_value=None):
    return FactReconciliationTargetSpec(
        stable_predicate_identity_id=predicate.id,
        subject_canonical_entity_id=subject or uuid.uuid4(),
        object_kind=None,
        object_canonical_entity_id=None,
        object_value=object_value,
        identity_qualifiers={},
        temporal_identity_key=None,
        identity_policy_version=predicate.identity_policy_version,
    )


def _command(library_id, source_fact, predicate, assertion, *, target_subject, target_count=1, effect="apply", key="p3-key", assignments=None):
    slots = tuple(
        FactReconciliationTargetSlotInput(
            target_key=f"target-{index}",
            target_ref_kind="new",
            target_spec=_target_spec(predicate, subject=target_subject, object_value={"target": index}),
        )
        for index in range(target_count)
    )
    topology = (
        FactReconciliationTopology(source_fact.id, tuple(slot.target_key for slot in slots)),
    )
    assignments = assignments or (
        FactReconciliationAssignmentInput(
            source_fact.id,
            assertion.id,
            "resolved",
            slots[0].target_key,
        ),
    )
    return FactReconciliationCommand(
        library_id=library_id,
        source_logical_fact_ids=(source_fact.id,),
        source_target_topology=topology,
        target_slots=slots,
        assignments=assignments,
        requested_effect=effect,
        idempotency_key=key,
        reason_code="manual_reconciliation",
        reason_text="P3.3 test reconciliation",
        method="manual",
        evidence_refs=({"evidence": "test"},),
        actor_type="user",
        actor_id="tester",
        request_id="request-1",
    )


def _with_precondition(db, command):
    return replace(
        command,
        expected_precondition_fingerprint=_run(
            build_fact_reconciliation_precondition_fingerprint(db, command)
        ),
    )


def _decision(library_id, command_id, *, status="applied"):
    return FactReconciliationDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=command_id,
        decision_payload_fingerprint=uuid.uuid4().hex * 2,
        operation_kind="reconcile",
        requested_effect="apply",
        evaluated_outcome="applied" if status == "applied" else "pending",
        lifecycle_status=status,
        operation_payload_snapshot={},
        expected_precondition_fingerprint=uuid.uuid4().hex * 2,
        observed_precondition_fingerprint=uuid.uuid4().hex * 2,
        reason_code="test",
        reason_text="test",
        method="test",
        confidence=None,
        evidence_refs=[],
        supersedes_decision_id=None,
        actor_type="test",
        actor_id="test",
        request_id="test",
    )


def _source(library_id, command_id, decision_id, fact_id, *, state="applied"):
    return FactReconciliationSource(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=command_id,
        evolution_decision_id=decision_id,
        source_logical_fact_id=fact_id,
        supersedes_source_transition_id=None,
        resolution_state=state,
    )


def _slot(library_id, command_id, decision_id, *, target_fact_id, state="applied"):
    return FactReconciliationTargetSlot(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=command_id,
        evolution_decision_id=decision_id,
        target_key=uuid.uuid4().hex,
        target_ref_kind="existing",
        existing_logical_fact_id=target_fact_id,
        target_spec_snapshot={},
        target_identity_fingerprint=uuid.uuid4().hex * 2,
        planned_target_logical_fact_id=None,
        target_logical_fact_id=target_fact_id,
        slot_state=state,
        supersedes_target_slot_id=None,
    )


def _edge(library_id, command_id, decision_id, source, slot, *, state="applied"):
    return FactReconciliationSourceTargetEdge(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=command_id,
        evolution_decision_id=decision_id,
        source_logical_fact_id=source.source_logical_fact_id,
        source_transition_id=source.id,
        target_key=slot.target_key,
        target_slot_id=slot.id,
        edge_state=state,
        supersedes_source_target_edge_id=None,
    )


def _assignment(library_id, command_id, decision_id, source, assertion_id, *, edge=None, pending=False):
    return FactReconciliationAssertionAssignment(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=command_id,
        evolution_decision_id=decision_id,
        source_logical_fact_id=source.source_logical_fact_id,
        source_transition_id=source.id,
        fact_assertion_id=assertion_id,
        source_group_fingerprint=uuid.uuid4().hex * 2,
        partition_basis_snapshot={},
        assignment_state="pending" if pending else "resolved",
        target_key=None if pending else edge.target_key,
        source_target_edge_id=None if pending else edge.id,
        reason_code="test",
        supersedes_assignment_id=None,
    )


def test_current_logical_fact_direct_leaf_is_resolved():
    library_id = uuid.uuid4()
    fact = _fact(library_id)

    result = _run(resolve_current_logical_fact(_Db(fact), library_id, fact.id))

    assert result.status == "resolved"
    assert result.current_logical_fact_id == fact.id


def test_current_logical_fact_uses_persisted_one_to_one_lineage():
    library_id = uuid.uuid4()
    source_fact, target_fact = _fact(library_id), _fact(library_id)
    command_id = uuid.uuid4()
    decision = _decision(library_id, command_id)
    source = _source(library_id, command_id, decision.id, source_fact.id)
    slot = _slot(library_id, command_id, decision.id, target_fact_id=target_fact.id)
    edge = _edge(library_id, command_id, decision.id, source, slot)

    result = _run(
        resolve_current_logical_fact(
            _Db(source_fact, target_fact, decision, source, slot, edge), library_id, source_fact.id
        )
    )

    assert result.status == "resolved"
    assert result.current_logical_fact_id == target_fact.id
    assert result.current_target_slot_id == slot.id


def test_current_logical_fact_split_requires_persisted_context_and_reports_pending():
    library_id = uuid.uuid4()
    source_fact, left_target, right_target = _fact(library_id), _fact(library_id), _fact(library_id)
    command_id = uuid.uuid4()
    decision = _decision(library_id, command_id)
    source = _source(library_id, command_id, decision.id, source_fact.id)
    left_slot = _slot(library_id, command_id, decision.id, target_fact_id=left_target.id)
    right_slot = _slot(library_id, command_id, decision.id, target_fact_id=right_target.id)
    left_edge = _edge(library_id, command_id, decision.id, source, left_slot)
    right_edge = _edge(library_id, command_id, decision.id, source, right_slot)
    assertion_id = uuid.uuid4()
    assignment = _assignment(
        library_id, command_id, decision.id, source, assertion_id, edge=left_edge
    )
    pending_assignment = _assignment(
        library_id, command_id, decision.id, source, uuid.uuid4(), pending=True
    )
    db = _Db(
        source_fact,
        left_target,
        right_target,
        decision,
        source,
        left_slot,
        right_slot,
        left_edge,
        right_edge,
        assignment,
        pending_assignment,
    )

    assert _run(resolve_current_logical_fact(db, library_id, source_fact.id)).status == "pending"
    assert _run(
        resolve_current_logical_fact(
            db, library_id, source_fact.id, fact_assertion_id=assertion_id
        )
    ).current_logical_fact_id == left_target.id
    assert _run(
        resolve_current_logical_fact(
            db, library_id, source_fact.id, fact_assertion_id=pending_assignment.fact_assertion_id
        )
    ).status == "pending"


def test_current_projection_updates_only_the_resolved_target_fact():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source_fact = _fact(library_id, predicate_id=predicate.id, status="active")
    target_fact = _fact(library_id, predicate_id=predicate.id, status="inactive")
    assertion = _assertion(library_id, source_fact.id)
    command_id = uuid.uuid4()
    decision = _decision(library_id, command_id)
    source = _source(library_id, command_id, decision.id, source_fact.id)
    slot = _slot(library_id, command_id, decision.id, target_fact_id=target_fact.id)
    edge = _edge(library_id, command_id, decision.id, source, slot)
    assignment = _assignment(library_id, command_id, decision.id, source, assertion.id, edge=edge)
    db = _Db(
        predicate,
        source_fact,
        target_fact,
        assertion,
        decision,
        source,
        slot,
        edge,
        assignment,
    )

    _run(
        recalculate_current_reconciled_projection_for_assertions(
            db, library_id=library_id, assertion_ids={assertion.id}
        )
    )

    assert target_fact.status == "active"
    assert source_fact.status == "active"


def test_resolved_fact_decision_wires_current_projection_in_the_same_transaction(monkeypatch):
    library_id = uuid.uuid4()
    fact = _fact(library_id)
    assertion = _assertion(library_id, fact.id)
    decision = SimpleNamespace(
        library_id=library_id,
        status="resolved",
        fact_assertion_id=assertion.id,
        supersedes_decision_id=None,
    )
    calls: list[tuple[str, set[uuid.UUID], bool | None]] = []

    async def lock(_db, *, library_id, assertion_ids):
        calls.append(("lock", set(assertion_ids), None))
        return ()

    async def historic(*_args, **_kwargs):
        return "active"

    async def current(_db, *, library_id, assertion_ids, scopes_locked=False):
        calls.append(("current", set(assertion_ids), scopes_locked))
        return {fact.id}

    monkeypatch.setattr(fact_lifecycle, "lock_current_reconciled_projection_for_assertions", lock)
    monkeypatch.setattr(fact_lifecycle, "recalculate_logical_fact_status", historic)
    monkeypatch.setattr(fact_lifecycle, "recalculate_current_reconciled_projection_for_assertions", current)

    _run(fact_lifecycle.reconcile_resolved_fact_decision(_Db(fact, assertion), library_id=library_id, decision=decision))

    assert calls == [
        ("lock", {assertion.id}, None),
        ("current", {assertion.id}, True),
    ]


def test_apply_fact_reconciliation_creates_deterministic_target_and_lineage():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    command = _with_precondition(
        db, _command(library_id, source, predicate, assertion, target_subject=target_subject)
    )

    result = _run(apply_fact_reconciliation(db, command))

    assert result.status == "APPLIED"
    assert len(result.source_transitions) == len(result.target_slots) == len(result.edges) == 1
    assert result.assignments[0].assignment_state == "resolved"
    target = result.target_slots[0].target_logical_fact_id
    assert target == planned_target_logical_fact_id(
        fact_reconciliation_command_fingerprint(command), result.target_slots[0].target_key
    )
    assert db.rows[LogicalFact][target].reconciliation_target_slot_id == result.target_slots[0].id
    assert db.rows[FactReconciliationCommandRow][result.command.id].command_identity_fingerprint == (
        fact_reconciliation_command_fingerprint(command)
    )


def test_apply_locks_graph_scopes_before_any_row_lock(monkeypatch):
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _LockOrderDb(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )

    command = _with_precondition(
        db, _command(library_id, source, predicate, assertion, target_subject=target_subject)
    )
    db.events.clear()

    async def lock_scopes(*_args, **_kwargs):
        db.events.append("graph_scope")

    monkeypatch.setattr(fact_reconciliation, "lock_graph_identity_scopes", lock_scopes)

    assert _run(apply_fact_reconciliation(db, command)).status == "APPLIED"
    assert db.events.index("graph_scope") < db.events.index("for_update")


def test_precondition_token_is_a_read_only_snapshot():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _LockOrderDb(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    command = _command(library_id, source, predicate, assertion, target_subject=target_subject)

    assert _run(build_fact_reconciliation_precondition_fingerprint(db, command))
    assert db.events == []


def test_reconciliation_rejects_a_noncurrent_target_predicate(monkeypatch):
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )

    async def pending_predicate(*_args, **_kwargs):
        return SimpleNamespace(status="pending", current_predicate_id=None)

    monkeypatch.setattr(
        fact_reconciliation, "resolve_current_stable_predicate_identity", pending_predicate
    )
    command = _command(library_id, source, predicate, assertion, target_subject=target_subject)

    result = _run(apply_fact_reconciliation(db, replace(command, expected_precondition_fingerprint="0" * 64)))

    assert (result.status, result.reason_code) == ("REJECTED", "target_predicate_not_current")
    assert not db.rows.get(FactReconciliationCommandRow)


def test_reconciliation_rejects_a_noncurrent_target_canonical(monkeypatch):
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )

    async def pending_canonical(*_args, **_kwargs):
        return SimpleNamespace(
            status="pending", current_canonical_entity_id=None, resolution_eligible=False
        )

    monkeypatch.setattr(
        fact_reconciliation, "resolve_current_canonical_identity", pending_canonical
    )
    command = _command(library_id, source, predicate, assertion, target_subject=target_subject)

    result = _run(apply_fact_reconciliation(db, replace(command, expected_precondition_fingerprint="0" * 64)))

    assert (result.status, result.reason_code) == ("REJECTED", "target_canonical_not_current")
    assert not db.rows.get(FactReconciliationCommandRow)


def test_reconciliation_rejects_a_source_without_a_reproducible_group():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(predicate, source, assertion, _canonical(library_id, target_subject))
    command = _command(library_id, source, predicate, assertion, target_subject=target_subject)
    command = _with_precondition(db, command)

    result = _run(apply_fact_reconciliation(db, command))

    assert (result.status, result.reason_code) == ("REJECTED", "source_group_missing_occurrence")
    assert not db.rows.get(FactReconciliationCommandRow)


def test_reconciliation_rejects_a_source_as_its_own_target():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    source.identity_fingerprint = fact_reconciliation.logical_fact_identity_fingerprint_v1(
        library_id=library_id,
        predicate=predicate,
        subject_canonical_entity_id=source.subject_canonical_entity_id,
        object_kind=source.object_kind,
        object_canonical_entity_id=source.object_canonical_entity_id,
        object_value=source.object_value,
        identity_qualifiers=source.identity_qualifiers,
        temporal_identity_key=source.temporal_identity_key,
    )
    assertion = _assertion(library_id, source.id)
    source_canonical = _canonical(library_id, source.subject_canonical_entity_id)
    db = _Db(predicate, source, assertion, source_canonical, _evidence(library_id, assertion.id))
    slot = FactReconciliationTargetSlotInput(
        "target-0",
        "existing",
        _target_spec(
            predicate,
            subject=source.subject_canonical_entity_id,
            object_value=source.object_value,
        ),
        existing_logical_fact_id=source.id,
    )
    command = FactReconciliationCommand(
        library_id=library_id,
        source_logical_fact_ids=(source.id,),
        source_target_topology=(FactReconciliationTopology(source.id, (slot.target_key,)),),
        target_slots=(slot,),
        assignments=(
            FactReconciliationAssignmentInput(source.id, assertion.id, "resolved", slot.target_key),
        ),
        requested_effect="apply",
        idempotency_key="self-target",
        reason_code="invalid_reconciliation",
        reason_text="A fact cannot reconcile to itself.",
        method="manual",
        evidence_refs=({"evidence": "test"},),
        actor_type="user",
        actor_id="tester",
        request_id="request-self-target",
    )
    command = _with_precondition(db, command)

    result = _run(apply_fact_reconciliation(db, command))

    assert (result.status, result.reason_code) == ("REJECTED", "source_equals_target")
    assert not db.rows.get(FactReconciliationCommandRow)


def test_fact_resolution_does_not_reuse_a_historical_logical_fact(monkeypatch):
    library_id = uuid.uuid4()
    predicate = StablePredicateIdentity(
        id=uuid.uuid4(),
        library_id=library_id,
        namespace="p3-test",
        key="p3_3_test",
        contract_version="v1",
        temporal_class="state_fact",
        identity_policy_version="p3-test",
        resolution_status="resolved",
        resolution_policy={
            "schema_version": "p2_v1",
            "temporal_class": "state_fact",
            "object_policy": {"source": "target_entity"},
            "measurement_policy": None,
            "qualifier_policy": {
                "identity_bearing": [],
                "assertion_bearing": [],
                "evidence_only": [],
            },
            "polarity_policy": {"source": "fixed", "value": "affirmed"},
            "modality_policy": {"source": "fixed", "value": "confirmed"},
            "valid_time_policy": {"source": "none"},
            "effective_time_policy": {"source": "none"},
            "event_temporal_identity_policy": None,
        },
    )
    source_canonical_id, target_canonical_id = uuid.uuid4(), uuid.uuid4()
    source_entity = SimpleNamespace(id=uuid.uuid4(), canonical_entity_id=source_canonical_id)
    target_entity = SimpleNamespace(id=uuid.uuid4(), canonical_entity_id=target_canonical_id)
    candidate = SimpleNamespace(
        id=uuid.uuid4(),
        relation_type_key="p3_3_test",
        proposed_properties={},
        evidence_support_mode="single_evidence",
    )
    evidence = SimpleNamespace(
        resolved_document_id=uuid.uuid4(),
        resolved_document_revision_id=uuid.uuid4(),
        resolved_evidence_id=uuid.uuid4(),
        resolved_chunk_id=uuid.uuid4(),
        resolved_block_id=None,
        resolved_source_span={"start": 0, "end": 1},
    )
    plan = fact_resolution.build_graph_relation_fact_plan(
        library_id=library_id,
        predicate=predicate,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence=evidence,
    )
    assert plan.logical_fact is not None and plan.assertion is not None
    fact = LogicalFact(
        id=uuid.uuid4(),
        library_id=library_id,
        stable_predicate_identity_id=predicate.id,
        subject_canonical_entity_id=source_canonical_id,
        object_kind=plan.logical_fact.object_kind,
        object_canonical_entity_id=plan.logical_fact.object_canonical_entity_id,
        object_value=plan.logical_fact.object_value,
        identity_qualifiers=plan.logical_fact.identity_qualifiers,
        temporal_identity_key=plan.logical_fact.temporal_identity_key,
        identity_policy_version=predicate.identity_policy_version,
        identity_fingerprint=plan.logical_fact.identity_fingerprint,
        status="active",
    )
    assertion = FactAssertion(
        id=uuid.uuid4(),
        library_id=library_id,
        logical_fact_id=fact.id,
        knowledge_relation_id=None,
        assertion_fingerprint=plan.assertion.assertion_fingerprint,
        asserted_object_kind=plan.assertion.asserted_object_kind,
        asserted_object_canonical_entity_id=plan.assertion.asserted_object_canonical_entity_id,
        asserted_value=plan.assertion.asserted_value,
        polarity=plan.assertion.polarity,
        modality=plan.assertion.modality,
        qualifiers=plan.assertion.qualifiers,
        valid_time=plan.assertion.valid_time,
        effective_time=plan.assertion.effective_time,
        status="active",
        confidence=None,
        source_kind="manual",
        raw_claim_id=None,
        graph_relation_candidate_id=None,
    )
    mapping = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        relation_type_id=uuid.uuid4(),
        stable_predicate_identity_id=predicate.id,
        mapping_status="active",
    )
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        status="resolved",
        logical_fact_id=fact.id,
        fact_assertion_id=assertion.id,
    )

    class _BoundFactResolutionDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def get(self, model, identifier):
            return {
                (StablePredicateIdentity, predicate.id): predicate,
                (LogicalFact, fact.id): fact,
                (FactAssertion, assertion.id): assertion,
            }.get((model, identifier))

        async def execute(self, statement, _params=None):
            descriptions = getattr(statement, "column_descriptions", ())
            if not descriptions:
                return _Result((True,))
            model = descriptions[0].get("entity")
            if model is StablePredicateMapping:
                return _Result((mapping,))
            if model is StablePredicateIdentity:
                return _Result((predicate,))
            if model is FactResolutionDecision:
                return _Result((existing,))
            return _Result()

    captured = {}

    async def historical(*_args, **_kwargs):
        return fact_reconciliation.CurrentLogicalFactResult(
            "pending", fact.id, reason_code="source_pending"
        )

    async def persist_pending(*_args, **kwargs):
        captured["plan"] = kwargs["plan"]
        return "pending-only"

    monkeypatch.setattr(fact_resolution, "resolve_current_logical_fact", historical)
    monkeypatch.setattr(fact_resolution, "_persist_preflight_unresolved", persist_pending)

    result = _run(
        fact_resolution.preflight_graph_relation_candidate_fact(
            _BoundFactResolutionDb(),
            library_id=library_id,
            candidate=candidate,
            relation_type_id=mapping.relation_type_id,
            source_entity=source_entity,
            target_entity=target_entity,
            evidence_rows=[evidence],
        )
    )

    assert result == "pending-only"
    assert captured["plan"].reason_code == "logical_fact_current_identity_changed"


def test_fact_reconciliation_replay_and_alias_key_are_not_new_effects():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    command = _with_precondition(
        db, _command(library_id, source, predicate, assertion, target_subject=target_subject)
    )
    applied = _run(apply_fact_reconciliation(db, command))

    replay = _run(apply_fact_reconciliation(db, command))
    alias = _run(apply_fact_reconciliation(db, replace(command, idempotency_key="other-key")))

    assert replay.status == "REUSED"
    assert replay.reused_decision_id == applied.decision.id
    assert replay.effective_outcome == "APPLIED"
    assert replay.current_decision_status == "applied"
    assert alias.status == "REJECTED"
    assert alias.reason_code == "command_identity_alias_key"


def test_pending_split_correction_supersedes_old_proposal_and_applies():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    first = _command(
        library_id,
        source,
        predicate,
        assertion,
        target_subject=target_subject,
        target_count=2,
        effect="stage",
        assignments=(FactReconciliationAssignmentInput(source.id, assertion.id, "pending", None),),
    )
    first = _with_precondition(db, first)
    staged = _run(apply_fact_reconciliation(db, first))
    second = replace(
        first,
        requested_effect="apply",
        assignments=(FactReconciliationAssignmentInput(source.id, assertion.id, "resolved", "target-1"),),
        expected_predecessor_decision_id=staged.decision.id,
    )
    second = _with_precondition(db, second)

    applied = _run(apply_fact_reconciliation(db, second))

    assert staged.status == "PENDING"
    assert staged.decision.lifecycle_status == "superseded"
    assert applied.status == "APPLIED"
    assert applied.decision.supersedes_decision_id == staged.decision.id
    assert all(row.resolution_state == "superseded" for row in staged.source_transitions)
    assert all(row.slot_state == "superseded" for row in staged.target_slots)
    assert all(row.edge_state == "superseded" for row in staged.edges)


def test_cancel_pending_fact_reconciliation_releases_its_source_slot():
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _Db(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    command = _command(
        library_id,
        source,
        predicate,
        assertion,
        target_subject=target_subject,
        target_count=2,
        effect="stage",
        assignments=(FactReconciliationAssignmentInput(source.id, assertion.id, "pending", None),),
    )
    staged = _run(apply_fact_reconciliation(db, _with_precondition(db, command)))

    cancelled = _run(
        cancel_pending_fact_reconciliation(
            db,
            library_id=library_id,
            cancellation=FactReconciliationCancellation(
                command_id=staged.command.id,
                original_idempotency_key=command.idempotency_key,
                expected_pending_decision_id=staged.decision.id,
                reason_code="operator_cancelled",
                reason_text="Cancel the incomplete split.",
                evidence_refs=({"evidence": "cancel"},),
                actor_type="user",
                actor_id="tester",
                request_id="request-cancel",
            ),
        )
    )

    assert cancelled.status == "CANCELLED"
    assert staged.decision.lifecycle_status == "superseded"
    assert all(row.resolution_state == "superseded" for row in staged.source_transitions)


def test_pending_cancellation_locks_graph_scopes_before_any_row_lock(monkeypatch):
    library_id = uuid.uuid4()
    predicate = _predicate(library_id)
    source = _fact(library_id, predicate_id=predicate.id)
    assertion = _assertion(library_id, source.id)
    target_subject = uuid.uuid4()
    db = _LockOrderDb(
        predicate,
        source,
        assertion,
        _canonical(library_id, target_subject),
        _evidence(library_id, assertion.id),
    )
    command = _command(
        library_id,
        source,
        predicate,
        assertion,
        target_subject=target_subject,
        target_count=2,
        effect="stage",
        assignments=(FactReconciliationAssignmentInput(source.id, assertion.id, "pending", None),),
    )
    staged = _run(apply_fact_reconciliation(db, _with_precondition(db, command)))
    db.events.clear()

    captured_scopes = []

    async def lock_scopes(_db, _library_id, scopes, **_kwargs):
        db.events.append("graph_scope")
        captured_scopes.extend(scopes)

    monkeypatch.setattr(fact_reconciliation, "lock_graph_identity_scopes", lock_scopes)
    result = _run(
        cancel_pending_fact_reconciliation(
            db,
            library_id=library_id,
            cancellation=FactReconciliationCancellation(
                command_id=staged.command.id,
                original_idempotency_key=command.idempotency_key,
                expected_pending_decision_id=staged.decision.id,
                reason_code="operator_cancelled",
                reason_text="Cancel incomplete split.",
                evidence_refs=({"evidence": "cancel"},),
                actor_type="user",
                actor_id="tester",
                request_id="request-cancel",
            ),
        )
    )

    assert result.status == "CANCELLED"
    assert db.events.index("graph_scope") < db.events.index("for_update")
    assert {scope.scope_type for scope in captured_scopes} == {
        CANONICAL_ENTITY_LOCK_SCOPE,
        STABLE_PREDICATE_LOCK_SCOPE,
        LOGICAL_FACT_LOCK_SCOPE,
    }


def test_reconciliation_command_identity_has_fixed_jcs_and_uuidv5_vectors():
    library_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    source_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    predicate_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    subject_id = uuid.UUID("00000000-0000-0000-0000-000000000004")
    spec = FactReconciliationTargetSpec(
        predicate_id, subject_id, None, None, None, {}, None, "v1"
    )
    slots = (
        FactReconciliationTargetSlotInput("z", "new", spec),
        FactReconciliationTargetSlotInput("café", "new", spec),
    )
    command = FactReconciliationCommand(
        library_id,
        (source_id,),
        (FactReconciliationTopology(source_id, ("z", "café")),),
        slots,
        (),
        "apply",
        "key",
        "reason",
        "reason",
        "manual",
        (),
        "user",
        "actor",
        "request",
    )
    reversed_command = replace(
        command,
        source_target_topology=(FactReconciliationTopology(source_id, ("café", "z")),),
        target_slots=tuple(reversed(slots)),
    )

    assert fact_reconciliation_command_fingerprint(command) == (
        "4188804b225440d1031fad7273265ce0f5972d39f7f8673571601e021cdf54b9"
    )
    assert fact_reconciliation_command_json_bytes(command) == fact_reconciliation_command_json_bytes(
        reversed_command
    )
    assert planned_target_logical_fact_id(
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "café"
    ) == uuid.UUID("81c72659-3927-50c6-b47c-6a74ead9a518")
