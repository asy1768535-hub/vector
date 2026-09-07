"""Append-only CanonicalEntity evolution with caller-owned transactions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from unicodedata import normalize

import rfc8785
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.canonical_entity import CANONICAL_ENTITY_STATUS_ACTIVE, CanonicalEntity
from app.models.canonical_entity_evolution import (
    EVOLUTION_ASSIGNMENT_PENDING,
    EVOLUTION_ASSIGNMENT_RESOLVED,
    EVOLUTION_ASSIGNMENT_SUPERSEDED,
    EVOLUTION_DECISION_APPLIED,
    EVOLUTION_DECISION_CANCELLED,
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
    EVOLUTION_SOURCE_SUPERSEDED,
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
from app.models.graph_governance_action import GraphGovernanceAction
from app.models.library import Library
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    ENTITY_PROJECTION_LOCK_SCOPE,
    ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)

EVOLUTION_CONTRACT_VERSION = "canonical_entity_evolution/v1"
_MAX_REASON_TEXT_LENGTH = 512
_MAX_IDEMPOTENCY_KEY_LENGTH = 256


class CanonicalEvolutionError(ValueError):
    """Malformed evolution input that cannot safely be persisted."""


@dataclass(frozen=True, slots=True)
class CanonicalEvolutionContext:
    entity_id: uuid.UUID | None = None
    entity_resolution_subject_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalEvolutionCancellation:
    """Authenticated control request that terminates exactly one pending Decision."""

    command_id: uuid.UUID
    original_idempotency_key: str
    expected_pending_decision_id: uuid.UUID
    reason_code: str
    reason_text: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    actor_type: str
    actor_id: uuid.UUID
    request_id: str


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
    actor_type: str
    actor_id: uuid.UUID
    request_id: str
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None
    projection_assignments: tuple[EvolutionProjectionPartition, ...] = ()

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
    actor_type: str
    actor_id: uuid.UUID
    request_id: str
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None

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
    actor_type: str
    actor_id: uuid.UUID
    request_id: str
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None

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
    reused_decision_id: uuid.UUID | None = None
    effective_outcome: str | None = None
    current_decision_status: str | None = None
    current_decision_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class _TargetPlan:
    key: str
    canonical_entity_id: uuid.UUID | None
    target_spec_snapshot: dict[str, Any] | None


def _stable_value(value: Any) -> Any:
    """Validate the RFC 8785 / I-JSON domain before handing it to the JCS encoder."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if normalize("NFC", value) != value:
            raise CanonicalEvolutionError("canonical evolution strings must be NFC")
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise CanonicalEvolutionError("canonical evolution integer is outside the I-JSON safe range")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or (value.is_zero() and value.is_signed()):
            raise CanonicalEvolutionError("canonical evolution decimal is invalid")
        if value == value.to_integral_value():
            return _stable_value(int(value))
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0 and math.copysign(1, value) < 0):
            raise CanonicalEvolutionError("canonical evolution number is invalid")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalEvolutionError("canonical evolution object keys must be strings")
        return {key: _stable_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    raise CanonicalEvolutionError(f"unsupported canonical evolution value: {type(value).__name__}")


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return rfc8785.dumps(_stable_value(value))
    except rfc8785.CanonicalizationError as exc:
        raise CanonicalEvolutionError("canonical evolution value is not RFC 8785 JSON") from exc


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


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
        "method": command.method,
        "operation_kind": command.operation_kind,
        "reason_code": command.reason_code,
        "reason_text": command.reason_text,
    }
    if include_expected:
        common["expected_precondition_fingerprint"] = command.expected_precondition_fingerprint
    if isinstance(command, CanonicalMergeCommand):
        common.update(
            {
                "source_canonical_entity_ids": sorted(command.source_canonical_entity_ids, key=str),
                "survivor_canonical_entity_id": command.survivor_canonical_entity_id,
                "projection_assignments": [
                    {
                        "entity_id": entry.entity_id,
                        "partition_basis_snapshot": entry.partition_basis_snapshot,
                        "reason_code": entry.reason_code,
                        "target_canonical_entity_id": entry.target_canonical_entity_id,
                    }
                    for entry in sorted(command.projection_assignments, key=lambda item: str(item.entity_id))
                ],
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
        participants = sorted(
            {*command.source_canonical_entity_ids, command.survivor_canonical_entity_id}, key=str
        )
        common["source_identity"] = {"participant_canonical_entity_ids": participants}
        common["command_scope"] = {
            "survivor_canonical_entity_id": command.survivor_canonical_entity_id,
        }
    elif isinstance(command, CanonicalSplitCommand):
        target_refs = [
            (
                f"existing:{target.canonical_entity_id}",
                {"canonical_entity_id": target.canonical_entity_id, "kind": "existing"},
            )
            if target.canonical_entity_id is not None
            else (f"new:{target.key}", {"kind": "new", "target_key": target.key})
            for target in _targets(command)
        ]
        if len({key for key, _ in target_refs}) != len(target_refs):
            raise CanonicalEvolutionError("split target slots must be distinct")
        common["source_identity"] = {
            "source_canonical_entity_id": command.source_canonical_entity_id,
        }
        common["command_scope"] = {
            "target_refs": [target for _, target in sorted(target_refs)],
        }
    else:
        common["source_identity"] = {"entity_projection_id": command.entity_id}
        common["command_scope"] = {
            "from_canonical_entity_id": command.from_canonical_entity_id,
            "target_canonical_entity_id": command.target_canonical_entity_id,
        }
    return common


def canonical_evolution_json_bytes(
    value: EvolutionCommand | Mapping[str, Any], *, identity: bool = False
) -> bytes:
    """Return the one RFC 8785 byte representation used by P3.1 fingerprints."""

    if identity:
        if not isinstance(value, (CanonicalMergeCommand, CanonicalSplitCommand, CanonicalReassignCommand)):
            raise CanonicalEvolutionError("command identity requires a canonical evolution command")
        return _canonical_json_bytes(_command_identity_payload(value))
    if not isinstance(value, Mapping):
        raise CanonicalEvolutionError("canonical evolution JSON must be an object")
    return _canonical_json_bytes(value)


def _command_identity_fingerprint(command: EvolutionCommand) -> str:
    return hashlib.sha256(canonical_evolution_json_bytes(command, identity=True)).hexdigest()


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
    if isinstance(command, CanonicalMergeCommand) and not command.projection_assignments:
        command = replace(
            command,
            projection_assignments=tuple(
                EvolutionProjectionPartition(
                    entity_id=row.id,
                    target_canonical_entity_id=row.canonical_entity_id,
                    partition_basis_snapshot={},
                    reason_code="merge_survivor",
                )
                for row in sorted(entities, key=lambda item: str(item.id))
            ),
        )
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


async def _existing_command_by_identity(
    db, library_id: uuid.UUID, command_identity_fingerprint: str
) -> CanonicalEntityEvolutionCommand | None:
    rows = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionCommand, library_id)
        if row.command_identity_fingerprint == command_identity_fingerprint
    ]
    if len(rows) > 1:
        raise CanonicalEvolutionError("command identity uniqueness is corrupt")
    return rows[0] if rows else None


async def _new_root_slot_is_occupied(db, command: EvolutionCommand) -> bool:
    source_ids = set(_command_source_ids(command))
    sources = await _scoped_rows(db, CanonicalEntityEvolutionSource, command.library_id)
    return any(
        row.source_canonical_entity_id in source_ids
        and row.resolution_state in {EVOLUTION_SOURCE_PENDING, EVOLUTION_SOURCE_APPLIED}
        for row in sources
    )


async def _graph_governance_projection_intent_is_occupied(
    db, command: EvolutionCommand
) -> bool:
    """Keep ontology Entity merge and canonical evolution mutually exclusive."""

    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id)
        if row.canonical_entity_id in set(_command_source_ids(command))
        or (isinstance(command, CanonicalReassignCommand) and row.id == command.entity_id)
    ]
    entity_ids = {row.id for row in entities}
    if not entity_ids:
        return False
    actions = await _scoped_rows(db, GraphGovernanceAction, command.library_id)
    return any(
        row.action_kind == "entity_merge"
        and row.status in {"pending_review", "approved"}
        and ({row.survivor_entity_id, row.loser_entity_id} & entity_ids)
        for row in actions
    )


async def _decision_rows(
    db, library_id: uuid.UUID, command_id: uuid.UUID
) -> list[CanonicalEntityEvolutionDecision]:
    return [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionDecision, library_id)
        if row.command_id == command_id
    ]


def _current_decision_head(
    decisions: list[CanonicalEntityEvolutionDecision],
) -> CanonicalEntityEvolutionDecision | None:
    """Find the one head from persisted predecessor links, never row order."""

    predecessor_ids = {
        row.supersedes_decision_id
        for row in decisions
        if row.supersedes_decision_id is not None
    }
    heads = [row for row in decisions if row.id not in predecessor_ids]
    return heads[0] if len(heads) == 1 else None


async def _replay_result(
    db,
    root: CanonicalEntityEvolutionCommand,
    decision_payload_fingerprint: str,
) -> CanonicalEvolutionResult | None:
    decisions = await _decision_rows(db, root.library_id, root.id)
    current = _current_decision_head(decisions)
    if current is None:
        return CanonicalEvolutionResult("REJECTED", "command_unusable", command=root)
    reused = next(
        (row for row in decisions if row.decision_payload_fingerprint == decision_payload_fingerprint),
        None,
    )
    if reused is None:
        return None
    if reused.evaluated_outcome == EVOLUTION_DECISION_CANCELLED:
        return CanonicalEvolutionResult(
            "CANCELLED",
            "cancel_already_committed",
            root,
            reused,
            current_decision_status=current.lifecycle_status,
            current_decision_id=current.id,
        )
    return CanonicalEvolutionResult(
        "REUSED",
        "exact_current_decision_replay"
        if reused.id == current.id
        else "exact_historical_decision_replay",
        root,
        reused,
        reused_decision_id=reused.id,
        effective_outcome=reused.evaluated_outcome.upper(),
        current_decision_status=current.lifecycle_status,
        current_decision_id=current.id,
    )


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
    identity_payload = _command_identity_payload(command)
    root = CanonicalEntityEvolutionCommand(
        id=uuid.uuid4(),
        library_id=command.library_id,
        idempotency_key=command.idempotency_key,
        command_identity_fingerprint=command_identity_fingerprint,
        operation_kind=command.operation_kind,
        source_identity_snapshot=_stable_value(identity_payload["source_identity"]),
        command_scope_snapshot=_stable_value(identity_payload["command_scope"]),
        contract_version=EVOLUTION_CONTRACT_VERSION,
    )
    db.add(root)
    await db.flush()
    return root


def _decision_snapshots(command: EvolutionCommand) -> tuple[dict[str, Any], list[dict[str, Any]] | None, Any]:
    if isinstance(command, CanonicalMergeCommand):
        assignments = [
            {
                "entity_id": str(entry.entity_id),
                "from_canonical_entity_id": str(entry.target_canonical_entity_id),
                "partition_basis_snapshot": _stable_value(dict(entry.partition_basis_snapshot)),
                "reason_code": entry.reason_code,
                "state": "resolved",
                "target_canonical_entity_id": str(command.survivor_canonical_entity_id),
            }
            for entry in sorted(command.projection_assignments, key=lambda item: str(item.entity_id))
        ]
        return (
            {"survivor_canonical_entity_id": str(command.survivor_canonical_entity_id)},
            None,
            {
                "projection_assignments": assignments,
                "survivor_canonical_entity_id": str(command.survivor_canonical_entity_id),
            },
        )
    if isinstance(command, CanonicalSplitCommand):
        partition = [
            {
                "entity_id": str(entry.entity_id),
                "from_canonical_entity_id": str(command.source_canonical_entity_id),
                "partition_basis_snapshot": _stable_value(dict(entry.partition_basis_snapshot)),
                "reason_code": entry.reason_code,
                "state": "resolved" if entry.target_canonical_entity_id is not None else "pending",
                "target_ref": (
                    {"kind": "existing", "canonical_entity_id": str(entry.target_canonical_entity_id)}
                    if isinstance(entry.target_canonical_entity_id, uuid.UUID)
                    else ({"kind": "new", "target_key": entry.target_canonical_entity_id}
                          if entry.target_canonical_entity_id is not None else None)
                ),
            }
            for entry in sorted(command.partition, key=lambda item: str(item.entity_id))
        ]
        target_specs = [
            target.target_spec_snapshot
            for target in _targets(command)
            if target.target_spec_snapshot is not None
        ]
        return (
            {"target_specifications": target_specs},
            partition,
            {"projection_assignments": partition, "target_specifications": target_specs},
        )
    assignment = {
        "entity_id": str(command.entity_id),
        "from_canonical_entity_id": str(command.from_canonical_entity_id),
        "partition_basis_snapshot": {},
        "reason_code": command.reason_code,
        "target_canonical_entity_id": str(command.target_canonical_entity_id),
    }
    return ({"target_canonical_entity_id": str(command.target_canonical_entity_id)}, None, {"projection_assignment": assignment})


def _decision_payload_fingerprint(command: EvolutionCommand) -> str:
    _, _, operation_payload = _decision_snapshots(command)
    evidence = sorted(
        {_canonical_json_bytes(_stable_value(dict(item))) for item in command.evidence_refs}
    )
    return _fingerprint(
        {
            "confidence": command.confidence,
            "evidence_refs": [json.loads(item) for item in evidence],
            "expected_precondition_fingerprint": command.expected_precondition_fingerprint,
            "method": command.method,
            "operation_payload": operation_payload,
            "reason_code": command.reason_code,
            "reason_text": command.reason_text,
        }
    )


def _cancellation_payload_fingerprint(
    cancellation: CanonicalEvolutionCancellation,
    expected_precondition_fingerprint: str,
) -> str:
    evidence = sorted(
        {_canonical_json_bytes(_stable_value(dict(item))) for item in cancellation.evidence_refs}
    )
    return _fingerprint(
        {
            "confidence": None,
            "evidence_refs": [json.loads(item) for item in evidence],
            "expected_precondition_fingerprint": expected_precondition_fingerprint,
            "method": "authorized_cancellation",
            "operation_payload": {
                "control_kind": "cancel_pending",
                "expected_pending_decision_id": cancellation.expected_pending_decision_id,
            },
            "reason_code": cancellation.reason_code,
            "reason_text": cancellation.reason_text,
        }
    )


def _new_decision(
    root: CanonicalEntityEvolutionCommand,
    command: EvolutionCommand,
    evaluated_outcome: str,
    observed_precondition_fingerprint: str,
    *,
    reason_code: str | None = None,
    supersedes_decision_id: uuid.UUID | None = None,
) -> CanonicalEntityEvolutionDecision:
    successor_detail_snapshot, split_partition_snapshot, projection_assignment_snapshot = _decision_snapshots(command)
    return CanonicalEntityEvolutionDecision(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        decision_payload_fingerprint=_decision_payload_fingerprint(command),
        operation_kind=command.operation_kind,
        evaluated_outcome=evaluated_outcome,
        lifecycle_status=evaluated_outcome,
        successor_detail_snapshot=successor_detail_snapshot,
        split_partition_snapshot=split_partition_snapshot,
        projection_assignment_snapshot=projection_assignment_snapshot,
        reason_code=reason_code or command.reason_code,
        reason_text=command.reason_text.strip()[:_MAX_REASON_TEXT_LENGTH],
        method=command.method,
        confidence=command.confidence,
        evidence_refs=[_stable_value(item) for item in command.evidence_refs],
        precondition_fingerprint=command.expected_precondition_fingerprint or observed_precondition_fingerprint,
        observed_precondition_fingerprint=observed_precondition_fingerprint,
        supersedes_decision_id=supersedes_decision_id,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        request_id=command.request_id,
    )


async def _append_terminal(
    db,
    root: CanonicalEntityEvolutionCommand,
    command: EvolutionCommand,
    lifecycle_status: str,
    precondition_fingerprint: str,
    reason_code: str,
    *,
    supersedes_decision_id: uuid.UUID | None = None,
) -> CanonicalEvolutionResult:
    decision = _new_decision(
        root,
        command,
        lifecycle_status,
        precondition_fingerprint,
        reason_code=reason_code,
        supersedes_decision_id=supersedes_decision_id,
    )
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
    active = await _active_decisions_for_entity(db, library_id, entity_id)
    return active[0] if active else None


async def _active_decisions_for_entity(
    db, library_id: uuid.UUID, entity_id: uuid.UUID
) -> list[EntityResolutionDecision]:
    active = [
        row
        for row in await _scoped_rows(db, EntityResolutionDecision, library_id, for_update=True)
        if row.entity_id == entity_id
        and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
        and row.canonical_entity_id is not None
    ]
    return sorted(active, key=lambda row: (row.subject_fingerprint, str(row.id)))


def _clone_resolution_decision(
    previous: EntityResolutionDecision,
    target_canonical_entity_id: uuid.UUID,
    evolution_decision_id: uuid.UUID,
    evolution_assignment_id: uuid.UUID,
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
        evolution_assignment_id=evolution_assignment_id,
    )


def _new_assignment(
    decision: CanonicalEntityEvolutionDecision,
    entity: Entity,
    from_canonical_entity_id: uuid.UUID,
    target_canonical_entity_id: uuid.UUID | None,
    state: str,
    basis: Mapping[str, Any],
    reason_code: str,
    target_successor_id: uuid.UUID | None = None,
    supersedes_assignment_id: uuid.UUID | None = None,
) -> CanonicalEntityProjectionAssignment:
    return CanonicalEntityProjectionAssignment(
        id=uuid.uuid4(),
        library_id=decision.library_id,
        command_id=decision.command_id,
        evolution_decision_id=decision.id,
        entity_id=entity.id,
        supersedes_assignment_id=supersedes_assignment_id,
        from_canonical_entity_id=from_canonical_entity_id,
        target_successor_id=target_successor_id,
        target_canonical_entity_id=target_canonical_entity_id,
        assignment_state=state,
        partition_basis_snapshot=_stable_value(dict(basis)),
        reason_code=reason_code,
    )


async def _apply_projection(
    db,
    decision: CanonicalEntityEvolutionDecision,
    entity: Entity,
    target_canonical_entity_id: uuid.UUID,
    basis: Mapping[str, Any],
    reason_code: str,
    target_successor_id: uuid.UUID | None = None,
    supersedes_assignment_id: uuid.UUID | None = None,
) -> CanonicalEntityProjectionAssignment | None:
    previous = await _active_decisions_for_entity(db, decision.library_id, entity.id)
    if not previous or any(
        row.canonical_entity_id != entity.canonical_entity_id for row in previous
    ):
        return None
    assignment = _new_assignment(
        decision,
        entity,
        entity.canonical_entity_id,
        target_canonical_entity_id,
        EVOLUTION_ASSIGNMENT_RESOLVED,
        basis,
        reason_code,
        target_successor_id=target_successor_id,
        supersedes_assignment_id=supersedes_assignment_id,
    )
    for row in previous:
        row.lifecycle_status = ENTITY_RESOLUTION_STATUS_SUPERSEDED
    await db.flush()
    for row in previous:
        db.add(
            _clone_resolution_decision(
                row, target_canonical_entity_id, decision.id, assignment.id
            )
        )
    entity.canonical_entity_id = target_canonical_entity_id
    db.add(assignment)
    return assignment


def _merge_entries_from_snapshot(
    command: CanonicalMergeCommand,
    snapshot: list[dict[str, Any]],
) -> CanonicalMergeCommand:
    """Reconstruct an exact historical merge payload without using row recency."""

    entries: list[EvolutionProjectionPartition] = []
    for value in snapshot:
        try:
            target_id = uuid.UUID(value["target_canonical_entity_id"])
            entity_id = uuid.UUID(value["entity_id"])
            from_id = uuid.UUID(value["from_canonical_entity_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CanonicalEvolutionError("stored merge projection snapshot is invalid") from exc
        if target_id != command.survivor_canonical_entity_id:
            raise CanonicalEvolutionError("stored merge projection snapshot is inconsistent")
        entries.append(
            EvolutionProjectionPartition(
                entity_id=entity_id,
                target_canonical_entity_id=from_id,
                partition_basis_snapshot=value.get("partition_basis_snapshot", {}),
                reason_code=value.get("reason_code", "merge_survivor"),
            )
        )
    return replace(command, projection_assignments=tuple(entries))


async def _evaluate_merge_projection_payload(
    db,
    command: CanonicalMergeCommand,
) -> CanonicalMergeCommand:
    """Freeze every direct losing projection into the Decision payload before root INSERT."""

    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id in set(command.source_canonical_entity_ids)
    ]
    expected_ids = {row.id for row in entities}
    if not command.projection_assignments:
        return replace(
            command,
            projection_assignments=tuple(
                EvolutionProjectionPartition(
                    entity_id=row.id,
                    target_canonical_entity_id=row.canonical_entity_id,
                    partition_basis_snapshot={},
                    reason_code="merge_survivor",
                )
                for row in sorted(entities, key=lambda item: str(item.id))
            ),
        )
    provided = {entry.entity_id: entry for entry in command.projection_assignments}
    if len(provided) != len(command.projection_assignments) or set(provided) != expected_ids:
        raise CanonicalEvolutionError("projection_partition_incomplete")
    for entity in entities:
        entry = provided[entity.id]
        if entry.target_canonical_entity_id != entity.canonical_entity_id:
            raise CanonicalEvolutionError("projection_source_invalid")
    return command


def _new_cancellation_decision(
    root: CanonicalEntityEvolutionCommand,
    cancellation: CanonicalEvolutionCancellation,
    expected_precondition_fingerprint: str,
    observed_precondition_fingerprint: str,
) -> CanonicalEntityEvolutionDecision:
    return CanonicalEntityEvolutionDecision(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        decision_payload_fingerprint=_cancellation_payload_fingerprint(
            cancellation, expected_precondition_fingerprint
        ),
        operation_kind=root.operation_kind,
        evaluated_outcome=EVOLUTION_DECISION_CANCELLED,
        lifecycle_status=EVOLUTION_DECISION_CANCELLED,
        successor_detail_snapshot={},
        split_partition_snapshot=None,
        projection_assignment_snapshot={},
        reason_code=cancellation.reason_code,
        reason_text=cancellation.reason_text.strip()[:_MAX_REASON_TEXT_LENGTH],
        method="authorized_cancellation",
        confidence=None,
        evidence_refs=[_stable_value(item) for item in cancellation.evidence_refs],
        precondition_fingerprint=expected_precondition_fingerprint,
        observed_precondition_fingerprint=observed_precondition_fingerprint,
        supersedes_decision_id=cancellation.expected_pending_decision_id,
        actor_type=cancellation.actor_type,
        actor_id=cancellation.actor_id,
        request_id=cancellation.request_id,
    )


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
    *,
    supersedes_decision_id: uuid.UUID | None = None,
    previous_sources: Mapping[uuid.UUID, CanonicalEntityEvolutionSource] | None = None,
    previous_assignments: Mapping[uuid.UUID, CanonicalEntityProjectionAssignment] | None = None,
) -> CanonicalEvolutionResult:
    source_predecessors = previous_sources or {}
    assignment_predecessors = previous_assignments or {}
    decision = _new_decision(
        root,
        command,
        EVOLUTION_DECISION_PENDING,
        precondition_fingerprint,
        reason_code=reason_code,
        supersedes_decision_id=supersedes_decision_id,
    )
    transition = CanonicalEntityEvolutionSource(
        id=uuid.uuid4(),
        library_id=command.library_id,
        command_id=root.id,
        evolution_decision_id=decision.id,
        source_canonical_entity_id=command.source_canonical_entity_id,
        supersedes_source_transition_id=(
            source_predecessors[command.source_canonical_entity_id].id
            if command.source_canonical_entity_id in source_predecessors
            else None
        ),
        resolution_state=EVOLUTION_SOURCE_PENDING,
    )
    db.add(decision)
    db.add(transition)
    successors: list[CanonicalEntityEvolutionSuccessor] = []
    for target in _targets(command):
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(),
            library_id=command.library_id,
            command_id=root.id,
            evolution_decision_id=decision.id,
            source_transition_id=transition.id,
            target_ref_kind="existing" if target.canonical_entity_id is not None else "new",
            target_ref_key=str(target.canonical_entity_id) if target.canonical_entity_id is not None else target.key,
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
            supersedes_assignment_id=(
                assignment_predecessors[entity.id].id
                if entity.id in assignment_predecessors
                else None
            ),
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
    *,
    supersedes_decision_id: uuid.UUID | None = None,
    previous_sources: Mapping[uuid.UUID, CanonicalEntityEvolutionSource] | None = None,
    previous_assignments: Mapping[uuid.UUID, CanonicalEntityProjectionAssignment] | None = None,
) -> CanonicalEvolutionResult:
    source_predecessors = previous_sources or {}
    assignment_predecessors = previous_assignments or {}
    source_ids = tuple(dict.fromkeys(command.source_canonical_entity_ids))
    if not source_ids or len(source_ids) != len(command.source_canonical_entity_ids):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "invalid_sources",
            supersedes_decision_id=supersedes_decision_id,
        )
    if command.survivor_canonical_entity_id in source_ids:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "survivor_must_be_explicit_non_source",
            supersedes_decision_id=supersedes_decision_id,
        )
    if not await _leaf_is_current_active(db, command.library_id, command.survivor_canonical_entity_id):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_STALE,
            precondition_fingerprint,
            "survivor_not_current_active_leaf",
            supersedes_decision_id=supersedes_decision_id,
        )
    for source_id in source_ids:
        if not await _leaf_is_current_active(db, command.library_id, source_id):
            return await _append_terminal(
                db,
                root,
                command,
                EVOLUTION_DECISION_STALE,
                precondition_fingerprint,
                "source_not_current_active_leaf",
                supersedes_decision_id=supersedes_decision_id,
            )
        if await _has_cycle(db, command.library_id, source_id, command.survivor_canonical_entity_id):
            return await _append_terminal(
                db,
                root,
                command,
                EVOLUTION_DECISION_STALE,
                precondition_fingerprint,
                "lineage_integrity_error",
                supersedes_decision_id=supersedes_decision_id,
            )
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id in source_ids
    ]
    entries = {entry.entity_id: entry for entry in command.projection_assignments}
    if len(entries) != len(command.projection_assignments) or set(entries) != {
        row.id for row in entities
    }:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "projection_partition_incomplete",
            supersedes_decision_id=supersedes_decision_id,
        )
    if any(
        entries[entity.id].target_canonical_entity_id != entity.canonical_entity_id
        for entity in entities
    ):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "projection_source_invalid",
            supersedes_decision_id=supersedes_decision_id,
        )
    missing = [
        row
        for row in entities
        if await _active_decision_for_entity(db, command.library_id, row.id) is None
    ]
    if missing:
        decision = _new_decision(
            root,
            command,
            EVOLUTION_DECISION_PENDING,
            precondition_fingerprint,
            reason_code="projection_decision_missing",
            supersedes_decision_id=supersedes_decision_id,
        )
        db.add(decision)
        sources = []
        assignments = []
        for source_id in source_ids:
            source = CanonicalEntityEvolutionSource(
                id=uuid.uuid4(), library_id=command.library_id, command_id=root.id,
                evolution_decision_id=decision.id, source_canonical_entity_id=source_id,
                supersedes_source_transition_id=(
                    source_predecessors[source_id].id
                    if source_id in source_predecessors
                    else None
                ),
                resolution_state=EVOLUTION_SOURCE_PENDING,
            )
            sources.append(source)
            db.add(source)
        for entity in entities:
            entry = entries[entity.id]
            assignment = _new_assignment(
                decision, entity, entity.canonical_entity_id, None, EVOLUTION_ASSIGNMENT_PENDING,
                entry.partition_basis_snapshot, entry.reason_code,
                supersedes_assignment_id=(
                    assignment_predecessors[entity.id].id
                    if entity.id in assignment_predecessors
                    else None
                ),
            )
            assignments.append(assignment)
            db.add(assignment)
        await db.flush()
        return CanonicalEvolutionResult("PENDING", "projection_decision_missing", root, decision, tuple(sources), (), tuple(assignments))
    decision = _new_decision(
        root,
        command,
        EVOLUTION_DECISION_APPLIED,
        precondition_fingerprint,
        supersedes_decision_id=supersedes_decision_id,
    )
    db.add(decision)
    sources = []
    successors = []
    for source_id in source_ids:
        source = CanonicalEntityEvolutionSource(
            id=uuid.uuid4(), library_id=command.library_id, command_id=root.id,
            evolution_decision_id=decision.id, source_canonical_entity_id=source_id,
            supersedes_source_transition_id=(
                source_predecessors[source_id].id
                if source_id in source_predecessors
                else None
            ),
            resolution_state=EVOLUTION_SOURCE_APPLIED,
        )
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(), library_id=command.library_id, command_id=root.id,
            evolution_decision_id=decision.id, source_transition_id=source.id,
            target_ref_kind="existing", target_ref_key=str(command.survivor_canonical_entity_id),
            target_canonical_entity_id=command.survivor_canonical_entity_id, target_spec_snapshot=None,
        )
        sources.append(source)
        successors.append(successor)
        db.add(source)
        db.add(successor)
    assignments: list[CanonicalEntityProjectionAssignment] = []
    successor_by_source = {
        source.source_canonical_entity_id: successor.id
        for source, successor in zip(sources, successors, strict=True)
    }
    for entity in entities:
        entry = entries[entity.id]
        assignment = await _apply_projection(
            db, decision, entity, command.survivor_canonical_entity_id,
            entry.partition_basis_snapshot,
            entry.reason_code,
            successor_by_source[entity.canonical_entity_id],
            supersedes_assignment_id=(
                assignment_predecessors[entity.id].id
                if entity.id in assignment_predecessors
                else None
            ),
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
    *,
    supersedes_decision_id: uuid.UUID | None = None,
    previous_sources: Mapping[uuid.UUID, CanonicalEntityEvolutionSource] | None = None,
    previous_assignments: Mapping[uuid.UUID, CanonicalEntityProjectionAssignment] | None = None,
) -> CanonicalEvolutionResult:
    source_predecessors = previous_sources or {}
    assignment_predecessors = previous_assignments or {}
    try:
        targets = _targets(command)
    except CanonicalEvolutionError as exc:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            str(exc),
            supersedes_decision_id=supersedes_decision_id,
        )
    if not await _leaf_is_current_active(db, command.library_id, command.source_canonical_entity_id):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_STALE,
            precondition_fingerprint,
            "source_not_current_active_leaf",
            supersedes_decision_id=supersedes_decision_id,
        )
    for target in targets:
        if target.canonical_entity_id is not None:
            if not await _leaf_is_current_active(db, command.library_id, target.canonical_entity_id):
                return await _append_terminal(
                    db,
                    root,
                    command,
                    EVOLUTION_DECISION_STALE,
                    precondition_fingerprint,
                    "target_not_current_active_leaf",
                    supersedes_decision_id=supersedes_decision_id,
                )
            if await _has_cycle(db, command.library_id, command.source_canonical_entity_id, target.canonical_entity_id):
                return await _append_terminal(
                    db,
                    root,
                    command,
                    EVOLUTION_DECISION_STALE,
                    precondition_fingerprint,
                    "lineage_integrity_error",
                    supersedes_decision_id=supersedes_decision_id,
                )
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id == command.source_canonical_entity_id
    ]
    entity_ids = [row.id for row in entities]
    partition_ids = [entry.entity_id for entry in command.partition]
    if len(partition_ids) != len(set(partition_ids)) or set(partition_ids) != set(entity_ids):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "projection_partition_incomplete",
            supersedes_decision_id=supersedes_decision_id,
        )
    target_keys = {target.key for target in targets}
    if any(
        entry.target_canonical_entity_id is not None and str(entry.target_canonical_entity_id) not in target_keys
        for entry in command.partition
    ):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "projection_target_invalid",
            supersedes_decision_id=supersedes_decision_id,
        )
    if any(entry.target_canonical_entity_id is None for entry in command.partition):
        return await _pending_split(
            db,
            root,
            command,
            precondition_fingerprint,
            entities,
            "projection_partition_pending",
            supersedes_decision_id=supersedes_decision_id,
            previous_sources=source_predecessors,
            previous_assignments=assignment_predecessors,
        )
    for entity in entities:
        if await _active_decision_for_entity(db, command.library_id, entity.id) is None:
            return await _pending_split(
                db,
                root,
                command,
                precondition_fingerprint,
                entities,
                "projection_decision_missing",
                supersedes_decision_id=supersedes_decision_id,
                previous_sources=source_predecessors,
                previous_assignments=assignment_predecessors,
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
    decision = _new_decision(
        root,
        command,
        EVOLUTION_DECISION_APPLIED,
        precondition_fingerprint,
        supersedes_decision_id=supersedes_decision_id,
    )
    transition = CanonicalEntityEvolutionSource(
        id=uuid.uuid4(), library_id=command.library_id, command_id=root.id,
        evolution_decision_id=decision.id, source_canonical_entity_id=command.source_canonical_entity_id,
        supersedes_source_transition_id=(
            source_predecessors[command.source_canonical_entity_id].id
            if command.source_canonical_entity_id in source_predecessors
            else None
        ),
        resolution_state=EVOLUTION_SOURCE_APPLIED,
    )
    db.add(decision)
    db.add(transition)
    successors = []
    for target in targets:
        successor = CanonicalEntityEvolutionSuccessor(
            id=uuid.uuid4(), library_id=command.library_id, command_id=root.id,
            evolution_decision_id=decision.id, source_transition_id=transition.id,
            target_ref_kind="existing" if target.canonical_entity_id is not None else "new",
            target_ref_key=target.key, target_canonical_entity_id=resolved_targets[target.key],
            target_spec_snapshot=target.target_spec_snapshot,
        )
        successors.append(successor)
        db.add(successor)
    entries = {entry.entity_id: entry for entry in command.partition}
    assignments = []
    successor_by_target = {
        successor.target_canonical_entity_id: successor.id for successor in successors
    }
    for entity in entities:
        entry = entries[entity.id]
        assert entry.target_canonical_entity_id is not None
        target_id = resolved_targets[str(entry.target_canonical_entity_id)]
        assignment = await _apply_projection(
            db,
            decision,
            entity,
            target_id,
            entry.partition_basis_snapshot,
            entry.reason_code,
            successor_by_target[target_id],
            supersedes_assignment_id=(
                assignment_predecessors[entity.id].id
                if entity.id in assignment_predecessors
                else None
            ),
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
    *,
    supersedes_decision_id: uuid.UUID | None = None,
    previous_assignments: Mapping[uuid.UUID, CanonicalEntityProjectionAssignment] | None = None,
) -> CanonicalEvolutionResult:
    assignment_predecessors = previous_assignments or {}
    if command.from_canonical_entity_id == command.target_canonical_entity_id:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_REJECTED,
            precondition_fingerprint,
            "reassign_target_unchanged",
            supersedes_decision_id=supersedes_decision_id,
        )
    if not await _leaf_is_current_active(db, command.library_id, command.from_canonical_entity_id) or not await _leaf_is_current_active(db, command.library_id, command.target_canonical_entity_id):
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_STALE,
            precondition_fingerprint,
            "canonical_not_current_active_leaf",
            supersedes_decision_id=supersedes_decision_id,
        )
    entity = await db.get(Entity, command.entity_id)
    if entity is None or entity.library_id != command.library_id or entity.canonical_entity_id != command.from_canonical_entity_id:
        return await _append_terminal(
            db,
            root,
            command,
            EVOLUTION_DECISION_STALE,
            precondition_fingerprint,
            "projection_state_changed",
            supersedes_decision_id=supersedes_decision_id,
        )
    previous = await _active_decision_for_entity(db, command.library_id, entity.id)
    if previous is None:
        decision = _new_decision(
            root,
            command,
            EVOLUTION_DECISION_PENDING,
            precondition_fingerprint,
            reason_code="projection_decision_missing",
            supersedes_decision_id=supersedes_decision_id,
        )
        assignment = _new_assignment(
            decision, entity, command.from_canonical_entity_id, None, EVOLUTION_ASSIGNMENT_PENDING,
            {}, "projection_decision_missing",
            supersedes_assignment_id=(
                assignment_predecessors[entity.id].id
                if entity.id in assignment_predecessors
                else None
            ),
        )
        db.add(decision)
        db.add(assignment)
        await db.flush()
        return CanonicalEvolutionResult("PENDING", "projection_decision_missing", root, decision, (), (), (assignment,))
    decision = _new_decision(
        root,
        command,
        EVOLUTION_DECISION_APPLIED,
        precondition_fingerprint,
        supersedes_decision_id=supersedes_decision_id,
    )
    db.add(decision)
    assignment = await _apply_projection(
        db, decision, entity, command.target_canonical_entity_id,
        {"operation": "reassign", "entity_id": str(entity.id)}, command.reason_code,
        supersedes_assignment_id=(
            assignment_predecessors[entity.id].id
            if entity.id in assignment_predecessors
            else None
        ),
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
    if command.confidence is not None and (
        not isinstance(command.confidence, Decimal)
        or command.confidence.as_tuple().exponent < -6
        or not 0 <= command.confidence <= 1
    ):
        return "confidence_invalid"
    if command.actor_type not in {"user", "service"} or not isinstance(command.actor_id, uuid.UUID):
        return "actor_invalid"
    if not isinstance(command.request_id, str) or not command.request_id.strip() or len(command.request_id) > 128:
        return "request_id_invalid"
    return None


def _command_shape_error(command: EvolutionCommand) -> str | None:
    if isinstance(command, CanonicalMergeCommand):
        sources = command.source_canonical_entity_ids
        if not sources or len(sources) != len(set(sources)):
            return "invalid_sources"
        if command.survivor_canonical_entity_id in sources:
            return "survivor_must_be_explicit_non_source"
    elif isinstance(command, CanonicalSplitCommand):
        try:
            _targets(command)
        except CanonicalEvolutionError:
            return "split_targets_invalid"
    elif command.from_canonical_entity_id == command.target_canonical_entity_id:
        return "reassign_target_unchanged"
    return None


async def _current_identity_admission_error(
    db,
    command: EvolutionCommand,
    *,
    pending_predecessor_id: uuid.UUID | None = None,
) -> str | None:
    if isinstance(command, CanonicalMergeCommand):
        source_ids = command.source_canonical_entity_ids
        target_ids = (command.survivor_canonical_entity_id,)
    elif isinstance(command, CanonicalSplitCommand):
        source_ids = (command.source_canonical_entity_id,)
        target_ids = tuple(
            target.canonical_entity_id
            for target in _targets(command)
            if target.canonical_entity_id is not None
        )
    else:
        source_ids = (command.from_canonical_entity_id,)
        target_ids = (command.target_canonical_entity_id,)
        entity = await db.get(Entity, command.entity_id)
        if entity is None or entity.canonical_entity_id != command.from_canonical_entity_id:
            return "current_identity_changed"
    pending_sources = {
        row.source_canonical_entity_id
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, command.library_id)
        if row.evolution_decision_id == pending_predecessor_id
        and row.resolution_state == EVOLUTION_SOURCE_PENDING
    }
    for canonical_id in target_ids:
        if not await _leaf_is_current_active(db, command.library_id, canonical_id):
            return "current_identity_changed"
    for canonical_id in source_ids:
        if canonical_id not in pending_sources and not await _leaf_is_current_active(
            db, command.library_id, canonical_id
        ):
            return "current_identity_changed"
    for source_id in source_ids:
        for target_id in target_ids:
            if await _has_cycle(db, command.library_id, source_id, target_id):
                return "lineage_integrity_error"
    return None


async def _split_partition_error(
    db, command: CanonicalSplitCommand
) -> str | None:
    """Reject an invalid correction before releasing its pending proposal."""

    try:
        targets = _targets(command)
    except CanonicalEvolutionError:
        return "split_targets_invalid"
    entities = [
        row
        for row in await _scoped_rows(db, Entity, command.library_id, for_update=True)
        if row.canonical_entity_id == command.source_canonical_entity_id
    ]
    partition_ids = [entry.entity_id for entry in command.partition]
    if len(partition_ids) != len(set(partition_ids)) or set(partition_ids) != {
        row.id for row in entities
    }:
        return "projection_partition_incomplete"
    target_keys = {target.key for target in targets}
    if any(
        entry.target_canonical_entity_id is not None
        and str(entry.target_canonical_entity_id) not in target_keys
        for entry in command.partition
    ):
        return "projection_target_invalid"
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
    if validation_error is not None:
        return CanonicalEvolutionResult("REJECTED", validation_error)
    shape_error = _command_shape_error(command)
    if shape_error is not None:
        return CanonicalEvolutionResult("REJECTED", shape_error)
    scope_error = await _scope_error(db, command)
    if scope_error is not None:
        return CanonicalEvolutionResult("REJECTED", scope_error)
    command_identity_fingerprint = _command_identity_fingerprint(command)
    existing = await _existing_command(db, command.library_id, command.idempotency_key)
    if existing is not None and existing.command_identity_fingerprint != command_identity_fingerprint:
        return CanonicalEvolutionResult("REJECTED", "idempotency_key_conflict", existing)
    if existing is None:
        alias = await _existing_command_by_identity(
            db, command.library_id, command_identity_fingerprint
        )
        if alias is not None:
            return CanonicalEvolutionResult("REJECTED", "command_identity_alias_key", alias)
    await _lock_command_scopes(db, command)
    existing = await _existing_command(db, command.library_id, command.idempotency_key)
    if existing is not None and existing.command_identity_fingerprint != command_identity_fingerprint:
        return CanonicalEvolutionResult("REJECTED", "idempotency_key_conflict", existing)
    if existing is None:
        alias = await _existing_command_by_identity(
            db, command.library_id, command_identity_fingerprint
        )
        if alias is not None:
            return CanonicalEvolutionResult("REJECTED", "command_identity_alias_key", alias)
    predecessor: CanonicalEntityEvolutionDecision | None = None
    previous_sources: dict[uuid.UUID, CanonicalEntityEvolutionSource] = {}
    previous_assignments: dict[uuid.UUID, CanonicalEntityProjectionAssignment] = {}
    if existing is not None:
        decisions = await _decision_rows(db, existing.library_id, existing.id)
        predecessor = _current_decision_head(decisions)
        if isinstance(command, CanonicalMergeCommand) and not command.projection_assignments:
            snapshot_decision = predecessor
            if snapshot_decision is not None and isinstance(
                snapshot_decision.projection_assignment_snapshot, dict
            ):
                snapshot = snapshot_decision.projection_assignment_snapshot.get(
                    "projection_assignments"
                )
                if isinstance(snapshot, list):
                    command = _merge_entries_from_snapshot(command, snapshot)
        actual_precondition = await build_evolution_precondition_fingerprint(db, command)
        replay = await _replay_result(db, existing, _decision_payload_fingerprint(command))
        if replay is not None:
            return replay
        if predecessor is None:
            return CanonicalEvolutionResult("REJECTED", "command_unusable", existing)
        if command.expected_predecessor_decision_id != predecessor.id:
            return CanonicalEvolutionResult(
                "STALE_OPERATION",
                "expected_predecessor_mismatch",
                existing,
                current_decision_status=predecessor.lifecycle_status,
                current_decision_id=predecessor.id,
            )
        if predecessor.lifecycle_status in {
            EVOLUTION_DECISION_APPLIED,
            EVOLUTION_DECISION_CANCELLED,
        }:
            return CanonicalEvolutionResult(
                "REJECTED",
                "command_already_applied"
                if predecessor.lifecycle_status == EVOLUTION_DECISION_APPLIED
                else "command_cancelled",
                existing,
            )
        if predecessor.lifecycle_status == EVOLUTION_DECISION_PENDING and (
            command.expected_precondition_fingerprint != actual_precondition
        ):
            return CanonicalEvolutionResult(
                "STALE_OPERATION",
                "precondition_changed",
                existing,
                current_decision_status=predecessor.lifecycle_status,
                current_decision_id=predecessor.id,
            )
        if predecessor.lifecycle_status == EVOLUTION_DECISION_PENDING:
            if isinstance(command, CanonicalMergeCommand):
                try:
                    command = await _evaluate_merge_projection_payload(db, command)
                except CanonicalEvolutionError as exc:
                    return CanonicalEvolutionResult(
                        "REJECTED",
                        str(exc),
                        existing,
                        current_decision_status=predecessor.lifecycle_status,
                        current_decision_id=predecessor.id,
                    )
            elif isinstance(command, CanonicalSplitCommand):
                partition_error = await _split_partition_error(db, command)
                if partition_error is not None:
                    return CanonicalEvolutionResult(
                        "REJECTED",
                        partition_error,
                        existing,
                        current_decision_status=predecessor.lifecycle_status,
                        current_decision_id=predecessor.id,
                    )
            admission_error = await _current_identity_admission_error(
                db, command, pending_predecessor_id=predecessor.id
            )
            if admission_error is not None:
                return CanonicalEvolutionResult(
                    "STALE_OPERATION",
                    admission_error,
                    existing,
                    current_decision_status=predecessor.lifecycle_status,
                    current_decision_id=predecessor.id,
                )
            if await _graph_governance_projection_intent_is_occupied(db, command):
                return CanonicalEvolutionResult(
                    "STALE_OPERATION",
                    "graph_governance_projection_intent_occupied",
                    existing,
                    current_decision_status=predecessor.lifecycle_status,
                    current_decision_id=predecessor.id,
                )
        root = existing
        if predecessor.lifecycle_status == EVOLUTION_DECISION_PENDING:
            previous_sources, previous_assignments = await _supersede_pending_proposal(
                db, predecessor
            )
    else:
        if isinstance(command, CanonicalMergeCommand):
            try:
                command = await _evaluate_merge_projection_payload(db, command)
            except CanonicalEvolutionError as exc:
                return CanonicalEvolutionResult("REJECTED", str(exc))
        actual_precondition = await build_evolution_precondition_fingerprint(db, command)
        if await _new_root_slot_is_occupied(db, command):
            return CanonicalEvolutionResult(
                "STALE_OPERATION", "source_or_projection_slot_occupied"
            )
        admission_error = await _current_identity_admission_error(db, command)
        if admission_error is not None:
            return CanonicalEvolutionResult("STALE_OPERATION", admission_error)
        if await _graph_governance_projection_intent_is_occupied(db, command):
            return CanonicalEvolutionResult(
                "STALE_OPERATION", "graph_governance_projection_intent_occupied"
            )
        root = await _new_root(db, command, command_identity_fingerprint)
        if command.expected_precondition_fingerprint != actual_precondition:
            return await _append_terminal(
                db,
                root,
                command,
                EVOLUTION_DECISION_STALE,
                actual_precondition,
                "precondition_changed",
            )
    try:
        if isinstance(command, CanonicalMergeCommand):
            result = await _apply_merge(
                db,
                root,
                command,
                actual_precondition,
                supersedes_decision_id=predecessor.id if predecessor is not None else None,
                previous_sources=previous_sources,
                previous_assignments=previous_assignments,
            )
        elif isinstance(command, CanonicalSplitCommand):
            result = await _apply_split(
                db,
                root,
                command,
                actual_precondition,
                supersedes_decision_id=predecessor.id if predecessor is not None else None,
                previous_sources=previous_sources,
                previous_assignments=previous_assignments,
            )
        else:
            result = await _apply_reassign(
                db,
                root,
                command,
                actual_precondition,
                supersedes_decision_id=predecessor.id if predecessor is not None else None,
                previous_assignments=previous_assignments,
            )
    except IntegrityError:
        return CanonicalEvolutionResult("RETRYABLE_CONFLICT", "integrity_conflict", root)
    if predecessor is not None and result.decision is not None:
        predecessor.lifecycle_status = EVOLUTION_DECISION_SUPERSEDED
        await db.flush()
    return result


async def _supersede_pending_proposal(
    db, pending: CanonicalEntityEvolutionDecision
) -> tuple[
    dict[uuid.UUID, CanonicalEntityEvolutionSource],
    dict[uuid.UUID, CanonicalEntityProjectionAssignment],
]:
    sources = {
        row.source_canonical_entity_id: row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, pending.library_id, for_update=True)
        if row.evolution_decision_id == pending.id and row.resolution_state == EVOLUTION_SOURCE_PENDING
    }
    assignments = {
        row.entity_id: row
        for row in await _scoped_rows(
            db, CanonicalEntityProjectionAssignment, pending.library_id, for_update=True
        )
        if row.evolution_decision_id == pending.id
        and row.assignment_state in {EVOLUTION_ASSIGNMENT_PENDING, EVOLUTION_ASSIGNMENT_RESOLVED}
    }
    for source in sources.values():
        source.resolution_state = EVOLUTION_SOURCE_SUPERSEDED
    for assignment in assignments.values():
        assignment.assignment_state = EVOLUTION_ASSIGNMENT_SUPERSEDED
    await db.flush()
    return sources, assignments


async def complete_pending_canonical_evolution(
    db, command: EvolutionCommand
) -> CanonicalEvolutionResult:
    """Compatibility entry point for a same-root correction with an explicit CAS token."""

    return await apply_canonical_evolution(db, command)


def _validate_cancellation(cancellation: CanonicalEvolutionCancellation) -> str | None:
    if not isinstance(cancellation.command_id, uuid.UUID):
        return "command_id_invalid"
    if not isinstance(cancellation.expected_pending_decision_id, uuid.UUID):
        return "expected_predecessor_invalid"
    if (
        not isinstance(cancellation.original_idempotency_key, str)
        or not cancellation.original_idempotency_key
        or len(cancellation.original_idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        return "idempotency_key_invalid"
    if not isinstance(cancellation.reason_code, str) or not cancellation.reason_code.strip():
        return "reason_invalid"
    if not isinstance(cancellation.reason_text, str) or not cancellation.reason_text.strip():
        return "reason_invalid"
    if cancellation.actor_type not in {"user", "service"} or not isinstance(cancellation.actor_id, uuid.UUID):
        return "actor_invalid"
    if not isinstance(cancellation.request_id, str) or not cancellation.request_id.strip():
        return "request_id_invalid"
    return None


async def _command_by_id(
    db, library_id: uuid.UUID, command_id: uuid.UUID
) -> CanonicalEntityEvolutionCommand | None:
    rows = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionCommand, library_id)
        if row.id == command_id
    ]
    return rows[0] if len(rows) == 1 else None


async def _lock_pending_command_scopes(
    db, root: CanonicalEntityEvolutionCommand, pending: CanonicalEntityEvolutionDecision
) -> None:
    sources = [
        row
        for row in await _scoped_rows(db, CanonicalEntityEvolutionSource, root.library_id)
        if row.evolution_decision_id == pending.id
        and row.resolution_state == EVOLUTION_SOURCE_PENDING
    ]
    assignments = [
        row
        for row in await _scoped_rows(db, CanonicalEntityProjectionAssignment, root.library_id)
        if row.evolution_decision_id == pending.id
        and row.assignment_state in {EVOLUTION_ASSIGNMENT_PENDING, EVOLUTION_ASSIGNMENT_RESOLVED}
    ]
    entities = {
        row.id: row
        for row in await _scoped_rows(db, Entity, root.library_id)
        if row.id in {assignment.entity_id for assignment in assignments}
    }
    active = [
        row
        for row in await _scoped_rows(db, EntityResolutionDecision, root.library_id)
        if row.entity_id in entities and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    ]
    scopes = [
        *(GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, row.source_canonical_entity_id) for row in sources),
        *(GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, entity_id) for entity_id in entities),
        *(
            GraphIdentityLockScope(ENTITY_RESOLUTION_SUBJECT_LOCK_SCOPE, row.subject_fingerprint)
            for row in active
        ),
    ]
    await lock_graph_identity_scopes(db, root.library_id, scopes)


async def cancel_pending_canonical_evolution(
    db,
    library_id: uuid.UUID,
    cancellation: CanonicalEvolutionCancellation,
) -> CanonicalEvolutionResult:
    """Append a cancellation Decision and release only its own pending proposal."""

    validation_error = _validate_cancellation(cancellation)
    if validation_error is not None:
        return CanonicalEvolutionResult("REJECTED", validation_error)
    root = await _command_by_id(db, library_id, cancellation.command_id)
    if root is None:
        return CanonicalEvolutionResult("REJECTED", "command_not_found")
    if not hmac.compare_digest(root.idempotency_key, cancellation.original_idempotency_key):
        return CanonicalEvolutionResult("REJECTED", "cancellation_root_key_mismatch")
    decisions = await _decision_rows(db, library_id, root.id)
    head = _current_decision_head(decisions)
    if head is None:
        return CanonicalEvolutionResult("REJECTED", "command_unusable", root)
    await _lock_pending_command_scopes(db, root, head)
    root = await _command_by_id(db, library_id, cancellation.command_id)
    if root is None:
        return CanonicalEvolutionResult("REJECTED", "command_not_found")
    decisions = await _decision_rows(db, library_id, root.id)
    expected_pending = next(
        (row for row in decisions if row.id == cancellation.expected_pending_decision_id),
        None,
    )
    expected_precondition = (
        expected_pending.observed_precondition_fingerprint
        if expected_pending is not None
        else "0" * 64
    )
    replay = await _replay_result(
        db,
        root,
        _cancellation_payload_fingerprint(cancellation, expected_precondition),
    )
    if replay is not None:
        return replay
    head = _current_decision_head(decisions)
    if head is None:
        return CanonicalEvolutionResult("REJECTED", "command_unusable", root)
    if head.id != cancellation.expected_pending_decision_id:
        return CanonicalEvolutionResult(
            "STALE_OPERATION",
            "expected_predecessor_mismatch",
            root,
            current_decision_status=head.lifecycle_status,
            current_decision_id=head.id,
        )
    if head.lifecycle_status != EVOLUTION_DECISION_PENDING:
        return CanonicalEvolutionResult("REJECTED", "no_pending_intent", root)
    observed = _fingerprint(
        {
            "command_id": root.id,
            "pending_decision_id": head.id,
            "pending_payload_fingerprint": head.decision_payload_fingerprint,
        }
    )
    _sources, _assignments = await _supersede_pending_proposal(db, head)
    decision = _new_cancellation_decision(
        root, cancellation, head.observed_precondition_fingerprint, observed
    )
    db.add(decision)
    head.lifecycle_status = EVOLUTION_DECISION_SUPERSEDED
    await db.flush()
    return CanonicalEvolutionResult(
        "CANCELLED",
        "cancelled_pending_intent",
        root,
        decision,
        current_decision_status=decision.lifecycle_status,
        current_decision_id=decision.id,
    )


__all__ = [
    "CanonicalEvolutionCancellation",
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
    "cancel_pending_canonical_evolution",
    "complete_pending_canonical_evolution",
    "resolve_current_canonical_identity",
]
