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
