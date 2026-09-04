from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.models.canonical_entity import CanonicalEntity
from app.models.entity import Entity
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_LINK_EXISTING,
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    EntityResolutionDecision,
)
from app.models.library import Library
from app.services import graph_relation_fact_resolution as fact_resolution
from app.services.canonical_entity_evolution import (
    CanonicalEvolutionContext,
    CanonicalMergeCommand,
    CanonicalReassignCommand,
    CanonicalSplitCommand,
    CurrentCanonicalIdentityResult,
    EvolutionProjectionPartition,
    NewCanonicalSplitTarget,
    apply_canonical_evolution,
    build_evolution_precondition_fingerprint,
    complete_pending_canonical_evolution,
    resolve_current_canonical_identity,
)
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    ENTITY_PROJECTION_LOCK_SCOPE,
    ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
    normalized_graph_identity_scopes,
)


def _run(coro):
    return asyncio.run(coro)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def scalar_one_or_none(self):
        if len(self.rows) > 1:
            raise AssertionError("unexpected multiple rows")
        return self.rows[0] if self.rows else None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Db:
    """Small unit-test session that serves library-scoped model rows."""

    def __init__(self, *rows):
        self.rows: dict[type, dict[uuid.UUID, object]] = {}
        self.added: list[object] = []
        self.lock_keys: list[int] = []
        for row in rows:
            self._store(row)

    def _store(self, row):
        self.rows.setdefault(type(row), {})[row.id] = row

    async def get(self, model, identifier):
        return self.rows.get(model, {}).get(identifier)

    async def execute(self, statement, params=None):
        if params and "lock_key" in params:
            self.lock_keys.append(params["lock_key"])
            return _Result()
        descriptions = getattr(statement, "column_descriptions", ())
        if not descriptions:
            return _Result()
        model = descriptions[0].get("entity")
        return _Result(self.rows.get(model, {}).values())

    def add(self, row):
        self.added.append(row)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def begin_nested(self):
        return _Nested()

    async def flush(self):
        pending = list(self.added)
        self.added.clear()
        for row in pending:
            self._store(row)


def _library(library_id):
    return Library(id=library_id, slug=f"p31-{library_id.hex[:8]}", name="P3.1")


def _canonical(library_id, name, *, canonical_id=None, status="active"):
    now = datetime.now(timezone.utc)
    return CanonicalEntity(
        id=canonical_id or uuid.uuid4(),
        library_id=library_id,
        canonical_name=name,
        normalized_name=name.lower(),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _entity(library_id, canonical_id, *, entity_id=None):
    now = datetime.now(timezone.utc)
    return Entity(
        id=entity_id or uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=uuid.uuid4(),
        entity_type_id=uuid.uuid4(),
        canonical_entity_id=canonical_id,
        canonical_name="projection",
        normalized_name="projection",
        status="active",
        source_type="extracted",
        created_at=now,
        updated_at=now,
    )


def _decision(library_id, entity, canonical_id, *, subject=None):
    return EntityResolutionDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        subject_fingerprint=subject or uuid.uuid4().hex * 2,
        decision_fingerprint=uuid.uuid4().hex * 2,
        graph_entity_candidate_id=None,
        entity_id=entity.id,
        canonical_entity_id=canonical_id,
        observed_name="projection",
        observed_normalized_name="projection",
        observed_type_key=None,
        identifier_snapshot=None,
        candidate_snapshot=[],
        evidence_refs=[{"source_ref": "p3.1-test"}],
        decision_kind=ENTITY_RESOLUTION_LINK_EXISTING,
        lifecycle_status=ENTITY_RESOLUTION_STATUS_ACTIVE,
        method="existing_mapping",
        confidence=1.0,
        reason_code=None,
        resolver_version="entity_resolution_v1",
        supersedes_decision_id=None,
        created_at=datetime.now(timezone.utc),
    )


def _merge_command(library_id, source_id, survivor_id, *, key="merge-a"):
    return CanonicalMergeCommand(
        library_id=library_id,
        source_canonical_entity_ids=(source_id,),
        survivor_canonical_entity_id=survivor_id,
        idempotency_key=key,
        reason_code="duplicate_identity",
        reason_text="operator confirmed duplicate canonical identities",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
    )


def test_merge_records_append_only_lineage_and_reassigns_projection():
    library_id = uuid.uuid4()
    loser = _canonical(library_id, "A")
    survivor = _canonical(library_id, "B")
    entity = _entity(library_id, loser.id)
    previous = _decision(library_id, entity, loser.id)
    db = _Db(_library(library_id), loser, survivor, entity, previous)
    command = _merge_command(library_id, loser.id, survivor.id)
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "APPLIED"
    assert entity.canonical_entity_id == survivor.id
    assert previous.lifecycle_status == "superseded"
    assert result.decision is not None
    assert result.decision.operation_kind == "merge"
    assert result.source_transitions[0].source_canonical_entity_id == loser.id
    assert result.successors[0].target_canonical_entity_id == survivor.id
    assert result.assignments[0].previous_entity_resolution_decision_id == previous.id
    assert result.assignments[0].new_entity_resolution_decision_id is not None

    replay = _run(apply_canonical_evolution(db, command))
    assert replay.status == "REUSED"
    assert replay.decision.id == result.decision.id


def test_split_returns_forked_without_context_and_resolves_by_persisted_projection():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    left_entity = _entity(library_id, source.id)
    right_entity = _entity(library_id, source.id)
    left_previous = _decision(library_id, left_entity, source.id)
    right_previous = _decision(library_id, right_entity, source.id)
    db = _Db(
        _library(library_id), source, left, right, left_entity, right_entity, left_previous, right_previous
    )
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(
            EvolutionProjectionPartition(left_entity.id, left.id, {"rule": "left"}),
            EvolutionProjectionPartition(right_entity.id, right.id, {"rule": "right"}),
        ),
        idempotency_key="split-a",
        reason_code="identity_overload",
        reason_text="operator partitioned projections",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
    ).with_expected_precondition(None)
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    assert _run(apply_canonical_evolution(db, command)).status == "APPLIED"
    forked = _run(resolve_current_canonical_identity(db, library_id, source.id))
    left_resolution = _run(
        resolve_current_canonical_identity(
            db, library_id, source.id, CanonicalEvolutionContext(entity_id=left_entity.id)
        )
    )
    assert forked.status == "forked"
    assert left_resolution.status == "resolved"
    assert left_resolution.current_canonical_entity_id == left.id


def test_ambiguous_split_is_pending_and_does_not_change_projection():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    entity = _entity(library_id, source.id)
    previous = _decision(library_id, entity, source.id)
    db = _Db(_library(library_id), source, left, right, entity, previous)
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(EvolutionProjectionPartition(entity.id, None, {"rule": "ambiguous"}),),
        idempotency_key="split-pending",
        reason_code="identity_overload",
        reason_text="projection cannot be uniquely partitioned",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "PENDING"
    assert entity.canonical_entity_id == source.id
    assert previous.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    assert result.assignments[0].assignment_state == "pending"


def test_pending_command_completion_appends_d2_and_supersedes_d1():
    library_id = uuid.uuid4()
    loser = _canonical(library_id, "A")
    survivor = _canonical(library_id, "B")
    entity = _entity(library_id, loser.id)
    db = _Db(_library(library_id), loser, survivor, entity)
    command = _merge_command(library_id, loser.id, survivor.id, key="merge-pending")
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )
    first = _run(apply_canonical_evolution(db, command))
    assert first.status == "PENDING"
    assert first.decision is not None

    db._store(_decision(library_id, entity, loser.id))
    completed = _run(complete_pending_canonical_evolution(db, command))

    assert completed.status == "APPLIED"
    assert completed.decision.supersedes_decision_id == first.decision.id
    assert first.decision.lifecycle_status == "superseded"
    assert completed.source_transitions[0].resolution_state == "applied"


def test_fully_partitioned_split_creates_only_explicit_new_targets():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    first_entity = _entity(library_id, source.id)
    second_entity = _entity(library_id, source.id)
    db = _Db(
        _library(library_id),
        source,
        first_entity,
        second_entity,
        _decision(library_id, first_entity, source.id),
        _decision(library_id, second_entity, source.id),
    )
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(
            NewCanonicalSplitTarget("first", "B", "b"),
            NewCanonicalSplitTarget("second", "C", "c"),
        ),
        partition=(
            EvolutionProjectionPartition(first_entity.id, "first", {"rule": "first"}),
            EvolutionProjectionPartition(second_entity.id, "second", {"rule": "second"}),
        ),
        idempotency_key="split-new-targets",
        reason_code="identity_overload",
        reason_text="operator supplied both successor identities",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "APPLIED"
    assert {entity.canonical_entity_id for entity in (first_entity, second_entity)} == {
        successor.target_canonical_entity_id for successor in result.successors
    }
    assert len(db.rows[CanonicalEntity]) == 3


def test_idempotency_conflict_and_cycle_are_rejected_without_new_lineage():
    library_id = uuid.uuid4()
    first = _canonical(library_id, "A")
    second = _canonical(library_id, "B")
    db = _Db(_library(library_id), first, second)
    command = _merge_command(library_id, first.id, second.id, key="same-key")
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )
    assert _run(apply_canonical_evolution(db, command)).status == "APPLIED"

    conflicting = _merge_command(library_id, second.id, first.id, key="same-key")
    conflicting = conflicting.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, conflicting))
    )
    assert _run(apply_canonical_evolution(db, conflicting)).reason_code == "idempotency_key_conflict"

    reverse = _merge_command(library_id, second.id, first.id, key="reverse")
    reverse = reverse.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, reverse))
    )
    assert _run(apply_canonical_evolution(db, reverse)).reason_code == "cycle"


def test_standalone_projection_reassignment_has_no_global_lineage():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    target = _canonical(library_id, "B")
    entity = _entity(library_id, source.id)
    db = _Db(_library(library_id), source, target, entity, _decision(library_id, entity, source.id))
    command = CanonicalReassignCommand(
        library_id=library_id,
        entity_id=entity.id,
        from_canonical_entity_id=source.id,
        target_canonical_entity_id=target.id,
        idempotency_key="reassign-a",
        reason_code="projection_correction",
        reason_text="operator corrected this ontology projection",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "APPLIED"
    assert entity.canonical_entity_id == target.id
    assert not result.source_transitions
    assert not result.successors


def test_cross_library_canonical_input_is_rejected():
    library_id = uuid.uuid4()
    other_library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    other_survivor = _canonical(other_library_id, "B")
    db = _Db(_library(library_id), _library(other_library_id), source, other_survivor)
    command = _merge_command(library_id, source.id, other_survivor.id, key="cross-library")
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "REJECTED"
    assert result.reason_code == "canonical_scope_mismatch"


def test_disabled_leaf_is_resolved_but_not_eligible_and_historical_bridge_is_unchanged():
    library_id = uuid.uuid4()
    disabled = _canonical(library_id, "A", status="disabled")
    historical_fact = SimpleNamespace(subject_canonical_entity_id=disabled.id)
    db = _Db(_library(library_id), disabled)

    result = _run(resolve_current_canonical_identity(db, library_id, disabled.id))

    assert result.status == "resolved"
    assert result.current_canonical_entity_id == disabled.id
    assert result.resolution_eligible is False
    assert historical_fact.subject_canonical_entity_id == disabled.id


def test_lock_contract_deduplicates_and_uses_total_order():
    library_id = uuid.uuid4()
    first = uuid.UUID("00000000-0000-0000-0000-000000000002")
    second = uuid.UUID("00000000-0000-0000-0000-000000000001")
    db = _Db()

    _run(
        lock_graph_identity_scopes(
            db,
            library_id,
            (
                GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, first),
                GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, first),
                GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, first),
                GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, second),
                GraphIdentityLockScope(ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE, "a" * 64),
            ),
        )
    )

    assert len(db.lock_keys) == 4
    assert [scope.scope_type for scope in normalized_graph_identity_scopes(
        library_id,
        (
            GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, first),
            GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, first),
            GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, first),
            GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, second),
            GraphIdentityLockScope(ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE, "a" * 64),
        ),
    )] == [
        CANONICAL_ENTITY_LOCK_SCOPE,
        ENTITY_PROJECTION_LOCK_SCOPE,
        ENTITY_PROJECTION_LOCK_SCOPE,
        ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE,
    ]


def test_fact_preflight_current_canonical_guard_stays_pending(monkeypatch):
    library_id = uuid.uuid4()
    source = SimpleNamespace(id=uuid.uuid4(), library_id=library_id, canonical_entity_id=uuid.uuid4())
    target = SimpleNamespace(id=uuid.uuid4(), library_id=library_id, canonical_entity_id=uuid.uuid4())
    candidate = SimpleNamespace(
        id=uuid.uuid4(),
        evidence_support_mode="single_evidence",
        proposed_properties={},
        relation_type_key="p3_1_test",
    )
    evidence = SimpleNamespace(
        resolved_document_id=uuid.uuid4(),
        resolved_document_revision_id=uuid.uuid4(),
        resolved_evidence_id=uuid.uuid4(),
        resolved_chunk_id=uuid.uuid4(),
        resolved_source_span={"start": 0, "end": 1},
    )
    captured = {}

    async def current(*_args, **_kwargs):
        return CurrentCanonicalIdentityResult(
            "forked", source.canonical_entity_id, None, (), False, "split_context_required"
        )

    async def pending(*_args, **kwargs):
        captured["plan"] = kwargs["plan"]
        return "pending-only"

    monkeypatch.setattr(fact_resolution, "resolve_current_canonical_identity", current)
    monkeypatch.setattr(fact_resolution, "_persist_preflight_unresolved", pending)

    result = _run(
        fact_resolution.preflight_graph_relation_candidate_fact(
            object(),
            library_id=library_id,
            candidate=candidate,
            relation_type_id=uuid.uuid4(),
            source_entity=source,
            target_entity=target,
            evidence_rows=[evidence],
        )
    )

    assert result == "pending-only"
    assert captured["plan"].status == "pending"
    assert captured["plan"].reason_code == "canonical_source_forked"


def test_migration_declares_scoped_append_only_evolution_contract():
    root = Path(__file__).parents[1]
    path = root / "alembic" / "versions" / "0071_canonical_entity_evolution.py"
    spec = importlib.util.spec_from_file_location("migration_0071", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = path.read_text(encoding="utf-8")

    assert module.revision == "0071"
    assert module.down_revision == "0070"
    for table in (
        "canonical_entity_evolution_commands",
        "canonical_entity_evolution_decisions",
        "canonical_entity_evolution_sources",
        "canonical_entity_evolution_successors",
        "canonical_entity_projection_assignments",
    ):
        assert table in source
    assert "uq_entity_resolution_decisions_id_library" in source
    assert "lifecycle_status = 'pending'" in source
    assert "lifecycle_status = 'applied'" in source
