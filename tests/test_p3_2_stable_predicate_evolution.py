from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.fact_foundation import StablePredicateIdentity, StablePredicateMapping
from app.models.library import Library
from app.models.stable_predicate_evolution import (
    StablePredicateEvolutionCommand,
    StablePredicateEvolutionDecision,
    StablePredicateEvolutionSource,
    StablePredicateEvolutionSuccessor,
    StablePredicateMappingEvolutionAssignment,
)
from app.services import graph_relation_fact_resolution as fact_resolution
from app.services import stable_predicate_evolution as evolution
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    LOGICAL_FACT_LOCK_SCOPE,
    STABLE_PREDICATE_LOCK_SCOPE,
    GraphIdentityLockBusy,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    credential_organization_scope,
)
from app.services.stable_predicate_evolution import (
    StablePredicateEvolutionAuthorizationError,
    StablePredicateEvolutionCancellation,
    StablePredicateEvolutionContext,
    StablePredicateEvolutionRetryableConflict,
    StablePredicateMappingPartition,
    StablePredicateMergeCommand,
    StablePredicateReassignCommand,
    StablePredicateSplitCommand,
    StablePredicateSuccessorSlot,
    StablePredicateTargetSpec,
    apply_stable_predicate_evolution,
    build_stable_predicate_precondition_fingerprint,
    cancel_pending_stable_predicate_evolution,
    resolve_current_stable_predicate_identity,
    stable_predicate_cancellation_fingerprint,
    stable_predicate_command_fingerprint,
    stable_predicate_command_json_bytes,
    stable_predicate_decision_fingerprint,
    stable_predicate_target_fingerprint,
    stable_predicate_target_id,
)
from app.services.stable_predicate_evolution_actor import (
    bind_credential_api_key_audit_identity,
    credential_api_key_audit_identity,
)


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _PostgresLockDb:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.statements: list[str] = []
        self.sync_session = SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        )

    async def execute(self, statement, _params):
        self.statements.append(str(statement))
        return _ScalarResult(self.acquired)


def _run(coro):
    return asyncio.run(coro)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_superuser=True)


def _prepare(db, command, *, user=None, credential_kind="session"):
    return _run(
        build_stable_predicate_precondition_fingerprint(
            db,
            command,
            user=user or _user(),
            credential_kind=credential_kind,
        )
    )


def _apply(db, command, *, user=None, credential_kind="session"):
    return _run(
        apply_stable_predicate_evolution(
            db,
            command,
            user=user or _user(),
            credential_kind=credential_kind,
        )
    )


def _cancel(db, library_id, cancellation, *, user=None, credential_kind="session"):
    return _run(
        cancel_pending_stable_predicate_evolution(
            db,
            library_id,
            cancellation,
            user=user or _user(),
            credential_kind=credential_kind,
        )
    )


@pytest.fixture(autouse=True)
def _allow_library_management(monkeypatch):
    async def allow(_db, *, user, library):
        return SimpleNamespace(user=user, library=library)

    monkeypatch.setattr(evolution, "authorize_library_management", allow)


def test_graph_identity_lock_fail_fast_preserves_blocking_default() -> None:
    library_id = uuid.uuid4()
    predicate_id = uuid.uuid4()
    scope = GraphIdentityLockScope(STABLE_PREDICATE_LOCK_SCOPE, predicate_id)

    blocking = _PostgresLockDb()
    _run(lock_graph_identity_scopes(blocking, library_id, (scope,)))
    assert "pg_advisory_xact_lock" in blocking.statements[0]
    assert "pg_try_advisory_xact_lock" not in blocking.statements[0]

    busy = _PostgresLockDb(acquired=False)
    with pytest.raises(GraphIdentityLockBusy):
        _run(lock_graph_identity_scopes(busy, library_id, (scope,), wait=False))
    assert "pg_try_advisory_xact_lock" in busy.statements[0]


def test_fact_resolution_shared_lock_uses_only_scopes_10_20_30_fail_fast(
    monkeypatch,
) -> None:
    library_id = uuid.uuid4()
    canonical_ids = (uuid.uuid4(), uuid.uuid4())
    predicate_id = uuid.uuid4()
    calls = []

    async def capture(_db, actual_library_id, scopes, *, wait=True):
        calls.append((actual_library_id, tuple(scopes), wait))

    monkeypatch.setattr(fact_resolution, "lock_graph_identity_scopes", capture)
    _run(
        fact_resolution._lock_fact_resolution_identity_scopes(
            object(),
            library_id=library_id,
            canonical_entity_ids=canonical_ids,
            predicate_id=predicate_id,
            logical_fact_fingerprint="f" * 64,
        )
    )

    assert calls[0][0] == library_id
    assert calls[0][2] is False
    assert [scope.scope_type for scope in calls[0][1]] == [
        CANONICAL_ENTITY_LOCK_SCOPE,
        CANONICAL_ENTITY_LOCK_SCOPE,
        STABLE_PREDICATE_LOCK_SCOPE,
        LOGICAL_FACT_LOCK_SCOPE,
    ]


def test_api_key_audit_identity_is_separate_from_organization_scope() -> None:
    user = SimpleNamespace()
    organization_id = uuid.uuid4()
    api_key_id = uuid.uuid4()

    bind_credential_api_key_audit_identity(user, organization_id, api_key_id)

    identity = credential_api_key_audit_identity(user)
    assert identity is not None
    assert identity.organization_id == organization_id
    assert identity.api_key_id == api_key_id
    assert credential_organization_scope(user) is None


def test_mutation_authorizes_before_command_or_predicate_lookup(monkeypatch) -> None:
    library_id = uuid.uuid4()
    source_id, target_id = uuid.uuid4(), uuid.uuid4()
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, target_id, "target"),
    )
    command = replace(
        _with_envelope(
            StablePredicateMergeCommand(library_id, (source_id,), target_id),
            "authorization-order",
        ),
        expected_precondition_fingerprint="a" * 64,
    )

    async def deny(_db, *, user, library):
        raise OrganizationAuthorizationError("organization_forbidden")

    monkeypatch.setattr(evolution, "authorize_library_management", deny)
    with pytest.raises(StablePredicateEvolutionAuthorizationError):
        _apply(db, command)

    assert db.executed_models == [Library]
    assert db.added == []


def test_server_derives_api_key_actor_and_rejects_unbound_api_key() -> None:
    library_id = uuid.uuid4()
    source_id, target_id = uuid.uuid4(), uuid.uuid4()
    mapping = _mapping(library_id, source_id, uuid.uuid4())
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, target_id, "target"),
        mapping,
    )
    command = _with_envelope(
        StablePredicateReassignCommand(library_id, mapping.id, source_id, target_id),
        "api-key-actor",
        effect="stage",
    )
    unbound_user = _user()
    with pytest.raises(StablePredicateEvolutionAuthorizationError):
        _prepare(db, command, user=unbound_user, credential_kind="api_key")

    api_user = _user()
    api_key_id = uuid.uuid4()
    organization_id = db.rows_by_model[Library][library_id].organization_id
    bind_credential_api_key_audit_identity(api_user, organization_id, api_key_id)
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(
            db,
            command,
            user=api_user,
            credential_kind="api_key",
        ),
    )
    result = _apply(db, command, user=api_user, credential_kind="api_key")

    assert result.status == "PENDING"
    assert result.decision.actor_type == "service"
    assert result.decision.actor_id == api_key_id


def test_caller_supplied_actor_is_rejected_before_authorization(monkeypatch) -> None:
    library_id = uuid.uuid4()
    command = replace(
        _with_envelope(
            StablePredicateMergeCommand(library_id, (uuid.uuid4(),), uuid.uuid4()),
            "forged-actor",
        ),
        expected_precondition_fingerprint="a" * 64,
        actor_type="service",
        actor_id=uuid.uuid4(),
    )
    called = False

    async def should_not_run(_db, *, user, library):
        nonlocal called
        called = True

    monkeypatch.setattr(evolution, "authorize_library_management", should_not_run)
    result = _apply(_MutationDb(), command)

    assert result.status == "REJECTED"
    assert result.reason_code == "invalid_envelope"
    assert called is False


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    (
        ({"idempotency_key": "e\u0301"}, "invalid_envelope"),
        ({"reason_code": "Not_Canonical"}, "invalid_envelope"),
        (
            {
                "evidence_refs": tuple(
                    {"source_ref": f"ref-{index}"} for index in range(257)
                )
            },
            "payload_too_large",
        ),
    ),
)
def test_mutation_rejects_noncanonical_or_oversized_envelope_before_lookup(
    changes,
    expected_reason,
) -> None:
    library_id = uuid.uuid4()
    command = replace(
        _with_envelope(
            StablePredicateMergeCommand(library_id, (uuid.uuid4(),), uuid.uuid4()),
            "bounded-envelope",
        ),
        expected_precondition_fingerprint="a" * 64,
        **changes,
    )
    db = _MutationDb()

    result = _apply(db, command)

    assert result.status == "REJECTED"
    assert result.reason_code == expected_reason
    assert db.executed_models == []


def test_split_rejects_assignment_count_and_json_depth_before_lookup() -> None:
    library_id = uuid.uuid4()
    source_id, left_id, right_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    too_many = tuple(
        StablePredicateMappingPartition(uuid.uuid4(), "pending", None)
        for _ in range(4097)
    )
    command = replace(
        _with_envelope(
            StablePredicateSplitCommand(
                library_id,
                source_id,
                (
                    StablePredicateSuccessorSlot.existing(left_id),
                    StablePredicateSuccessorSlot.existing(right_id),
                ),
                mapping_assignments=too_many,
            ),
            "too-many-assignments",
            effect="stage",
        ),
        expected_precondition_fingerprint="a" * 64,
    )
    assert _apply(_MutationDb(), command).reason_code == "payload_too_large"

    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    deep_command = replace(
        command,
        mapping_assignments=(
            StablePredicateMappingPartition(
                uuid.uuid4(),
                "pending",
                None,
                deep,
            ),
        ),
        idempotency_key="deep-partition",
    )
    assert _apply(_MutationDb(), deep_command).reason_code == "payload_too_large"


def test_cancellation_rejects_invalid_or_deep_envelope_before_lookup() -> None:
    library_id = uuid.uuid4()
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    cancellation = StablePredicateEvolutionCancellation(
        command_id=uuid.uuid4(),
        original_idempotency_key="cancel-deep",
        expected_pending_decision_id=uuid.uuid4(),
        reason_code="incorrect_pending_intent",
        reason_text="cancel invalid pending intent",
        evidence_refs=(deep,),
        request_id="p32:cancel-deep",
    )
    db = _MutationDb()

    result = _cancel(db, library_id, cancellation)

    assert result.status == "REJECTED"
    assert result.reason_code == "payload_too_large"
    assert db.executed_models == []


def _target_spec() -> StablePredicateTargetSpec:
    return StablePredicateTargetSpec(
        namespace="urn:test:predicate",
        key="target-c",
        contract_version="v1",
        temporal_class="static_fact",
        identity_policy_version="p2_identity_v1",
        resolution_policy={
            "effective_time_policy": {"source": "none"},
            "event_temporal_identity_policy": None,
            "measurement_policy": None,
            "modality_policy": {"source": "fixed", "value": "confirmed"},
            "object_policy": {"source": "target_entity"},
            "polarity_policy": {"source": "fixed", "value": "affirmed"},
            "qualifier_policy": {
                "assertion_bearing": [],
                "evidence_only": [],
                "identity_bearing": [],
            },
            "schema_version": "p2_v1",
            "temporal_class": "static_fact",
            "valid_time_policy": {"source": "none"},
        },
    )


def test_stable_predicate_command_identity_golden_vectors() -> None:
    library_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    predicate_a = uuid.UUID("10000000-0000-4000-8000-000000000001")
    predicate_b = uuid.UUID("10000000-0000-4000-8000-000000000002")
    mapping_id = uuid.UUID("20000000-0000-4000-8000-000000000001")
    target = _target_spec()

    assert stable_predicate_target_fingerprint(target) == (
        "3d7756b5823c55e8c0bbcc3f8e1f91837c268f72497e40fff75d65ab9da6e2d4"
    )
    target_id = stable_predicate_target_id(library_id, target)
    assert target_id == uuid.UUID("b0700668-7622-55ce-9ba5-b62cc7b260c1")

    merge = StablePredicateMergeCommand(library_id, (predicate_b,), predicate_a)
    split = StablePredicateSplitCommand(
        library_id,
        predicate_a,
        (
            StablePredicateSuccessorSlot.existing(predicate_b),
            StablePredicateSuccessorSlot.new(target_id, target),
        ),
    )
    reassign = StablePredicateReassignCommand(
        library_id, mapping_id, predicate_a, predicate_b
    )

    assert stable_predicate_command_fingerprint(merge) == (
        "1752568c139591e1d57b3721b70ec286ebb2a3b1a8c77d4ef64b765394c6162f"
    )
    assert stable_predicate_command_fingerprint(split) == (
        "d7d34bf1914f9cff25b1d9bebe7f5106803dffe7c3afa487cccd9f661417c86e"
    )
    assert stable_predicate_command_fingerprint(reassign) == (
        "02d522f3b7b7049a5b18e3a58b9546bc86ab83a6f9c29751d52155f62575078e"
    )
    assert stable_predicate_command_json_bytes(
        StablePredicateMergeCommand(library_id, (predicate_b,), predicate_a)
    ) == stable_predicate_command_json_bytes(merge)


def test_stable_predicate_decision_identity_golden_vectors() -> None:
    library_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    predicate_a = uuid.UUID("10000000-0000-4000-8000-000000000001")
    predicate_b = uuid.UUID("10000000-0000-4000-8000-000000000002")
    mapping_1 = uuid.UUID("20000000-0000-4000-8000-000000000001")
    mapping_2 = uuid.UUID("20000000-0000-4000-8000-000000000002")
    expected = "a" * 64
    policy = _target_spec().resolution_policy
    target = _target_spec()
    target_id = stable_predicate_target_id(library_id, target)
    common = {
        "expected_precondition_fingerprint": expected,
        "method": "manual",
        "evidence_refs": (),
    }
    merge = replace(
        StablePredicateMergeCommand(library_id, (predicate_b,), predicate_a),
        requested_effect="apply",
        reason_code="manual_merge",
        reason_text="merge B into A",
        **common,
    )
    merge_payload = {
        "mapping_assignments": [{
            "from_predicate_id": predicate_b,
            "mapping_id": mapping_1,
            "partition_basis_snapshot": {},
            "reason_code": "merge_survivor",
            "state": "resolved",
            "target_ref": {"kind": "existing", "predicate_id": predicate_a},
        }],
        "policy_compatibility": [
            {
                "contract_version": "v1", "identity_policy_version": "p2_identity_v1",
                "key": key, "namespace": "urn:test:predicate", "predicate_id": predicate_id,
                "resolution_policy": dict(policy), "temporal_class": "static_fact",
            }
            for predicate_id, key in ((predicate_a, "predicate-a"), (predicate_b, "predicate-b"))
        ],
        "survivor_predicate_id": predicate_a,
    }
    assert stable_predicate_decision_fingerprint(merge, merge_payload) == (
        "68e3d53f169fc0ed9b83c14d20b0bcd678562ed7fafdbda3589964319b02f126"
    )

    split = replace(
        StablePredicateSplitCommand(
            library_id,
            predicate_a,
            (
                StablePredicateSuccessorSlot.existing(predicate_b),
                StablePredicateSuccessorSlot.new(target_id, target),
            ),
        ),
        requested_effect="stage",
        reason_code="manual_split",
        reason_text="partition A into B and C",
        **common,
    )
    split_payload = {
        "mapping_assignments": [
            {
                "from_predicate_id": predicate_a, "mapping_id": mapping_1,
                "partition_basis_snapshot": {}, "reason_code": "manual_partition",
                "state": "resolved",
                "target_ref": {"kind": "existing", "predicate_id": predicate_b},
            },
            {
                "from_predicate_id": predicate_a, "mapping_id": mapping_2,
                "partition_basis_snapshot": {}, "reason_code": "needs_review",
                "state": "pending", "target_ref": None,
            },
        ],
        "successor_slots": [
            {"kind": "existing", "predicate_id": predicate_b},
            {
                "kind": "new", "predicate_id": target_id,
                "target_spec_fingerprint": stable_predicate_target_fingerprint(target),
                "target_spec_snapshot": {
                    "schema": "p3_2_stable_predicate_target_v1",
                    "namespace": target.namespace, "key": target.key,
                    "contract_version": target.contract_version,
                    "temporal_class": target.temporal_class,
                    "identity_policy_version": target.identity_policy_version,
                    "resolution_status": "resolved",
                    "resolution_policy": dict(target.resolution_policy),
                },
            },
        ],
    }
    assert stable_predicate_decision_fingerprint(split, split_payload) == (
        "b76f0d0a3d2ef69c339931f47a486af49dc4dfc6233a0f8e84d5b36158c829f7"
    )

    reassign = replace(
        StablePredicateReassignCommand(library_id, mapping_1, predicate_a, predicate_b),
        requested_effect="apply",
        reason_code="manual_reassign",
        reason_text="move M1 from A to B",
        **common,
    )
    reassign_payload = {"mapping_assignment": {
        "from_predicate_id": predicate_a, "mapping_id": mapping_1,
        "partition_basis_snapshot": {}, "reason_code": "manual_reassign",
        "state": "resolved",
        "target_ref": {"kind": "existing", "predicate_id": predicate_b},
    }}
    assert stable_predicate_decision_fingerprint(reassign, reassign_payload) == (
        "9fd80e6d95d8b30e927e6c3096fd262161834b6067e37b8bfe640668de7d1c9f"
    )

    cancellation = StablePredicateEvolutionCancellation(
        command_id=uuid.uuid4(),
        original_idempotency_key="split-cancel",
        expected_pending_decision_id=uuid.UUID("30000000-0000-4000-8000-000000000001"),
        reason_code="incorrect_pending_intent",
        reason_text="cancel incorrect pending split",
        evidence_refs=(),
        request_id="p32:cancel",
    )
    assert stable_predicate_cancellation_fingerprint(cancellation, expected) == (
        "444889da10a1ab3b58b00dcc86d77449a1cf9ad60bd02f1fdf43fe6b60f6fc92"
    )


class _RowsResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _EvolutionDb:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model

    async def get(self, model, identifier):
        return next(
            (row for row in self.rows_by_model.get(model, ()) if row.id == identifier),
            None,
        )

    async def execute(self, statement, _params=None):
        model = statement.column_descriptions[0]["entity"]
        return _RowsResult(self.rows_by_model.get(model, ()))


class _MutationDb:
    def __init__(self, *rows):
        self.rows_by_model: dict[type, dict[uuid.UUID, object]] = {}
        self.added: list[object] = []
        self.flush_count = 0
        self.executed_models: list[type] = []
        self.statements: list[str] = []
        for row in rows:
            self._store(row)
        for library_id in {
            row.library_id for row in rows if hasattr(row, "library_id")
        }:
            self.rows_by_model.setdefault(Library, {})[library_id] = SimpleNamespace(
                id=library_id,
                organization_id=uuid.uuid4(),
                slug=f"library-{library_id}",
                deleted_at=None,
            )

    def _store(self, row):
        self.rows_by_model.setdefault(type(row), {})[row.id] = row

    async def get(self, model, identifier):
        return self.rows_by_model.get(model, {}).get(identifier)

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        if params and "lock_key" in params:
            return _ScalarResult(True)
        model = statement.column_descriptions[0]["entity"]
        self.executed_models.append(model)
        return _RowsResult(self.rows_by_model.get(model, {}).values())

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1
        for row in self.added:
            self._store(row)
        self.added.clear()

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))


def _predicate(library_id, predicate_id, key):
    target = _target_spec()
    return StablePredicateIdentity(
        id=predicate_id,
        library_id=library_id,
        namespace="urn:test:predicate",
        key=key,
        contract_version="v1",
        temporal_class="static_fact",
        identity_policy_version="p2_identity_v1",
        resolution_status="resolved",
        resolution_policy=dict(target.resolution_policy),
        created_at=datetime.now(timezone.utc),
    )


def _mapping(library_id, predicate_id, relation_type_id):
    return StablePredicateMapping(
        id=uuid.uuid4(),
        library_id=library_id,
        stable_predicate_identity_id=predicate_id,
        relation_type_id=relation_type_id,
        mapping_status="active",
        created_at=datetime.now(timezone.utc),
    )


def _with_envelope(command, key, *, effect="apply", predecessor=None):
    return replace(
        command,
        idempotency_key=key,
        requested_effect=effect,
        reason_code="operator_review",
        reason_text="approved predicate evolution",
        method="manual",
        evidence_refs=({"source_ref": "p3.2-test"},),
        request_id=f"p32:{key}",
        expected_predecessor_decision_id=predecessor,
    )


def test_current_predicate_resolver_split_is_forked_pending_or_context_resolved() -> None:
    library_id = uuid.uuid4()
    source_id, target_b_id, target_c_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_mapping_id, new_mapping_id = uuid.uuid4(), uuid.uuid4()
    decision_id, transition_id = uuid.uuid4(), uuid.uuid4()
    source = SimpleNamespace(id=source_id, library_id=library_id)
    target_b = SimpleNamespace(id=target_b_id, library_id=library_id)
    target_c = SimpleNamespace(id=target_c_id, library_id=library_id)
    old_mapping = SimpleNamespace(
        id=old_mapping_id,
        library_id=library_id,
        stable_predicate_identity_id=source_id,
        relation_type_id=uuid.uuid4(),
        mapping_status="superseded",
    )
    new_mapping = SimpleNamespace(
        id=new_mapping_id,
        library_id=library_id,
        stable_predicate_identity_id=target_b_id,
        relation_type_id=old_mapping.relation_type_id,
        mapping_status="active",
    )
    decision = SimpleNamespace(
        id=decision_id,
        library_id=library_id,
        operation_kind="split",
        lifecycle_status="applied",
    )
    transition = SimpleNamespace(
        id=transition_id,
        library_id=library_id,
        evolution_decision_id=decision_id,
        source_predicate_id=source_id,
        evolution_status="applied",
    )
    successors = [
        SimpleNamespace(
            id=uuid.uuid4(),
            library_id=library_id,
            evolution_decision_id=decision_id,
            source_transition_id=transition_id,
            source_predicate_id=source_id,
            target_predicate_id=target_id,
        )
        for target_id in (target_b_id, target_c_id)
    ]
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        evolution_decision_id=decision_id,
        source_transition_id=transition_id,
        old_mapping_id=old_mapping_id,
        source_predicate_id=source_id,
        target_successor_id=successors[0].id,
        target_predicate_id=target_b_id,
        new_mapping_id=new_mapping_id,
        assignment_state="resolved",
    )
    new_mapping.evolution_assignment_id = assignment.id
    rows = {
        StablePredicateIdentity: [source, target_b, target_c],
        StablePredicateMapping: [old_mapping, new_mapping],
        StablePredicateEvolutionDecision: [decision],
        StablePredicateEvolutionSource: [transition],
        StablePredicateEvolutionSuccessor: successors,
        StablePredicateMappingEvolutionAssignment: [assignment],
    }
    db = _EvolutionDb(rows)

    unscoped = _run(
        resolve_current_stable_predicate_identity(db, library_id, source_id)
    )
    assert unscoped.status == "forked"

    scoped = _run(
        resolve_current_stable_predicate_identity(
            db,
            library_id,
            source_id,
            StablePredicateEvolutionContext(
                mapping_id=old_mapping_id,
                relation_type_id=old_mapping.relation_type_id,
            ),
        )
    )
    assert scoped.status == "resolved"
    assert scoped.current_predicate_id == target_b_id
    assert scoped.current_mapping_id == new_mapping_id

    transition.evolution_status = "pending"
    decision.lifecycle_status = "pending"
    assert _run(
        resolve_current_stable_predicate_identity(
            db,
            library_id,
            source_id,
            StablePredicateEvolutionContext(mapping_id=old_mapping_id),
        )
    ).status == "pending"


def test_current_predicate_resolver_follows_multigeneration_mapping_lineage() -> None:
    library_id = uuid.uuid4()
    predicate_ids = [uuid.uuid4() for _ in range(3)]
    mapping_ids = [uuid.uuid4() for _ in range(3)]
    relation_type_id = uuid.uuid4()
    decisions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            library_id=library_id,
            operation_kind="merge",
            lifecycle_status="applied",
        )
        for _ in range(2)
    ]
    sources = [
        SimpleNamespace(
            id=uuid.uuid4(),
            library_id=library_id,
            evolution_decision_id=decisions[index].id,
            source_predicate_id=predicate_ids[index],
            evolution_status="applied",
        )
        for index in range(2)
    ]
    successors = [
        SimpleNamespace(
            id=uuid.uuid4(),
            library_id=library_id,
            evolution_decision_id=decisions[index].id,
            source_transition_id=sources[index].id,
            source_predicate_id=predicate_ids[index],
            target_predicate_id=predicate_ids[index + 1],
        )
        for index in range(2)
    ]
    mappings = [
        SimpleNamespace(
            id=mapping_ids[index],
            library_id=library_id,
            stable_predicate_identity_id=predicate_ids[index],
            relation_type_id=relation_type_id,
            mapping_status="active" if index == 2 else "superseded",
            evolution_assignment_id=None,
        )
        for index in range(3)
    ]
    assignments = [
        SimpleNamespace(
            id=uuid.uuid4(),
            library_id=library_id,
            evolution_decision_id=decisions[index].id,
            source_transition_id=sources[index].id,
            old_mapping_id=mapping_ids[index],
            source_predicate_id=predicate_ids[index],
            target_successor_id=successors[index].id,
            target_predicate_id=predicate_ids[index + 1],
            new_mapping_id=mapping_ids[index + 1],
            assignment_state="resolved",
        )
        for index in range(2)
    ]
    mappings[1].evolution_assignment_id = assignments[0].id
    mappings[2].evolution_assignment_id = assignments[1].id
    db = _EvolutionDb(
        {
            StablePredicateIdentity: [
                SimpleNamespace(id=value, library_id=library_id)
                for value in predicate_ids
            ],
            StablePredicateMapping: mappings,
            StablePredicateEvolutionDecision: decisions,
            StablePredicateEvolutionSource: sources,
            StablePredicateEvolutionSuccessor: successors,
            StablePredicateMappingEvolutionAssignment: assignments,
        }
    )

    resolved = _run(
        resolve_current_stable_predicate_identity(
            db,
            library_id,
            predicate_ids[0],
            StablePredicateEvolutionContext(
                mapping_id=mapping_ids[0],
                relation_type_id=relation_type_id,
            ),
        )
    )

    assert resolved.status == "resolved", resolved
    assert resolved.current_predicate_id == predicate_ids[2]
    assert resolved.current_mapping_id == mapping_ids[2]
    assert resolved.lineage_decision_ids == tuple(row.id for row in decisions)

    mappings[2].evolution_assignment_id = uuid.uuid4()
    corrupt = _run(
        resolve_current_stable_predicate_identity(
            db,
            library_id,
            predicate_ids[0],
            StablePredicateEvolutionContext(
                mapping_id=mapping_ids[0],
                relation_type_id=relation_type_id,
            ),
        )
    )
    assert corrupt.status == "pending"
    assert corrupt.reason_code == "predicate_evolution_integrity"


def test_fact_resolution_active_predicate_gate_blocks_incomplete_split() -> None:
    library_id = uuid.uuid4()
    relation_type_id = uuid.uuid4()
    predicate_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    mapping = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        stable_predicate_identity_id=predicate_id,
        relation_type_id=relation_type_id,
        mapping_status="active",
    )
    predicate = SimpleNamespace(id=predicate_id, library_id=library_id)
    decision = SimpleNamespace(
        id=decision_id,
        library_id=library_id,
        operation_kind="split",
        lifecycle_status="pending",
    )
    transition = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        evolution_decision_id=decision_id,
        source_predicate_id=predicate_id,
        evolution_status="pending",
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        evolution_decision_id=decision_id,
        source_transition_id=transition.id,
        old_mapping_id=mapping.id,
        assignment_state="pending",
    )
    db = _EvolutionDb(
        {
            StablePredicateIdentity: [predicate],
            StablePredicateMapping: [mapping],
            StablePredicateEvolutionDecision: [decision],
            StablePredicateEvolutionSource: [transition],
            StablePredicateEvolutionSuccessor: [],
            StablePredicateMappingEvolutionAssignment: [assignment],
        }
    )

    predicates, reason_code = _run(
        fact_resolution._active_predicates(
            db,
            library_id=library_id,
            relation_type_id=relation_type_id,
        )
    )
    assert predicates == []
    assert reason_code == "predicate_split_partition_incomplete"


def test_applied_merge_records_lineage_reassigns_mapping_and_replays() -> None:
    library_id = uuid.uuid4()
    survivor_id, source_id, relation_type_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    survivor = _predicate(library_id, survivor_id, "survivor")
    source = _predicate(library_id, source_id, "source")
    mapping = _mapping(library_id, source_id, relation_type_id)
    historical_fact = SimpleNamespace(stable_predicate_identity_id=source_id)
    db = _MutationDb(survivor, source, mapping)
    command = _with_envelope(
        StablePredicateMergeCommand(library_id, (source_id,), survivor_id),
        "merge-source",
    )
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(db, command),
    )

    result = _apply(db, command)
    assert result.status == "APPLIED"
    assert result.decision.lifecycle_status == "applied"
    assert mapping.mapping_status == "superseded"
    assert len(result.source_transitions) == 1
    assert result.source_transitions[0].source_predicate_id == source_id
    assert result.successors[0].target_predicate_id == survivor_id
    assert result.assignments[0].new_mapping_id is not None
    replacement = _run(db.get(StablePredicateMapping, result.assignments[0].new_mapping_id))
    assert replacement.mapping_status == "active"
    assert replacement.stable_predicate_identity_id == survivor_id
    assert historical_fact.stable_predicate_identity_id == source_id

    replay = _apply(db, command)
    assert replay.status == "REUSED"
    assert replay.reused_decision_id == result.decision.id
    assert replay.effective_outcome == "APPLIED"
    assert replay.current_decision_status == "applied"


def test_pending_split_can_be_corrected_atomically_then_resolves_by_mapping() -> None:
    library_id = uuid.uuid4()
    source_id, left_id, right_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    relation_one, relation_two = uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    left = _predicate(library_id, left_id, "left")
    right = _predicate(library_id, right_id, "right")
    first_mapping = _mapping(library_id, source_id, relation_one)
    second_mapping = _mapping(library_id, source_id, relation_two)
    db = _MutationDb(source, left, right, first_mapping, second_mapping)
    pending = _with_envelope(
        StablePredicateSplitCommand(
            library_id,
            source_id,
            (
                StablePredicateSuccessorSlot.existing(left_id),
                StablePredicateSuccessorSlot.existing(right_id),
            ),
            mapping_assignments=(
                StablePredicateMappingPartition(first_mapping.id, "resolved", left_id),
                StablePredicateMappingPartition(second_mapping.id, "pending", None),
            ),
        ),
        "split-source",
    )
    pending = replace(
        pending,
        expected_precondition_fingerprint=_prepare(db, pending),
    )
    first = _apply(db, pending)
    assert first.status == "PENDING"
    assert first_mapping.mapping_status == second_mapping.mapping_status == "active"

    corrected = replace(
        pending,
        mapping_assignments=(
            StablePredicateMappingPartition(first_mapping.id, "resolved", left_id),
            StablePredicateMappingPartition(second_mapping.id, "resolved", right_id),
        ),
        requested_effect="apply",
        expected_predecessor_decision_id=first.decision.id,
    )
    corrected = replace(
        corrected,
        expected_precondition_fingerprint=_prepare(db, corrected),
    )
    applied = _apply(db, corrected)
    assert applied.status == "APPLIED"
    assert first.decision.lifecycle_status == "superseded"
    assert all(row.assignment_state == "superseded" for row in first.assignments)
    assert first_mapping.mapping_status == second_mapping.mapping_status == "superseded"
    resolved = _run(
        resolve_current_stable_predicate_identity(
            db,
            library_id,
            source_id,
            StablePredicateEvolutionContext(
                mapping_id=first_mapping.id,
                relation_type_id=relation_one,
            ),
        )
    )
    assert resolved.status == "resolved"
    assert resolved.current_predicate_id == left_id


def test_pending_split_new_target_is_only_a_planned_identity() -> None:
    library_id = uuid.uuid4()
    source_id, existing_id = uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    existing = _predicate(library_id, existing_id, "existing")
    mapping = _mapping(library_id, source_id, uuid.uuid4())
    target = _target_spec()
    planned_id = stable_predicate_target_id(library_id, target)
    db = _MutationDb(source, existing, mapping)
    command = _with_envelope(
        StablePredicateSplitCommand(
            library_id,
            source_id,
            (
                StablePredicateSuccessorSlot.existing(existing_id),
                StablePredicateSuccessorSlot.new(planned_id, target),
            ),
            mapping_assignments=(
                StablePredicateMappingPartition(
                    mapping.id,
                    "resolved",
                    planned_id,
                ),
            ),
        ),
        "split-planned-target",
        effect="stage",
    )
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(db, command),
    )

    result = _apply(db, command)

    assert result.status == "PENDING"
    planned_successor = next(
        row for row in result.successors if row.planned_target_predicate_id == planned_id
    )
    assert planned_successor.target_predicate_id is None
    assert result.assignments[0].target_successor_id == planned_successor.id
    assert result.assignments[0].target_predicate_id is None
    assert _run(db.get(StablePredicateIdentity, planned_id)) is None
    assert mapping.mapping_status == "active"


def test_pending_reassign_cancellation_releases_mapping_slot() -> None:
    library_id = uuid.uuid4()
    source_id, target_id, next_target_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    target = _predicate(library_id, target_id, "target")
    next_target = _predicate(library_id, next_target_id, "next-target")
    mapping = _mapping(library_id, source_id, uuid.uuid4())
    db = _MutationDb(source, target, next_target, mapping)
    command = _with_envelope(
        StablePredicateReassignCommand(library_id, mapping.id, source_id, target_id),
        "reassign-source",
        effect="stage",
    )
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(db, command),
    )
    pending = _apply(db, command)
    assert pending.status == "PENDING"
    assert mapping.mapping_status == "active"

    cancellation = StablePredicateEvolutionCancellation(
        command_id=pending.command.id,
        original_idempotency_key=command.idempotency_key,
        expected_pending_decision_id=pending.decision.id,
        reason_code="incorrect_pending_intent",
        reason_text="cancel incorrect reassign",
        evidence_refs=(),
        request_id="p32:cancel-reassign",
    )
    cancelled = _cancel(db, library_id, cancellation)
    assert cancelled.status == "CANCELLED"
    assert (
        cancelled.decision.expected_precondition_fingerprint
        == pending.decision.observed_precondition_fingerprint
    )
    assert (
        cancelled.decision.observed_precondition_fingerprint
        != cancelled.decision.expected_precondition_fingerprint
    )
    assert any("FOR UPDATE" in statement for statement in db.statements)
    assert pending.decision.lifecycle_status == "superseded"
    assert pending.assignments[0].assignment_state == "superseded"
    assert mapping.mapping_status == "active"

    next_command = replace(
        command,
        idempotency_key="reassign-after-cancel",
        to_predicate_id=next_target_id,
    )
    next_command = replace(
        next_command,
        expected_precondition_fingerprint=_prepare(db, next_command),
    )
    assert _apply(db, next_command).status == "PENDING"


def test_decision_payload_over_limit_is_rejected_before_root_insert() -> None:
    library_id = uuid.uuid4()
    source_id, left_id, right_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    left = _predicate(library_id, left_id, "left")
    right = _predicate(library_id, right_id, "right")
    mappings = tuple(
        _mapping(library_id, source_id, uuid.uuid4()) for _ in range(18)
    )
    command = replace(
        _with_envelope(
            StablePredicateSplitCommand(
                library_id,
                source_id,
                (
                    StablePredicateSuccessorSlot.existing(left_id),
                    StablePredicateSuccessorSlot.existing(right_id),
                ),
                mapping_assignments=tuple(
                    StablePredicateMappingPartition(
                        mapping.id,
                        "resolved",
                        left_id,
                        {"basis": "x" * 62000},
                    )
                    for mapping in mappings
                ),
            ),
            "oversized-decision",
            effect="stage",
        ),
        expected_precondition_fingerprint="a" * 64,
    )
    db = _MutationDb(source, left, right, *mappings)

    result = _apply(db, command)

    assert result.status == "REJECTED"
    assert result.reason_code == "payload_too_large"
    assert db.rows_by_model.get(StablePredicateEvolutionCommand, {}) == {}


def test_lock_busy_requires_outer_transaction_rollback(monkeypatch) -> None:
    library_id = uuid.uuid4()
    source_id, target_id = uuid.uuid4(), uuid.uuid4()
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, target_id, "target"),
    )
    command = replace(
        _with_envelope(
            StablePredicateMergeCommand(library_id, (source_id,), target_id),
            "lock-busy",
        ),
        expected_precondition_fingerprint="a" * 64,
    )

    async def busy(_db, _command):
        raise GraphIdentityLockBusy("busy")

    monkeypatch.setattr(evolution, "_lock_command_scopes", busy)

    with pytest.raises(StablePredicateEvolutionRetryableConflict) as caught:
        _apply(db, command)
    assert caught.value.reason_code == "predicate_evolution_lock_busy"


def test_integrity_conflict_requires_outer_transaction_rollback(monkeypatch) -> None:
    library_id = uuid.uuid4()
    source_id, target_id = uuid.uuid4(), uuid.uuid4()
    mapping = _mapping(library_id, source_id, uuid.uuid4())
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, target_id, "target"),
        mapping,
    )
    command = _with_envelope(
        StablePredicateMergeCommand(library_id, (source_id,), target_id),
        "integrity-conflict",
    )
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(db, command),
    )

    async def conflict(*_args, **_kwargs):
        raise IntegrityError("statement", {}, Exception("unique conflict"))

    monkeypatch.setattr(evolution, "_create_children_and_effects", conflict)

    with pytest.raises(StablePredicateEvolutionRetryableConflict) as caught:
        _apply(db, command)
    assert caught.value.reason_code == "integrity_conflict"


def test_idempotency_conflict_alias_key_and_head_cas_fail_closed() -> None:
    library_id = uuid.uuid4()
    source_id, target_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    target = _predicate(library_id, target_id, "target")
    other = _predicate(library_id, other_id, "other")
    db = _MutationDb(source, target, other)
    command = _with_envelope(
        StablePredicateMergeCommand(library_id, (source_id,), target_id),
        "identity-root",
        effect="stage",
    )
    command = replace(
        command,
        expected_precondition_fingerprint=_prepare(db, command),
    )
    pending = _apply(db, command)
    assert pending.status == "PENDING"

    conflict = replace(command, survivor_predicate_id=other_id)
    assert _apply(db, conflict).reason_code == "idempotency_key_conflict"

    alias = replace(command, idempotency_key="identity-alias")
    assert _apply(db, alias).reason_code == "command_identity_alias_key"

    correction = replace(
        command,
        reason_text="corrected intent",
        expected_predecessor_decision_id=uuid.uuid4(),
    )
    correction = replace(
        correction,
        expected_precondition_fingerprint=_prepare(db, correction),
    )
    stale = _apply(db, correction)
    assert stale.status == "STALE_OPERATION"
    assert stale.reason_code == "expected_predecessor_mismatch"
    assert pending.decision.lifecycle_status == "pending"


def test_new_target_uuid_and_scoped_identity_collisions_fail_closed() -> None:
    library_id = uuid.uuid4()
    source_id, existing_id = uuid.uuid4(), uuid.uuid4()
    target = _target_spec()
    planned_id = stable_predicate_target_id(library_id, target)
    occupant = _predicate(library_id, planned_id, "different-key")
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, existing_id, "existing"),
        occupant,
    )
    command = replace(
        _with_envelope(
            StablePredicateSplitCommand(
                library_id,
                source_id,
                (
                    StablePredicateSuccessorSlot.existing(existing_id),
                    StablePredicateSuccessorSlot.new(planned_id, target),
                ),
            ),
            "uuid-collision",
            effect="stage",
        ),
        expected_precondition_fingerprint="a" * 64,
    )

    result = _apply(db, command)

    assert result.status == "STALE_OPERATION"
    assert result.reason_code == "target_predicate_uuid_collision"
    assert db.rows_by_model.get(StablePredicateEvolutionCommand, {}) == {}


def test_command_and_decision_identity_normalize_set_like_arrays() -> None:
    library_id = uuid.uuid4()
    source_a, source_b, survivor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = StablePredicateMergeCommand(
        library_id,
        (source_a, source_b),
        survivor,
    )
    second = StablePredicateMergeCommand(
        library_id,
        (source_b, source_a),
        survivor,
    )
    assert stable_predicate_command_json_bytes(first) == stable_predicate_command_json_bytes(
        second
    )

    first = replace(
        first,
        requested_effect="stage",
        reason_code="normalize",
        reason_text="normalize payload order",
        method="manual",
        expected_precondition_fingerprint="a" * 64,
        evidence_refs=({"source_ref": "b"}, {"source_ref": "a"}),
    )
    second = replace(first, evidence_refs=tuple(reversed(first.evidence_refs)))
    payload = {
        "mapping_assignments": [],
        "policy_compatibility": [],
        "survivor_predicate_id": survivor,
    }
    assert stable_predicate_decision_fingerprint(
        first,
        payload,
    ) == stable_predicate_decision_fingerprint(second, payload)


def test_mutation_lock_scope_includes_existing_and_planned_predicates(
    monkeypatch,
) -> None:
    library_id = uuid.uuid4()
    source_id, existing_id = uuid.uuid4(), uuid.uuid4()
    target = _target_spec()
    planned_id = stable_predicate_target_id(library_id, target)
    command = StablePredicateSplitCommand(
        library_id,
        source_id,
        (
            StablePredicateSuccessorSlot.existing(existing_id),
            StablePredicateSuccessorSlot.new(planned_id, target),
        ),
    )
    calls = []

    async def capture(_db, actual_library_id, scopes, *, wait=True):
        calls.append((actual_library_id, tuple(scopes), wait))

    monkeypatch.setattr(evolution, "lock_graph_identity_scopes", capture)
    _run(evolution._lock_command_scopes(_MutationDb(), command))

    assert calls[0][0] == library_id
    assert calls[0][2] is False
    assert {scope.scope_key for scope in calls[0][1]} == {
        source_id,
        existing_id,
        planned_id,
    }
    assert {scope.scope_type for scope in calls[0][1]} == {
        STABLE_PREDICATE_LOCK_SCOPE
    }


def test_oversized_new_target_is_rejected_as_payload_too_large() -> None:
    library_id = uuid.uuid4()
    policy = dict(_target_spec().resolution_policy)
    policy["qualifier_policy"] = {
        "identity_bearing": [f"property_{index:05d}" for index in range(5000)],
        "assertion_bearing": [],
        "evidence_only": [],
    }
    target = replace(
        _target_spec(),
        resolution_policy=policy,
    )
    planned_id = stable_predicate_target_id(library_id, target)
    command = replace(
        _with_envelope(
            StablePredicateSplitCommand(
                library_id,
                uuid.uuid4(),
                (
                    StablePredicateSuccessorSlot.existing(uuid.uuid4()),
                    StablePredicateSuccessorSlot.new(planned_id, target),
                ),
            ),
            "oversized-target",
            effect="stage",
        ),
        expected_precondition_fingerprint="a" * 64,
    )
    db = _MutationDb()

    result = _apply(db, command)

    assert result.status == "REJECTED"
    assert result.reason_code == "payload_too_large"
    assert db.executed_models == []


def test_stale_and_rejected_heads_accept_only_same_root_retry() -> None:
    library_id = uuid.uuid4()
    source_id, survivor_id = uuid.uuid4(), uuid.uuid4()
    source = _predicate(library_id, source_id, "source")
    survivor = _predicate(library_id, survivor_id, "survivor")
    db = _MutationDb(source, survivor)
    command = replace(
        _with_envelope(
            StablePredicateMergeCommand(library_id, (source_id,), survivor_id),
            "stale-retry",
        ),
        expected_precondition_fingerprint="0" * 64,
    )

    stale = _apply(db, command)
    assert stale.status == "STALE_OPERATION"
    assert stale.decision.lifecycle_status == "stale"

    retry = replace(
        command,
        reason_text="retry after fresh observation",
        expected_predecessor_decision_id=stale.decision.id,
    )
    retry = replace(retry, expected_precondition_fingerprint=_prepare(db, retry))
    applied = _apply(db, retry)
    assert applied.status == "APPLIED"
    assert stale.decision.lifecycle_status == "superseded"
    assert applied.decision.supersedes_decision_id == stale.decision.id

    rejected_source_id, rejected_target_id = uuid.uuid4(), uuid.uuid4()
    rejected_source = _predicate(library_id, rejected_source_id, "rejected-source")
    rejected_target = _predicate(library_id, rejected_target_id, "rejected-target")
    rejected_target.resolution_policy = {
        **rejected_target.resolution_policy,
        "modality_policy": {"source": "fixed", "value": "possible"},
    }
    rejected_db = _MutationDb(rejected_source, rejected_target)
    rejected_command = _with_envelope(
        StablePredicateMergeCommand(
            library_id,
            (rejected_source_id,),
            rejected_target_id,
        ),
        "rejected-retry",
    )
    rejected_command = replace(
        rejected_command,
        expected_precondition_fingerprint=_prepare(rejected_db, rejected_command),
    )
    rejected = _apply(rejected_db, rejected_command)
    assert rejected.status == "REJECTED"
    assert rejected.decision.lifecycle_status == "rejected"

    rejected_retry = replace(
        rejected_command,
        reason_text="audited retry remains incompatible",
        expected_predecessor_decision_id=rejected.decision.id,
    )
    rejected_retry = replace(
        rejected_retry,
        expected_precondition_fingerprint=_prepare(rejected_db, rejected_retry),
    )
    rejected_again = _apply(rejected_db, rejected_retry)
    assert rejected_again.status == "REJECTED"
    assert rejected.decision.lifecycle_status == "superseded"
    assert rejected_again.decision.supersedes_decision_id == rejected.decision.id


def test_cross_command_cancellation_cannot_release_another_pending_head() -> None:
    library_id = uuid.uuid4()
    first_source, second_source, target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _MutationDb(
        _predicate(library_id, first_source, "first-source"),
        _predicate(library_id, second_source, "second-source"),
        _predicate(library_id, target, "target"),
    )

    def stage(source_id: uuid.UUID, key: str):
        command = _with_envelope(
            StablePredicateMergeCommand(library_id, (source_id,), target),
            key,
            effect="stage",
        )
        command = replace(
            command,
            expected_precondition_fingerprint=_prepare(db, command),
        )
        return command, _apply(db, command)

    first_command, first = stage(first_source, "first-command")
    _second_command, second = stage(second_source, "second-command")
    cancellation = StablePredicateEvolutionCancellation(
        command_id=first.command.id,
        original_idempotency_key=first_command.idempotency_key,
        expected_pending_decision_id=second.decision.id,
        reason_code="incorrect_pending_intent",
        reason_text="must not release another command",
        evidence_refs=(),
        request_id="p32:cross-command-cancel",
    )

    result = _cancel(db, library_id, cancellation)

    assert result.status == "STALE_OPERATION"
    assert result.reason_code == "expected_predecessor_mismatch"
    assert first.decision.lifecycle_status == "pending"
    assert second.decision.lifecycle_status == "pending"


def test_existing_scoped_identity_must_be_submitted_as_existing_slot() -> None:
    library_id = uuid.uuid4()
    source_id, existing_id = uuid.uuid4(), uuid.uuid4()
    target = _target_spec()
    planned_id = stable_predicate_target_id(library_id, target)
    scoped_occupant = _predicate(library_id, uuid.uuid4(), target.key)
    db = _MutationDb(
        _predicate(library_id, source_id, "source"),
        _predicate(library_id, existing_id, "existing"),
        scoped_occupant,
    )
    command = replace(
        _with_envelope(
            StablePredicateSplitCommand(
                library_id,
                source_id,
                (
                    StablePredicateSuccessorSlot.existing(existing_id),
                    StablePredicateSuccessorSlot.new(planned_id, target),
                ),
            ),
            "scoped-identity-collision",
            effect="stage",
        ),
        expected_precondition_fingerprint="a" * 64,
    )

    result = _apply(db, command)

    assert result.status == "STALE_OPERATION"
    assert result.reason_code == "new_target_identity_already_exists"
