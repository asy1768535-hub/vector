from __future__ import annotations

import asyncio
import importlib.util
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.models.canonical_entity import CanonicalEntity
from app.models.canonical_entity_evolution import CanonicalEntityEvolutionCommand
from app.models.entity import Entity
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_LINK_EXISTING,
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    ENTITY_RESOLUTION_STATUS_SUPERSEDED,
    EntityResolutionDecision,
)
from app.models.library import Library
from app.services import canonical_entity_evolution as evolution
from app.services import graph_relation_fact_resolution as fact_resolution
from app.services.canonical_entity_evolution import (
    CanonicalEvolutionCancellation,
    CanonicalEvolutionContext,
    CanonicalMergeCommand,
    CanonicalReassignCommand,
    CanonicalSplitCommand,
    CurrentCanonicalIdentityResult,
    EvolutionProjectionPartition,
    NewCanonicalSplitTarget,
    apply_canonical_evolution,
    build_evolution_precondition_fingerprint,
    cancel_pending_canonical_evolution,
    canonical_evolution_json_bytes,
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

_AUDIT_ACTOR_ID = uuid.UUID("90000000-0000-4000-8000-000000000001")


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
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id=f"p31:{key}",
    )


def test_command_identity_golden_vectors_use_rfc8785_jcs_bytes():
    library_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    first = uuid.UUID("10000000-0000-4000-8000-000000000001")
    second = uuid.UUID("10000000-0000-4000-8000-000000000002")
    third = uuid.UUID("10000000-0000-4000-8000-000000000003")
    entity_id = uuid.UUID("20000000-0000-4000-8000-000000000001")

    merge = _merge_command(library_id, second, first)
    split = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=first,
        targets=(third, second),
        partition=(),
        idempotency_key="split",
        reason_code="manual_split",
        reason_text="partition A into B and C",
        method="manual",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-golden",
    )
    reassign = CanonicalReassignCommand(
        library_id=library_id,
        entity_id=entity_id,
        from_canonical_entity_id=first,
        target_canonical_entity_id=second,
        idempotency_key="reassign",
        reason_code="manual_reassign",
        reason_text="move E1 from A to B",
        method="manual",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:reassign-golden",
    )

    expected = (
        (
            merge,
            b'{"command_scope":{"survivor_canonical_entity_id":"10000000-0000-4000-8000-000000000001"},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"merge","source_identity":{"participant_canonical_entity_ids":["10000000-0000-4000-8000-000000000001","10000000-0000-4000-8000-000000000002"]}}',
            "b4b17cdf0050dfc609920f611148d4430ccee579a6803b78c0a6ab6ea3b29cec",
        ),
        (
            split,
            b'{"command_scope":{"target_refs":[{"canonical_entity_id":"10000000-0000-4000-8000-000000000002","kind":"existing"},{"canonical_entity_id":"10000000-0000-4000-8000-000000000003","kind":"existing"}]},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"split","source_identity":{"source_canonical_entity_id":"10000000-0000-4000-8000-000000000001"}}',
            "6f88900a4a4d7cb9b5a5f3e490e34c205f4ef2acf96cfd953f11e1d568d75465",
        ),
        (
            reassign,
            b'{"command_scope":{"from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","target_canonical_entity_id":"10000000-0000-4000-8000-000000000002"},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"reassign","source_identity":{"entity_projection_id":"20000000-0000-4000-8000-000000000001"}}',
            "6212d479317b2a466f16f57b146e79b67dc5e537d6d9a024d1d3b6a8c356041f",
        ),
    )

    for command, expected_bytes, expected_hash in expected:
        encoded = canonical_evolution_json_bytes(command, identity=True)
        assert encoded == expected_bytes
        assert __import__("hashlib").sha256(encoded).hexdigest() == expected_hash


def test_decision_payload_golden_vectors_include_merge_projections_and_cancellation_cas():
    library_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    survivor = uuid.UUID("10000000-0000-4000-8000-000000000001")
    source = uuid.UUID("10000000-0000-4000-8000-000000000002")
    entity_id = uuid.UUID("20000000-0000-4000-8000-000000000001")
    expected = "a" * 64
    merge = CanonicalMergeCommand(
        library_id=library_id,
        source_canonical_entity_ids=(source,),
        survivor_canonical_entity_id=survivor,
        idempotency_key="merge-golden",
        reason_code="manual_merge",
        reason_text="merge B into A",
        method="manual",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:merge-golden",
        expected_precondition_fingerprint=expected,
        projection_assignments=(
            EvolutionProjectionPartition(entity_id, source, {}, "merge_survivor"),
        ),
    )
    cancellation = CanonicalEvolutionCancellation(
        command_id=uuid.uuid4(),
        original_idempotency_key="split-cancel",
        expected_pending_decision_id=uuid.UUID("30000000-0000-4000-8000-000000000001"),
        reason_code="incorrect_pending_intent",
        reason_text="cancel incorrect pending split",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:cancel-golden",
    )

    assert evolution._decision_payload_fingerprint(merge) == (
        "da1de0e83a1ec3e95585b963c5c11983f0a6479460c097a8ab7f8653e3f2dd16"
    )
    assert evolution._cancellation_payload_fingerprint(cancellation, expected) == (
        "17a8d1230e96fc15dea7bb9b6e816583c0c99f7e611bc2fb7d7e4ff790953564"
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
    assert result.assignments[0].target_successor_id == result.successors[0].id
    replacement = next(
        row
        for row in db.rows[EntityResolutionDecision].values()
        if row.supersedes_decision_id == previous.id
    )
    assert replacement.evolution_assignment_id == result.assignments[0].id

    replay = _run(apply_canonical_evolution(db, command))
    assert replay.status == "REUSED"
    assert replay.decision.id == result.decision.id
    assert replay.reused_decision_id == result.decision.id
    assert replay.effective_outcome == "APPLIED"
    assert replay.current_decision_status == "applied"
    assert replay.current_decision_id == result.decision.id


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
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-a",
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
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-pending",
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
    correction = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )
    correction = replace(correction, expected_predecessor_decision_id=first.decision.id)
    completed = _run(complete_pending_canonical_evolution(db, correction))

    assert completed.status == "APPLIED"
    assert completed.decision.supersedes_decision_id == first.decision.id
    assert first.decision.lifecycle_status == "superseded"
    assert completed.source_transitions[0].resolution_state == "applied"


def test_pending_split_correction_reuses_root_when_partition_changes():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    first_entity = _entity(library_id, source.id)
    second_entity = _entity(library_id, source.id)
    db = _Db(
        _library(library_id),
        source,
        left,
        right,
        first_entity,
        second_entity,
        _decision(library_id, first_entity, source.id),
        _decision(library_id, second_entity, source.id),
    )
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(
            EvolutionProjectionPartition(first_entity.id, left.id, {"rule": "left"}),
            EvolutionProjectionPartition(second_entity.id, None, {"rule": "pending"}),
        ),
        idempotency_key="split-correction",
        reason_code="identity_overload",
        reason_text="operator needs a second review",
        method="operator_review",
        evidence_refs=({"source_ref": "p3.1-test"},),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-correction",
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    first = _run(apply_canonical_evolution(db, command))
    corrected = replace(
        command,
        partition=(
            EvolutionProjectionPartition(first_entity.id, left.id, {"rule": "left"}),
            EvolutionProjectionPartition(second_entity.id, right.id, {"rule": "right"}),
        ),
    )
    corrected = corrected.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, corrected))
    )
    corrected = replace(corrected, expected_predecessor_decision_id=first.decision.id)
    completed = _run(complete_pending_canonical_evolution(db, corrected))

    assert first.status == "PENDING"
    assert completed.status == "APPLIED"
    assert completed.command.id == first.command.id
    assert completed.decision.supersedes_decision_id == first.decision.id
    assert first.decision.lifecycle_status == "superseded"
    assert second_entity.canonical_entity_id == right.id


def test_rejected_pending_split_correction_preserves_current_intent():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    first_entity = _entity(library_id, source.id)
    second_entity = _entity(library_id, source.id)
    db = _Db(
        _library(library_id),
        source,
        left,
        right,
        first_entity,
        second_entity,
        _decision(library_id, first_entity, source.id),
        _decision(library_id, second_entity, source.id),
    )
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(
            EvolutionProjectionPartition(first_entity.id, left.id, {"rule": "left"}),
            EvolutionProjectionPartition(second_entity.id, None, {"rule": "pending"}),
        ),
        idempotency_key="split-rejected-correction",
        reason_code="identity_overload",
        reason_text="projection requires review",
        method="operator_review",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-rejected-correction",
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )
    pending = _run(apply_canonical_evolution(db, command))
    assert pending.decision is not None

    rejected = replace(
        command,
        partition=(EvolutionProjectionPartition(first_entity.id, left.id, {"rule": "left"}),),
    )
    rejected = rejected.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, rejected))
    )
    rejected = replace(
        rejected,
        expected_predecessor_decision_id=pending.decision.id,
    )
    result = _run(complete_pending_canonical_evolution(db, rejected))

    assert result.status == "REJECTED"
    assert result.reason_code == "projection_partition_incomplete"
    assert pending.decision.lifecycle_status == "pending"
    assert len(db.rows[type(pending.decision)]) == 1
    assert {row.assignment_state for row in pending.assignments} == {"pending"}


def test_pending_cancellation_appends_terminal_decision_and_releases_source_slot():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    entity = _entity(library_id, source.id)
    db = _Db(_library(library_id), source, left, right, entity, _decision(library_id, entity, source.id))
    command = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(EvolutionProjectionPartition(entity.id, None, {"rule": "ambiguous"}),),
        idempotency_key="split-cancel",
        reason_code="identity_overload",
        reason_text="projection requires review",
        method="operator_review",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-cancel",
    )
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )
    pending = _run(apply_canonical_evolution(db, command))

    cancellation = CanonicalEvolutionCancellation(
        command_id=pending.command.id,
        original_idempotency_key=command.idempotency_key,
        expected_pending_decision_id=pending.decision.id,
        reason_code="incorrect_pending_intent",
        reason_text="cancel incorrect pending split",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:cancel-split",
    )
    cancelled = _run(cancel_pending_canonical_evolution(db, library_id, cancellation))

    assert cancelled.status == "CANCELLED"
    assert cancelled.decision.supersedes_decision_id == pending.decision.id
    assert pending.decision.lifecycle_status == "superseded"
    assert pending.source_transitions[0].resolution_state == "superseded"
    assert pending.assignments[0].assignment_state == "superseded"
    assert cancelled.source_transitions == ()
    assert cancelled.successors == ()
    assert cancelled.assignments == ()


def test_pending_source_slot_rejects_new_identity_until_cancellation_commits():
    library_id = uuid.uuid4()
    source = _canonical(library_id, "A")
    left = _canonical(library_id, "B")
    right = _canonical(library_id, "C")
    extra = _canonical(library_id, "D")
    entity = _entity(library_id, source.id)
    db = _Db(_library(library_id), source, left, right, extra, entity, _decision(library_id, entity, source.id))
    first = CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source.id,
        targets=(left.id, right.id),
        partition=(EvolutionProjectionPartition(entity.id, None, {"rule": "ambiguous"}),),
        idempotency_key="split-slot-first",
        reason_code="identity_overload",
        reason_text="projection requires review",
        method="operator_review",
        evidence_refs=(),
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:slot-first",
    )
    first = first.with_expected_precondition(_run(build_evolution_precondition_fingerprint(db, first)))
    assert _run(apply_canonical_evolution(db, first)).status == "PENDING"

    competing = replace(first, targets=(left.id, extra.id), idempotency_key="split-slot-second")
    competing = competing.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, competing))
    )
    result = _run(apply_canonical_evolution(db, competing))

    assert result.status == "STALE_OPERATION"
    assert result.reason_code == "source_or_projection_slot_occupied"
    assert len(db.rows[CanonicalEntityEvolutionCommand]) == 1


def test_projection_reassignment_replaces_every_active_supporting_subject():
    library_id = uuid.uuid4()
    loser = _canonical(library_id, "A")
    survivor = _canonical(library_id, "B")
    entity = _entity(library_id, loser.id)
    first_previous = _decision(library_id, entity, loser.id, subject="a" * 64)
    second_previous = _decision(library_id, entity, loser.id, subject="b" * 64)
    db = _Db(_library(library_id), loser, survivor, entity, first_previous, second_previous)
    command = _merge_command(library_id, loser.id, survivor.id, key="merge-many-subjects")
    command = command.with_expected_precondition(
        _run(build_evolution_precondition_fingerprint(db, command))
    )

    result = _run(apply_canonical_evolution(db, command))

    assert result.status == "APPLIED"
    assert first_previous.lifecycle_status == ENTITY_RESOLUTION_STATUS_SUPERSEDED
    assert second_previous.lifecycle_status == ENTITY_RESOLUTION_STATUS_SUPERSEDED
    replacements = [
        row
        for row in db.rows[EntityResolutionDecision].values()
        if row.evolution_assignment_id == result.assignments[0].id
    ]
    assert {row.supersedes_decision_id for row in replacements} == {
        first_previous.id,
        second_previous.id,
    }
    assert {row.subject_fingerprint for row in replacements} == {"a" * 64, "b" * 64}


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
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:split-new-targets",
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
    reverse_result = _run(apply_canonical_evolution(db, reverse))
    assert reverse_result.status == "STALE_OPERATION"
    assert reverse_result.reason_code == "current_identity_changed"


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
        actor_type="user",
        actor_id=_AUDIT_ACTOR_ID,
        request_id="p31:reassign-a",
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
