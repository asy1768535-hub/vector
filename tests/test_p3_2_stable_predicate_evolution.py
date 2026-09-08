from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.graph_identity_locks import (
    STABLE_PREDICATE_LOCK_SCOPE,
    GraphIdentityLockBusy,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)
from app.services.organization_authorization import credential_organization_scope
from app.services.stable_predicate_evolution import (
    StablePredicateMergeCommand,
    StablePredicateReassignCommand,
    StablePredicateSplitCommand,
    StablePredicateSuccessorSlot,
    StablePredicateTargetSpec,
    stable_predicate_command_fingerprint,
    stable_predicate_command_json_bytes,
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
