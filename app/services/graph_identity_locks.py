"""Shared transaction advisory locks for graph identity writers."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text

CANONICAL_ENTITY_LOCK_SCOPE = 10
STABLE_PREDICATE_LOCK_SCOPE = 20
LOGICAL_FACT_LOCK_SCOPE = 30
ENTITY_PROJECTION_LOCK_SCOPE = 40
ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE = 50

_VALID_SCOPE_TYPES = {
    CANONICAL_ENTITY_LOCK_SCOPE,
    STABLE_PREDICATE_LOCK_SCOPE,
    LOGICAL_FACT_LOCK_SCOPE,
    ENTITY_PROJECTION_LOCK_SCOPE,
    ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE,
}
_LOCK_PREFIX = "vector-kb:graph-identity-lock:v1:"


class GraphIdentityLockError(ValueError):
    """A caller supplied a lock scope outside the frozen P3 contract."""


class GraphIdentityLockBusy(RuntimeError):
    """A fail-fast graph identity lock could not be acquired."""


@dataclass(frozen=True, slots=True)
class GraphIdentityLockScope:
    scope_type: int
    scope_key: uuid.UUID | str

    def __post_init__(self) -> None:
        if self.scope_type not in _VALID_SCOPE_TYPES:
            raise GraphIdentityLockError("unsupported graph identity lock scope")
        if not isinstance(self.scope_key, (uuid.UUID, str)) or not str(self.scope_key):
            raise GraphIdentityLockError("graph identity lock scope key is invalid")


def _database_dialect_name(db) -> str | None:
    try:
        bind = db.sync_session.get_bind()
    except (AttributeError, RuntimeError):
        try:
            bind = db.get_bind()
        except (AttributeError, RuntimeError):
            return None
    return getattr(getattr(bind, "dialect", None), "name", None)


def _lock_key(library_id: uuid.UUID, scope: GraphIdentityLockScope) -> int:
    digest = hashlib.sha256(
        f"{_LOCK_PREFIX}{library_id}:{scope.scope_type}:{scope.scope_key}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def graph_identity_lock_key(library_id: uuid.UUID, scope: GraphIdentityLockScope) -> int:
    """Expose the shared key derivation for legacy lock-key compatibility tests."""

    return _lock_key(library_id, scope)


def normalized_graph_identity_scopes(
    library_id: uuid.UUID,
    scopes: Iterable[GraphIdentityLockScope],
) -> tuple[GraphIdentityLockScope, ...]:
    """Deduplicate scopes and apply the globally frozen total order."""

    if not isinstance(library_id, uuid.UUID):
        raise GraphIdentityLockError("library_id must be a UUID")
    unique = {(scope.scope_type, str(scope.scope_key)): scope for scope in scopes}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (str(library_id), item[0], item[1]))
    )


async def lock_graph_identity_scopes(
    db,
    library_id: uuid.UUID,
    scopes: Iterable[GraphIdentityLockScope],
    *,
    wait: bool = True,
) -> None:
    """Lock all graph identity scopes in order without owning the transaction."""

    if not isinstance(wait, bool):
        raise GraphIdentityLockError("wait must be a boolean")
    ordered = normalized_graph_identity_scopes(library_id, scopes)
    dialect = _database_dialect_name(db)
    if dialect in {None, "sqlite"}:
        return
    if dialect != "postgresql":
        raise GraphIdentityLockError("graph identity concurrency control requires PostgreSQL")
    for scope in ordered:
        result = await db.execute(
            text(
                "SELECT pg_advisory_xact_lock(:lock_key)"
                if wait
                else "SELECT pg_try_advisory_xact_lock(:lock_key)"
            ),
            {"lock_key": _lock_key(library_id, scope)},
        )
        if not wait and result.scalar_one() is not True:
            raise GraphIdentityLockBusy("graph identity lock is busy")


__all__ = [
    "CANONICAL_ENTITY_LOCK_SCOPE",
    "ENTITY_PROJECTION_LOCK_SCOPE",
    "ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE",
    "LOGICAL_FACT_LOCK_SCOPE",
    "STABLE_PREDICATE_LOCK_SCOPE",
    "GraphIdentityLockBusy",
    "GraphIdentityLockError",
    "GraphIdentityLockScope",
    "graph_identity_lock_key",
    "lock_graph_identity_scopes",
    "normalized_graph_identity_scopes",
]
