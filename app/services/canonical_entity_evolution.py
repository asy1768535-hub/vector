"""Append-only CanonicalEntity evolution with caller-owned transactions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.canonical_entity import CANONICAL_ENTITY_STATUS_ACTIVE, CanonicalEntity
from app.models.canonical_entity_evolution import (
    EVOLUTION_ASSIGNMENT_PENDING,
    EVOLUTION_ASSIGNMENT_RESOLVED,
    EVOLUTION_DECISION_APPLIED,
    EVOLUTION_DECISION_PENDING,
    EVOLUTION_DECISION_REJECTED,
    EVOLUTION_DECISION_STALE,
    EVOLUTION_DECISION_SUPERSEDED,
    EVOLUTION_OPERATION_MERGE,
    EVOLUTION_OPERATION_REASSIGN,
    EVOLUTION_OPERATION_SPLIT,
    EVOLUTION_SOURCE_APPLIED,
    EVOLUTION_SOURCE_HISTORICAL_ONLY,
    EVOLUTION_SOURCE_PENDING,
    CanonicalEntityEvolutionCommand,
    CanonicalEntityEvolutionDecision,
    CanonicalEntityEvolutionSource,
    CanonicalEntityEvolutionSuccessor,
    CanonicalEntityProjectionAssignment,
)
from app.models.entity import Entity
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_LINK_EXISTING,
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    ENTITY_RESOLUTION_STATUS_SUPERSEDED,
    EntityResolutionDecision,
)
from app.models.library import Library
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    ENTITY_PROJECTION_LOCK_SCOPE,
    ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)

EVOLUTION_CONTRACT_VERSION = "canonical_entity_evolution_v1"
_MAX_REASON_TEXT_LENGTH = 512
_MAX_IDEMPOTENCY_KEY_LENGTH = 256


class CanonicalEvolutionError(ValueError):
    """Malformed evolution input that cannot safely be persisted."""


@dataclass(frozen=True, slots=True)
class CanonicalEvolutionContext:
    entity_id: uuid.UUID | None = None
    entity_resolution_subject_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class EvolutionProjectionPartition:
    entity_id: uuid.UUID
    target_canonical_entity_id: uuid.UUID | str | None
    partition_basis_snapshot: Mapping[str, Any]
    reason_code: str = "projection_partition"


@dataclass(frozen=True, slots=True)
class NewCanonicalSplitTarget:
    target_key: str
    canonical_name: str
    normalized_name: str
    status: str = CANONICAL_ENTITY_STATUS_ACTIVE


@dataclass(frozen=True, slots=True)
class CanonicalMergeCommand:
    library_id: uuid.UUID
    source_canonical_entity_ids: tuple[uuid.UUID, ...]
    survivor_canonical_entity_id: uuid.UUID
    idempotency_key: str
    reason_code: str
    reason_text: str
    method: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    expected_precondition_fingerprint: str | None = None
    confidence: float | None = None
    supersedes_command_id: uuid.UUID | None = None

    @property
    def operation_kind(self) -> str:
        return EVOLUTION_OPERATION_MERGE

    def with_expected_precondition(self, value: str | None):
        return replace(self, expected_precondition_fingerprint=value)


@dataclass(frozen=True, slots=True)
class CanonicalSplitCommand:
    library_id: uuid.UUID
    source_canonical_entity_id: uuid.UUID
    targets: tuple[uuid.UUID | NewCanonicalSplitTarget, ...]
    partition: tuple[EvolutionProjectionPartition, ...]
    idempotency_key: str
    reason_code: str
    reason_text: str
    method: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    expected_precondition_fingerprint: str | None = None
    confidence: float | None = None
    supersedes_command_id: uuid.UUID | None = None

    @property
    def operation_kind(self) -> str:
        return EVOLUTION_OPERATION_SPLIT

    def with_expected_precondition(self, value: str | None):
        return replace(self, expected_precondition_fingerprint=value)


@dataclass(frozen=True, slots=True)
class CanonicalReassignCommand:
    library_id: uuid.UUID
    entity_id: uuid.UUID
    from_canonical_entity_id: uuid.UUID
    target_canonical_entity_id: uuid.UUID
    idempotency_key: str
    reason_code: str
    reason_text: str
    method: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    expected_precondition_fingerprint: str | None = None
    confidence: float | None = None
    supersedes_command_id: uuid.UUID | None = None

    @property
    def operation_kind(self) -> str:
        return EVOLUTION_OPERATION_REASSIGN

    def with_expected_precondition(self, value: str | None):
        return replace(self, expected_precondition_fingerprint=value)


EvolutionCommand = CanonicalMergeCommand | CanonicalSplitCommand | CanonicalReassignCommand


@dataclass(frozen=True, slots=True)
class CurrentCanonicalIdentityResult:
    status: str
    historical_canonical_entity_id: uuid.UUID
    current_canonical_entity_id: uuid.UUID | None
    lineage_decision_ids: tuple[uuid.UUID, ...]
    resolution_eligible: bool
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class CanonicalEvolutionResult:
    status: str
    reason_code: str | None = None
    command: CanonicalEntityEvolutionCommand | None = None
    decision: CanonicalEntityEvolutionDecision | None = None
    source_transitions: tuple[CanonicalEntityEvolutionSource, ...] = ()
    successors: tuple[CanonicalEntityEvolutionSuccessor, ...] = ()
    assignments: tuple[CanonicalEntityProjectionAssignment, ...] = ()


@dataclass(frozen=True, slots=True)
class _TargetPlan:
    key: str
    canonical_entity_id: uuid.UUID | None
    target_spec_snapshot: dict[str, Any] | None


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _stable_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    raise CanonicalEvolutionError(f"unsupported canonical evolution value: {type(value).__name__}")


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(_stable_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_fingerprint(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


async def _scoped_rows(db, model, library_id: uuid.UUID, *, for_update: bool = False) -> list[Any]:
    statement = select(model).where(model.library_id == library_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    result = await db.execute(statement)
    return [row for row in result.scalars().all() if row.library_id == library_id]


def _targets(command: CanonicalSplitCommand) -> tuple[_TargetPlan, ...]:
    plans: list[_TargetPlan] = []
    for target in command.targets:
        if isinstance(target, uuid.UUID):
            plans.append(_TargetPlan(str(target), target, None))
            continue
        if not isinstance(target, NewCanonicalSplitTarget):
            raise CanonicalEvolutionError("split targets must be UUIDs or explicit new target specs")
        if (
            not target.target_key
            or not target.canonical_name.strip()
            or not target.normalized_name.strip()
            or target.status != CANONICAL_ENTITY_STATUS_ACTIVE
        ):
            raise CanonicalEvolutionError("new split target specification is invalid")
        plans.append(
            _TargetPlan(
                target.target_key,
                None,
                {
                    "canonical_name": target.canonical_name.strip(),
                    "normalized_name": target.normalized_name.strip(),
                    "status": target.status,
                    "target_key": target.target_key,
                },
            )
        )
    if len(plans) < 2 or len({plan.key for plan in plans}) != len(plans):
        raise CanonicalEvolutionError("split requires at least two distinct explicit targets")
    return tuple(plans)


def _command_payload(command: EvolutionCommand, *, include_expected: bool) -> dict[str, Any]:
    common = {
        "contract_version": EVOLUTION_CONTRACT_VERSION,
        "evidence_refs": list(command.evidence_refs),
        "idempotency_key": command.idempotency_key,
        "method": command.method,
        "operation_kind": command.operation_kind,
        "reason_code": command.reason_code,
        "reason_text": command.reason_text,
        "supersedes_command_id": command.supersedes_command_id,
    }
    if include_expected:
        common["expected_precondition_fingerprint"] = command.expected_precondition_fingerprint
    if isinstance(command, CanonicalMergeCommand):
        common.update(
            {
                "source_canonical_entity_ids": sorted(command.source_canonical_entity_ids, key=str),
                "survivor_canonical_entity_id": command.survivor_canonical_entity_id,
            }
        )
    elif isinstance(command, CanonicalSplitCommand):
        common.update(
            {
                "source_canonical_entity_id": command.source_canonical_entity_id,
                "targets": [
                    {
                        "canonical_entity_id": target.canonical_entity_id,
                        "target_key": target.key,
                        "target_spec_snapshot": target.target_spec_snapshot,
                    }
                    for target in sorted(_targets(command), key=lambda item: item.key)
                ],
                "partition": [
                    {
                        "entity_id": entry.entity_id,
                        "partition_basis_snapshot": entry.partition_basis_snapshot,
                        "reason_code": entry.reason_code,
                        "target_canonical_entity_id": entry.target_canonical_entity_id,
                    }
                    for entry in sorted(command.partition, key=lambda item: str(item.entity_id))
                ],
            }
        )
    else:
        common.update(
            {
                "entity_id": command.entity_id,
                "from_canonical_entity_id": command.from_canonical_entity_id,
                "target_canonical_entity_id": command.target_canonical_entity_id,
            }
        )
    return common


def _command_identity_payload(command: EvolutionCommand) -> dict[str, Any]:
    """Return only the stable intent identity shared by all decision versions."""

    common = {
        "contract_version": EVOLUTION_CONTRACT_VERSION,
        "library_id": command.library_id,
        "operation_kind": command.operation_kind,
    }
    if isinstance(command, CanonicalMergeCommand):
        common["source_identity"] = sorted(command.source_canonical_entity_ids, key=str)
        common["command_scope"] = {"source_set": sorted(command.source_canonical_entity_ids, key=str)}
    elif isinstance(command, CanonicalSplitCommand):
        common["source_identity"] = command.source_canonical_entity_id
        common["command_scope"] = {"source_canonical_entity_id": command.source_canonical_entity_id}
    else:
        common["source_identity"] = command.entity_id
        common["command_scope"] = {
            "entity_id": command.entity_id,
            "from_canonical_entity_id": command.from_canonical_entity_id,
        }
    return common


def _command_identity_fingerprint(command: EvolutionCommand) -> str:
    return _fingerprint(_command_identity_payload(command))


def _command_source_ids(command: EvolutionCommand) -> tuple[uuid.UUID, ...]:
    if isinstance(command, CanonicalMergeCommand):
        return command.source_canonical_entity_ids
    if isinstance(command, CanonicalSplitCommand):
        return (command.source_canonical_entity_id,)
    return (command.from_canonical_entity_id,)


def _command_target_ids(command: EvolutionCommand) -> tuple[uuid.UUID, ...]:
    if isinstance(command, CanonicalMergeCommand):
        return (command.survivor_canonical_entity_id,)
    if isinstance(command, CanonicalSplitCommand):
        return tuple(plan.canonical_entity_id for plan in _targets(command) if plan.canonical_entity_id)
    return (command.target_canonical_entity_id,)


async def _context_entity_id(
    db,
    library_id: uuid.UUID,
    context: CanonicalEvolutionContext | None,
) -> tuple[uuid.UUID | None, bool]:
    if context is None:
        return None, False
    entity_id = context.entity_id
    subject = context.entity_resolution_subject_fingerprint
    if entity_id is not None and not isinstance(entity_id, uuid.UUID):
        return None, True
    if subject is not None and not _is_fingerprint(subject):
        return None, True
    if subject is not None:
        decisions = [
            row
            for row in await _scoped_rows(db, EntityResolutionDecision, library_id)
            if row.subject_fingerprint == subject
            and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
            and row.entity_id is not None
        ]
        if len(decisions) != 1:
            return None, True
        if entity_id is not None and decisions[0].entity_id != entity_id:
            return None, True
        entity_id = decisions[0].entity_id
    if entity_id is not None:
        entity = await db.get(Entity, entity_id)
        if entity is None or entity.library_id != library_id:
            return None, True
    return entity_id, False


async def resolve_current_canonical_identity(
    db,
    library_id: uuid.UUID,
    canonical_entity_id: uuid.UUID,
    context: CanonicalEvolutionContext | None = None,
) -> CurrentCanonicalIdentityResult:
    """Resolve only persisted lineage; no name, recency, similarity, or LLM fallback."""

    if not isinstance(library_id, uuid.UUID) or not isinstance(canonical_entity_id, uuid.UUID):
        raise CanonicalEvolutionError("library and canonical entity identifiers must be UUIDs")
    canonical_rows = await _scoped_rows(db, CanonicalEntity, library_id)
    canonical_by_id = {row.id: row for row in canonical_rows}
    if canonical_entity_id not in canonical_by_id:
        return CurrentCanonicalIdentityResult(
            "pending", canonical_entity_id, None, (), False, "canonical_scope_mismatch"
        )
    entity_id, invalid_context = await _context_entity_id(db, library_id, context)
    if invalid_context:
        return CurrentCanonicalIdentityResult(
            "pending", canonical_entity_id, None, (), False, "invalid_resolution_context"
        )
    transitions = await _scoped_rows(db, CanonicalEntityEvolutionSource, library_id)
    successors = await _scoped_rows(db, CanonicalEntityEvolutionSuccessor, library_id)
    decisions = {
        row.id: row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionDecision, library_id)
    }
    assignments = await _scoped_rows(db, CanonicalEntityProjectionAssignment, library_id)
    current = canonical_entity_id
    visited: set[uuid.UUID] = set()
    lineage: list[uuid.UUID] = []
    while True:
        if current in visited:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
            )
        visited.add(current)
        current_transitions = [
            row
            for row in transitions
            if row.source_canonical_entity_id == current
            and row.resolution_state in {
                EVOLUTION_SOURCE_PENDING,
                EVOLUTION_SOURCE_APPLIED,
                EVOLUTION_SOURCE_HISTORICAL_ONLY,
            }
        ]
        if not current_transitions:
            leaf = canonical_by_id.get(current)
            if leaf is None:
                return CurrentCanonicalIdentityResult(
                    "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
                )
            return CurrentCanonicalIdentityResult(
                "resolved",
                canonical_entity_id,
                current,
                tuple(lineage),
                leaf.status == CANONICAL_ENTITY_STATUS_ACTIVE,
                None,
            )
        if len(current_transitions) != 1:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
            )
        transition = current_transitions[0]
        transition_successors = [
            row for row in successors if row.source_transition_id == transition.id
        ]
        if transition.resolution_state == EVOLUTION_SOURCE_PENDING:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "evolution_pending"
            )
        if transition.resolution_state == EVOLUTION_SOURCE_HISTORICAL_ONLY:
            if transition_successors:
                return CurrentCanonicalIdentityResult(
                    "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
                )
            return CurrentCanonicalIdentityResult(
                "historical_only", canonical_entity_id, None, tuple(lineage), False, None
            )
        decision = decisions.get(transition.evolution_decision_id)
        if decision is None or decision.lifecycle_status != EVOLUTION_DECISION_APPLIED:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
            )
        lineage.append(decision.id)
        target_ids = [row.target_canonical_entity_id for row in transition_successors]
        if any(target is None or target not in canonical_by_id for target in target_ids):
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
            )
        if decision.operation_kind == EVOLUTION_OPERATION_MERGE:
            if len(target_ids) != 1:
                return CurrentCanonicalIdentityResult(
                    "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
                )
            current = target_ids[0]
            continue
        if decision.operation_kind != EVOLUTION_OPERATION_SPLIT or len(target_ids) < 2:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "lineage_integrity_error"
            )
        if entity_id is None:
            return CurrentCanonicalIdentityResult(
                "forked", canonical_entity_id, None, tuple(lineage), False, "split_context_required"
            )
        partition_rows = [
            row
            for row in assignments
            if row.evolution_decision_id == decision.id and row.entity_id == entity_id
        ]
        if len(partition_rows) != 1:
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "invalid_resolution_context"
            )
        partition = partition_rows[0]
        if (
            partition.assignment_state != EVOLUTION_ASSIGNMENT_RESOLVED
            or partition.target_canonical_entity_id not in target_ids
        ):
            return CurrentCanonicalIdentityResult(
                "pending", canonical_entity_id, None, tuple(lineage), False, "projection_pending"
            )
        current = partition.target_canonical_entity_id


async def build_evolution_precondition_fingerprint(db, command: EvolutionCommand) -> str:
    """Snapshot persisted state that must remain true after all locks are acquired."""

    source_ids = set(_command_source_ids(command))
    target_ids = set(_command_target_ids(command))
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id)
        if row.canonical_entity_id in source_ids
        or (isinstance(command, CanonicalReassignCommand) and row.id == command.entity_id)
    ]
    entity_ids = {row.id for row in entities}
    decisions = [
        row
        for row in await _scoped_rows(db, EntityResolutionDecision, command.library_id)
        if row.entity_id in entity_ids and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    ]
    canonical_rows = {
        row.id: row for row in await _scoped_rows(db, CanonicalEntity, command.library_id)
    }
    current_states = []
    for canonical_id in sorted(source_ids | target_ids, key=str):
        canonical = canonical_rows.get(canonical_id)
        current = await resolve_current_canonical_identity(db, command.library_id, canonical_id)
        current_states.append(
            {
                "canonical_entity_id": canonical_id,
                "canonical_status": canonical.status if canonical is not None else None,
                "current_canonical_entity_id": current.current_canonical_entity_id,
                "current_status": current.status,
                "reason_code": current.reason_code,
            }
        )
    return _fingerprint(
        {
            "command": _command_payload(command, include_expected=False),
            "current_states": current_states,
            "projections": [
                {"canonical_entity_id": row.canonical_entity_id, "entity_id": row.id}
                for row in sorted(entities, key=lambda row: str(row.id))
            ],
            "resolution_decisions": [
                {
                    "canonical_entity_id": row.canonical_entity_id,
                    "decision_fingerprint": row.decision_fingerprint,
                    "entity_id": row.entity_id,
                    "id": row.id,
                    "subject_fingerprint": row.subject_fingerprint,
                }
                for row in sorted(decisions, key=lambda row: str(row.id))
            ],
        }
    )


async def _existing_command(
    db, library_id: uuid.UUID, idempotency_key: str
) -> CanonicalEntityEvolutionCommand | None:
    rows = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionCommand, library_id)
        if row.idempotency_key == idempotency_key
    ]
    if len(rows) > 1:
        raise CanonicalEvolutionError("idempotency key uniqueness is corrupt")
    return rows[0] if rows else None


async def _decision_rows(
    db, library_id: uuid.UUID, command_id: uuid.UUID
) -> list[CanonicalEntityEvolutionDecision]:
    return [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionDecision, library_id)
        if row.command_id == command_id
    ]


async def _replay_result(
    db,
    root: CanonicalEntityEvolutionCommand,
    command_identity_fingerprint: str,
) -> CanonicalEvolutionResult:
    if root.request_fingerprint != command_identity_fingerprint:
        return CanonicalEvolutionResult("REJECTED", "idempotency_key_conflict", command=root)
    decisions = await _decision_rows(db, root.library_id, root.id)
    applied = next((row for row in decisions if row.lifecycle_status == EVOLUTION_DECISION_APPLIED), None)
    if applied is not None:
        return CanonicalEvolutionResult("REUSED", command=root, decision=applied)
    pending = next((row for row in decisions if row.lifecycle_status == EVOLUTION_DECISION_PENDING), None)
    if pending is not None:
        return CanonicalEvolutionResult("PENDING", command=root, decision=pending)
    stale = next((row for row in decisions if row.lifecycle_status == EVOLUTION_DECISION_STALE), None)
    if stale is not None:
        return CanonicalEvolutionResult("STALE_OPERATION", stale.reason_code, root, stale)
    rejected = next((row for row in decisions if row.lifecycle_status == EVOLUTION_DECISION_REJECTED), None)
    return CanonicalEvolutionResult("REJECTED", rejected.reason_code if rejected else "command_unusable", root, rejected)


async def _lock_command_scopes(db, command: EvolutionCommand) -> None:
    source_ids = set(_command_source_ids(command))
    target_ids = set(_command_target_ids(command))
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id)
        if row.canonical_entity_id in source_ids
        or (isinstance(command, CanonicalReassignCommand) and row.id == command.entity_id)
    ]
    entity_ids = {row.id for row in entities}
    active = [
        row
        for row in await _scoped_rows(db, EntityResolutionDecision, command.library_id)
        if row.entity_id in entity_ids and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    ]
    scopes = [
        *(GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, canonical_id) for canonical_id in source_ids | target_ids),
        *(GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, entity.id) for entity in entities),
        *(
            GraphIdentityLockScope(ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE, decision.subject_fingerprint)
            for decision in active
        ),
    ]
    await lock_graph_identity_scopes(db, command.library_id, scopes)


async def _new_root(
    db, command: EvolutionCommand, command_identity_fingerprint: str
) -> CanonicalEntityEvolutionCommand:
    root = CanonicalEntityEvolutionCommand(
        id=uuid.uuid4(),
        library_id=command.library_id,
        idempotency_key=command.idempotency_key,
        request_fingerprint=command_identity_fingerprint,
        operation_kind=command.operation_kind,
        contract_version=EVOLUTION_CONTRACT_VERSION,
        supersedes_command_id=command.supersedes_command_id,
    )
    db.add(root)
    await db.flush()
    return root


def _new_decision(
    root: CanonicalEntityEvolutionCommand,
    command: EvolutionCommand,
    lifecycle_status: str,
    precondition_fingerprint: str,
    *,
    reason_code: str | None = None,
) -> CanonicalEntityEvolutionDecision:
    return CanonicalEntityEvolutionDecision(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        operation_kind=command.operation_kind,
        lifecycle_status=lifecycle_status,
        reason_code=reason_code or command.reason_code,
        reason_text=command.reason_text.strip()[:_MAX_REASON_TEXT_LENGTH],
        method=command.method,
        confidence=command.confidence,
        evidence_refs=[_stable_value(item) for item in command.evidence_refs],
        precondition_fingerprint=precondition_fingerprint,
        supersedes_decision_id=None,
    )


async def _append_terminal(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: EvolutionCommand,
    lifecycle_status: str,
    precondition_fingerprint: str,
    reason_code: str,
) -> CanonicalEvolutionResult:
    decision = _new_decision(root, command, lifecycle_status, precondition_fingerprint, reason_code=reason_code)
    db.add(decision)
    await db.flush()
    vocabulary = {
        EVOLUTION_DECISION_STALE: "STALE_OPERATION",
        EVOLUTION_DECISION_REJECTED: "REJECTED",
        EVOLUTION_DECISION_PENDING: "PENDING",
    }
    return CanonicalEvolutionResult(vocabulary[lifecycle_status], reason_code, root, decision)


async def _equivalent_applied_transition(
    db, command: EvolutionCommand
) -> CanonicalEntityEvolutionDecision | None:
    if not isinstance(command, CanonicalMergeCommand):
        return None
    sources = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, command.library_id)
        if row.source_canonical_entity_id in set(command.source_canonical_entity_ids)
        and row.resolution_state == EVOLUTION_SOURCE_APPLIED
    ]
    if len(sources) != len(set(command.source_canonical_entity_ids)):
        return None
    successors = await _scoped_rows(db, CanonicalEntityEvolutionSuccessor, command.library_id)
    by_source = {
        row.source_transition_id: row.target_canonical_entity_id for row in successors
    }
    if any(by_source.get(source.id) != command.survivor_canonical_entity_id for source in sources):
        return None
    decisions = {
        row.id: row for row in await _scoped_rows(db, CanonicalEntityEvolutionDecision, command.library_id)
    }
    rows = {decisions.get(source.evolution_decision_id) for source in sources}
    rows.discard(None)
    return next(iter(rows)) if len(rows) == 1 else None


async def _has_cycle(
    db, library_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> bool:
    if source_id == target_id:
        return True
    sources = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, library_id)
        if row.resolution_state == EVOLUTION_SOURCE_APPLIED
    ]
    successors = await _scoped_rows(db, CanonicalEntityEvolutionSuccessor, library_id)
    targets_by_transition: dict[uuid.UUID, list[uuid.UUID]] = {}
    for successor in successors:
        if successor.target_canonical_entity_id is not None:
            targets_by_transition.setdefault(successor.source_transition_id, []).append(
                successor.target_canonical_entity_id
            )
    adjacency: dict[uuid.UUID, set[uuid.UUID]] = {}
    for transition in sources:
        adjacency.setdefault(transition.source_canonical_entity_id, set()).update(
            targets_by_transition.get(transition.id, ())
        )
    todo = [target_id]
    seen: set[uuid.UUID] = set()
    while todo:
        current = todo.pop()
        if current == source_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        todo.extend(adjacency.get(current, ()))
    return False


async def _active_decision_for_entity(
    db, library_id: uuid.UUID, entity_id: uuid.UUID
) -> EntityResolutionDecision | None:
    active = [
        row
        for row in await _scoped_rows(db, EntityResolutionDecision, library_id, for_update=True)
        if row.entity_id == entity_id and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    ]
    return active[0] if len(active) == 1 else None


def _clone_resolution_decision(
    previous: EntityResolutionDecision,
    target_canonical_entity_id: uuid.UUID,
    evolution_decision_id: uuid.UUID,
) -> EntityResolutionDecision:
    return EntityResolutionDecision(
        id=uuid.uuid4(),
        library_id=previous.library_id,
        subject_fingerprint=previous.subject_fingerprint,
        decision_fingerprint=_fingerprint(
            {
                "evolution_decision_id": evolution_decision_id,
                "previous_decision_fingerprint": previous.decision_fingerprint,
                "schema_version": EVOLUTION_CONTRACT_VERSION,
                "target_canonical_entity_id": target_canonical_entity_id,
            }
        ),
        graph_entity_candidate_id=previous.graph_entity_candidate_id,
        entity_id=previous.entity_id,
        canonical_entity_id=target_canonical_entity_id,
        observed_name=previous.observed_name,
        observed_normalized_name=previous.observed_normalized_name,
        observed_type_key=previous.observed_type_key,
        identifier_snapshot=previous.identifier_snapshot,
        candidate_snapshot=previous.candidate_snapshot,
        evidence_refs=previous.evidence_refs,
        decision_kind=ENTITY_RESOLUTION_LINK_EXISTING,
        lifecycle_status=ENTITY_RESOLUTION_STATUS_ACTIVE,
        method=EVOLUTION_CONTRACT_VERSION,
        confidence=previous.confidence,
        reason_code=previous.reason_code,
        resolver_version=EVOLUTION_CONTRACT_VERSION,
        supersedes_decision_id=previous.id,
    )


def _new_assignment(
    decision: CanonicalEntityEvolutionDecision,
    entity: Entity,
    from_canonical_entity_id: uuid.UUID,
    target_canonical_entity_id: uuid.UUID | None,
    state: str,
    basis: Mapping[str, Any],
    reason_code: str,
    previous: EntityResolutionDecision | None = None,
    new: EntityResolutionDecision | None = None,
) -> CanonicalEntityProjectionAssignment:
    return CanonicalEntityProjectionAssignment(
        id=uuid.uuid4(),
        library_id=decision.library_id,
        evolution_decision_id=decision.id,
        entity_id=entity.id,
        from_canonical_entity_id=from_canonical_entity_id,
        target_canonical_entity_id=target_canonical_entity_id,
        assignment_state=state,
        partition_basis_snapshot=_stable_value(dict(basis)),
        reason_code=reason_code,
        previous_entity_resolution_decision_id=previous.id if previous else None,
        new_entity_resolution_decision_id=new.id if new else None,
    )


async def _apply_projection(
    db,
    decision: CanonicalEntityEvolutionDecision,
    entity: Entity,
    target_canonical_entity_id: uuid.UUID,
    basis: Mapping[str, Any],
    reason_code: str,
) -> CanonicalEntityProjectionAssignment | None:
    previous = await _active_decision_for_entity(db, decision.library_id, entity.id)
    if previous is None or previous.canonical_entity_id != entity.canonical_entity_id:
        return None
    new = _clone_resolution_decision(previous, target_canonical_entity_id, decision.id)
    previous.lifecycle_status = ENTITY_RESOLUTION_STATUS_SUPERSEDED
    entity.canonical_entity_id = target_canonical_entity_id
    assignment = _new_assignment(
        decision,
        entity,
        previous.canonical_entity_id,
        target_canonical_entity_id,
        EVOLUTION_ASSIGNMENT_RESOLVED,
        basis,
        reason_code,
        previous,
        new,
    )
    db.add(new)
    db.add(assignment)
    return assignment


async def _leaf_is_current_active(
    db, library_id: uuid.UUID, canonical_entity_id: uuid.UUID
) -> bool:
    result = await resolve_current_canonical_identity(db, library_id, canonical_entity_id)
    return (
        result.status == "resolved"
        and result.current_canonical_entity_id == canonical_entity_id
        and result.resolution_eligible
    )


async def _pending_split(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: CanonicalSplitCommand,
    precondition_fingerprint: str,
    entities: list[Entity],
    reason_code: str,
) -> CanonicalEvolutionResult:
    decision = _new_decision(root, command, EVOLUTION_DECISION_PENDING, precondition_fingerprint, reason_code=reason_code)
    transition = CanonicalEntityEvolutionSource(
        id=uuid.uuid4(),
        library_id=command.library_id,
        evolution_decision_id=decision.id,
        source_canonical_entity_id=command.source_canonical_entity_id,
        resolution_state=EVOLUTION_SOURCE_PENDING,
    )
    db.add(decision)
    db.add(transition)
    successors: list[CanonicalEntityEvolutionSuccessor] = []
    for target in _targets(command):
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(),
            library_id=command.library_id,
            source_transition_id=transition.id,
            target_canonical_entity_id=target.canonical_entity_id,
            target_spec_snapshot=target.target_spec_snapshot,
        )
        successors.append(successor)
        db.add(successor)
    entries = {entry.entity_id: entry for entry in command.partition}
    assignments = []
    for entity in entities:
        entry = entries.get(entity.id)
        basis = dict(entry.partition_basis_snapshot) if entry else {}
        if entry is not None:
            basis["requested_target"] = str(entry.target_canonical_entity_id) if entry.target_canonical_entity_id else None
        assignment = _new_assignment(
            decision,
            entity,
            command.source_canonical_entity_id,
            None,
            EVOLUTION_ASSIGNMENT_PENDING,
            basis,
            reason_code,
        )
        assignments.append(assignment)
        db.add(assignment)
    await db.flush()
    return CanonicalEvolutionResult(
        "PENDING", reason_code, root, decision, (transition,), tuple(successors), tuple(assignments)
    )


async def _apply_merge(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: CanonicalMergeCommand,
    precondition_fingerprint: str,
) -> CanonicalEvolutionResult:
    source_ids = tuple(dict.fromkeys(command.source_canonical_entity_ids))
    if not source_ids or len(source_ids) != len(command.source_canonical_entity_ids):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "invalid_sources")
    if command.survivor_canonical_entity_id in source_ids:
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "survivor_must_be_explicit_non_source")
    for source_id in source_ids:
        if await _has_cycle(db, command.library_id, source_id, command.survivor_canonical_entity_id):
            return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "cycle")
    if not await _leaf_is_current_active(db, command.library_id, command.survivor_canonical_entity_id):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "survivor_not_current_active_leaf")
    for source_id in source_ids:
        if not await _leaf_is_current_active(db, command.library_id, source_id):
            return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "source_not_current_active_leaf")
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id in source_ids
    ]
    missing = [
        row
        for row in entities
        if await _active_decision_for_entity(db, command.library_id, row.id) is None
    ]
    if missing:
        decision = _new_decision(root, command, EVOLUTION_DECISION_PENDING, precondition_fingerprint, reason_code="projection_decision_missing")
        db.add(decision)
        sources = []
        assignments = []
        for source_id in source_ids:
            source = CanonicalEntityEvolutionSource(
                id=uuid.uuid4(), library_id=command.library_id, evolution_decision_id=decision.id,
                source_canonical_entity_id=source_id, resolution_state=EVOLUTION_SOURCE_PENDING,
            )
            sources.append(source)
            db.add(source)
        for entity in entities:
            assignment = _new_assignment(
                decision, entity, entity.canonical_entity_id, None, EVOLUTION_ASSIGNMENT_PENDING,
                {}, "projection_decision_missing",
            )
            assignments.append(assignment)
            db.add(assignment)
        await db.flush()
        return CanonicalEvolutionResult("PENDING", "projection_decision_missing", root, decision, tuple(sources), (), tuple(assignments))
    decision = _new_decision(root, command, EVOLUTION_DECISION_APPLIED, precondition_fingerprint)
    db.add(decision)
    sources = []
    successors = []
    for source_id in source_ids:
        source = CanonicalEntityEvolutionSource(
            id=uuid.uuid4(), library_id=command.library_id, evolution_decision_id=decision.id,
            source_canonical_entity_id=source_id, resolution_state=EVOLUTION_SOURCE_APPLIED,
        )
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(), library_id=command.library_id, source_transition_id=source.id,
            target_canonical_entity_id=command.survivor_canonical_entity_id, target_spec_snapshot=None,
        )
        sources.append(source)
        successors.append(successor)
        db.add(source)
        db.add(successor)
    assignments: list[CanonicalEntityProjectionAssignment] = []
    for entity in entities:
        assignment = await _apply_projection(
            db, decision, entity, command.survivor_canonical_entity_id,
            {"operation": "merge", "survivor_canonical_entity_id": str(command.survivor_canonical_entity_id)},
            "merge_survivor",
        )
        if assignment is None:
            raise CanonicalEvolutionError("projection state changed after lock")
        assignments.append(assignment)
    await db.flush()
    return CanonicalEvolutionResult("APPLIED", None, root, decision, tuple(sources), tuple(successors), tuple(assignments))


async def _apply_split(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: CanonicalSplitCommand,
    precondition_fingerprint: str,
) -> CanonicalEvolutionResult:
    try:
        targets = _targets(command)
    except CanonicalEvolutionError as exc:
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, str(exc))
    if not await _leaf_is_current_active(db, command.library_id, command.source_canonical_entity_id):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "source_not_current_active_leaf")
    for target in targets:
        if target.canonical_entity_id is not None:
            if await _has_cycle(db, command.library_id, command.source_canonical_entity_id, target.canonical_entity_id):
                return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "cycle")
            if not await _leaf_is_current_active(db, command.library_id, target.canonical_entity_id):
                return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "target_not_current_active_leaf")
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id == command.source_canonical_entity_id
    ]
    entity_ids = [row.id for row in entities]
    partition_ids = [entry.entity_id for entry in command.partition]
    if len(partition_ids) != len(set(partition_ids)) or set(partition_ids) != set(entity_ids):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "projection_partition_incomplete")
    target_keys = {target.key for target in targets}
    if any(
        entry.target_canonical_entity_id is not None and str(entry.target_canonical_entity_id) not in target_keys
        for entry in command.partition
    ):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "projection_target_invalid")
    if any(entry.target_canonical_entity_id is None for entry in command.partition):
        return await _pending_split(db, root, command, precondition_fingerprint, entities, "projection_partition_pending")
    for entity in entities:
        if await _active_decision_for_entity(db, command.library_id, entity.id) is None:
            return await _pending_split(
                db,
                root,
                command,
                precondition_fingerprint,
                entities,
                "projection_decision_missing",
            )
    resolved_targets: dict[str, uuid.UUID] = {}
    for target in targets:
        if target.canonical_entity_id is not None:
            resolved_targets[target.key] = target.canonical_entity_id
            continue
        assert target.target_spec_snapshot is not None
        created = CanonicalEntity(
            id=uuid.uuid4(),
            library_id=command.library_id,
            canonical_name=target.target_spec_snapshot["canonical_name"],
            normalized_name=target.target_spec_snapshot["normalized_name"],
            status=target.target_spec_snapshot["status"],
        )
        resolved_targets[target.key] = created.id
        db.add(created)
    decision = _new_decision(root, command, EVOLUTION_DECISION_APPLIED, precondition_fingerprint)
    transition = CanonicalEntityEvolutionSource(
        id=uuid.uuid4(), library_id=command.library_id, evolution_decision_id=decision.id,
        source_canonical_entity_id=command.source_canonical_entity_id, resolution_state=EVOLUTION_SOURCE_APPLIED,
    )
    db.add(decision)
    db.add(transition)
    successors = []
    for target in targets:
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(), library_id=command.library_id, source_transition_id=transition.id,
            target_canonical_entity_id=resolved_targets[target.key], target_spec_snapshot=target.target_spec_snapshot,
        )
        successors.append(successor)
        db.add(successor)
    entries = {entry.entity_id: entry for entry in command.partition}
    assignments = []
    for entity in entities:
        entry = entries[entity.id]
        assert entry.target_canonical_entity_id is not None
        target_id = resolved_targets[str(entry.target_canonical_entity_id)]
        assignment = await _apply_projection(
            db, decision, entity, target_id, entry.partition_basis_snapshot, entry.reason_code
        )
        if assignment is None:
            raise CanonicalEvolutionError("projection state changed after lock")
        assignments.append(assignment)
    await db.flush()
    return CanonicalEvolutionResult("APPLIED", None, root, decision, (transition,), tuple(successors), tuple(assignments))


async def _apply_reassign(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: CanonicalReassignCommand,
    precondition_fingerprint: str,
) -> CanonicalEvolutionResult:
    if command.from_canonical_entity_id == command.target_canonical_entity_id:
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_REJECTED, precondition_fingerprint, "reassign_target_unchanged")
    if not await _leaf_is_current_active(db, command.library_id, command.from_canonical_entity_id) or not await _leaf_is_current_active(db, command.library_id, command.target_canonical_entity_id):
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "canonical_not_current_active_leaf")
    entity = await db.get(Entity, command.entity_id)
    if entity is None or entity.library_id != command.library_id or entity.canonical_entity_id != command.from_canonical_entity_id:
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, precondition_fingerprint, "projection_state_changed")
    previous = await _active_decision_for_entity(db, command.library_id, entity.id)
    if previous is None:
        decision = _new_decision(root, command, EVOLUTION_DECISION_PENDING, precondition_fingerprint, reason_code="projection_decision_missing")
        assignment = _new_assignment(
            decision, entity, command.from_canonical_entity_id, None, EVOLUTION_ASSIGNMENT_PENDING,
            {}, "projection_decision_missing",
        )
        db.add(decision)
        db.add(assignment)
        await db.flush()
        return CanonicalEvolutionResult("PENDING", "projection_decision_missing", root, decision, (), (), (assignment,))
    decision = _new_decision(root, command, EVOLUTION_DECISION_APPLIED, precondition_fingerprint)
    db.add(decision)
    assignment = await _apply_projection(
        db, decision, entity, command.target_canonical_entity_id,
        {"operation": "reassign", "entity_id": str(entity.id)}, command.reason_code,
    )
    if assignment is None:
        raise CanonicalEvolutionError("projection state changed after lock")
    await db.flush()
    return CanonicalEvolutionResult("APPLIED", None, root, decision, (), (), (assignment,))


def _validate_command(command: EvolutionCommand) -> str | None:
    if not isinstance(command.library_id, uuid.UUID):
        return "library_id_invalid"
    if not isinstance(command.idempotency_key, str) or not command.idempotency_key.strip() or len(command.idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        return "idempotency_key_invalid"
    if not _is_fingerprint(command.expected_precondition_fingerprint):
        return "expected_precondition_invalid"
    if not isinstance(command.reason_code, str) or not command.reason_code.strip() or not isinstance(command.reason_text, str) or not command.reason_text.strip():
        return "reason_invalid"
    if not isinstance(command.method, str) or not command.method.strip():
        return "method_invalid"
    if not isinstance(command.evidence_refs, tuple):
        return "evidence_invalid"
    if command.confidence is not None and (not isinstance(command.confidence, (int, float)) or not 0 <= command.confidence <= 1):
        return "confidence_invalid"
    return None


async def _scope_error(db, command: EvolutionCommand) -> str | None:
    library = await db.get(Library, command.library_id)
    if library is None:
        return "library_not_found"
    for canonical_entity_id in set(_command_source_ids(command)) | set(_command_target_ids(command)):
        canonical = await db.get(CanonicalEntity, canonical_entity_id)
        if canonical is None or canonical.library_id != command.library_id:
            return "canonical_scope_mismatch"
    if isinstance(command, CanonicalReassignCommand):
        entity = await db.get(Entity, command.entity_id)
        if entity is None or entity.library_id != command.library_id:
            return "entity_scope_mismatch"
    return None


async def apply_canonical_evolution(db, command: EvolutionCommand) -> CanonicalEvolutionResult:
    """Apply one CanonicalEntity command; the caller owns commit and rollback."""

    if not isinstance(command, (CanonicalMergeCommand, CanonicalSplitCommand, CanonicalReassignCommand)):
        raise CanonicalEvolutionError("unsupported canonical evolution command")
    validation_error = _validate_command(command)
    scope_error = await _scope_error(db, command)
    if scope_error == "library_not_found":
        return CanonicalEvolutionResult("REJECTED", scope_error)
    command_identity_fingerprint = _command_identity_fingerprint(command)
    existing = await _existing_command(db, command.library_id, command.idempotency_key)
    if existing is not None:
        return await _replay_result(db, existing, command_identity_fingerprint)
    await _lock_command_scopes(db, command)
    existing = await _existing_command(db, command.library_id, command.idempotency_key)
    if existing is not None:
        return await _replay_result(db, existing, command_identity_fingerprint)
    actual_precondition = await build_evolution_precondition_fingerprint(db, command)
    root = await _new_root(db, command, command_identity_fingerprint)
    if validation_error is not None or scope_error is not None:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            actual_precondition,
            validation_error or scope_error,
        )
    if command.expected_precondition_fingerprint != actual_precondition:
        return await _append_terminal(db, root, command, EVOLUTION_DECISION_STALE, actual_precondition, "precondition_changed")
    equivalent = await _equivalent_applied_transition(db, command)
    if equivalent is not None:
        return CanonicalEvolutionResult("REUSED", "equivalent_applied_transition", root, equivalent)
    try:
        if isinstance(command, CanonicalMergeCommand):
            return await _apply_merge(db, root, command, actual_precondition)
        if isinstance(command, CanonicalSplitCommand):
            return await _apply_split(db, root, command, actual_precondition)
        return await _apply_reassign(db, root, command, actual_precondition)
    except IntegrityError:
        return CanonicalEvolutionResult("RETRYABLE_CONFLICT", "integrity_conflict", root)


async def _pending_completion_is_ready(db, command: EvolutionCommand) -> bool:
    if isinstance(command, CanonicalSplitCommand):
        if any(entry.target_canonical_entity_id is None for entry in command.partition):
            return False
        source_ids = {command.source_canonical_entity_id}
    elif isinstance(command, CanonicalMergeCommand):
        source_ids = set(command.source_canonical_entity_ids)
    else:
        return await _active_decision_for_entity(db, command.library_id, command.entity_id) is not None
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id in source_ids
    ]
    for entity in entities:
        if await _active_decision_for_entity(db, command.library_id, entity.id) is None:
            return False
    return True


async def complete_pending_canonical_evolution(
    db, command: EvolutionCommand
) -> CanonicalEvolutionResult:
    """Append a D2 successor for a ready pending command without changing D1 payload."""

    if not isinstance(command, (CanonicalMergeCommand, CanonicalSplitCommand, CanonicalReassignCommand)):
        raise CanonicalEvolutionError("unsupported canonical evolution command")
    command_identity_fingerprint = _command_identity_fingerprint(command)
    root = await _existing_command(db, command.library_id, command.idempotency_key)
    if root is None:
        return CanonicalEvolutionResult("REJECTED", "pending_command_not_found")
    if root.request_fingerprint != command_identity_fingerprint:
        return CanonicalEvolutionResult("REJECTED", "idempotency_key_conflict", root)
    await _lock_command_scopes(db, command)
    decisions = await _decision_rows(db, command.library_id, root.id)
    pending = next((row for row in decisions if row.lifecycle_status == EVOLUTION_DECISION_PENDING), None)
    if pending is None:
        return await _replay_result(db, root, command_identity_fingerprint)
    if not await _pending_completion_is_ready(db, command):
        return CanonicalEvolutionResult("PENDING", pending.reason_code, root, pending)
    prior_sources = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, command.library_id, for_update=True)
        if row.evolution_decision_id == pending.id and row.resolution_state == EVOLUTION_SOURCE_PENDING
    ]
    for source in prior_sources:
        source.resolution_state = "superseded"
    precondition = await build_evolution_precondition_fingerprint(db, command)
    if isinstance(command, CanonicalMergeCommand):
        result = await _apply_merge(db, root, command, precondition)
    elif isinstance(command, CanonicalSplitCommand):
        result = await _apply_split(db, root, command, precondition)
    else:
        result = await _apply_reassign(db, root, command, precondition)
    if result.decision is None or result.status == "PENDING":
        for source in prior_sources:
            source.resolution_state = EVOLUTION_SOURCE_PENDING
        return result
    result.decision.supersedes_decision_id = pending.id
    pending.lifecycle_status = EVOLUTION_DECISION_SUPERSEDED
    await db.flush()
    return result


__all__ = [
    "CanonicalEvolutionContext",
    "CanonicalEvolutionError",
    "CanonicalEvolutionResult",
    "CanonicalMergeCommand",
    "CanonicalReassignCommand",
    "CanonicalSplitCommand",
    "CurrentCanonicalIdentityResult",
    "EvolutionProjectionPartition",
    "NewCanonicalSplitTarget",
    "apply_canonical_evolution",
    "build_evolution_precondition_fingerprint",
    "complete_pending_canonical_evolution",
    "resolve_current_canonical_identity",
]
