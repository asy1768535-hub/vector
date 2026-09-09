"""Persisted current-LogicalFact lineage resolution for P3.3."""

from __future__ import annotations

import hashlib
import hmac
import math
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import rfc8785
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.canonical_entity import CanonicalEntity
from app.models.fact_foundation import (
    FactAssertion,
    FactResolutionDecision,
    LogicalFact,
    StablePredicateIdentity,
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
from app.services.canonical_entity_evolution import resolve_current_canonical_identity
from app.services.graph_identity_locks import (
    CANONICAL_ENTITY_LOCK_SCOPE,
    LOGICAL_FACT_LOCK_SCOPE,
    STABLE_PREDICATE_LOCK_SCOPE,
    GraphIdentityLockBusy,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)
from app.services.logical_fact_identity import logical_fact_identity_fingerprint_v1
from app.services.stable_predicate_evolution import resolve_current_stable_predicate_identity


@dataclass(frozen=True, slots=True)
class CurrentLogicalFactResult:
    status: str
    historical_logical_fact_id: uuid.UUID
    current_logical_fact_id: uuid.UUID | None = None
    current_target_slot_id: uuid.UUID | None = None
    reason_code: str | None = None


async def _rows(db: Any, model: type[Any], library_id: uuid.UUID) -> list[Any]:
    result = await db.execute(select(model).where(model.library_id == library_id))
    return [row for row in result.scalars().all() if row.library_id == library_id]


async def resolve_current_logical_fact(
    db: Any,
    library_id: uuid.UUID,
    logical_fact_id: uuid.UUID,
    *,
    fact_assertion_id: uuid.UUID | None = None,
    source_group_fingerprint: str | None = None,
) -> CurrentLogicalFactResult:
    """Resolve a current Fact using persisted reconciliation rows only."""

    if not isinstance(library_id, uuid.UUID) or not isinstance(logical_fact_id, uuid.UUID):
        raise TypeError("library_id and logical_fact_id must be UUIDs")
    if fact_assertion_id is not None and not isinstance(fact_assertion_id, uuid.UUID):
        raise TypeError("fact_assertion_id must be a UUID")
    if source_group_fingerprint is not None and (
        len(source_group_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in source_group_fingerprint)
    ):
        raise ValueError("source_group_fingerprint must be lowercase SHA-256")

    facts = {row.id: row for row in await _rows(db, LogicalFact, library_id)}
    if logical_fact_id not in facts:
        return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="logical_fact_scope_mismatch")
    decisions = {row.id: row for row in await _rows(db, FactReconciliationDecision, library_id)}
    sources = await _rows(db, FactReconciliationSource, library_id)
    slots = {row.id: row for row in await _rows(db, FactReconciliationTargetSlot, library_id)}
    edges = await _rows(db, FactReconciliationSourceTargetEdge, library_id)
    assignments = await _rows(db, FactReconciliationAssertionAssignment, library_id)

    current = logical_fact_id
    target_slot_id: uuid.UUID | None = None
    visited: set[uuid.UUID] = set()
    while True:
        if current in visited:
            return CurrentLogicalFactResult(
                "pending", logical_fact_id, reason_code="lineage_integrity"
            )
        visited.add(current)
        fact_sources = [row for row in sources if row.source_logical_fact_id == current]
        live_sources = [
            row
            for row in fact_sources
            if row.resolution_state in {"pending", "applied"}
            and (decision := decisions.get(row.evolution_decision_id)) is not None
            and decision.lifecycle_status in {"pending", "applied"}
        ]
        if not live_sources:
            if any(row.resolution_state == "historical_only" for row in fact_sources):
                return CurrentLogicalFactResult("historical_only", logical_fact_id)
            if fact_sources:
                return CurrentLogicalFactResult(
                    "pending", logical_fact_id, reason_code="lineage_integrity"
                )
            return CurrentLogicalFactResult(
                "resolved", logical_fact_id, current, target_slot_id
            )
        if len(live_sources) != 1:
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="lineage_integrity")
        source = live_sources[0]
        if source.resolution_state == "pending":
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="source_pending")
        source_assignments = [
            row
            for row in assignments
            if row.source_transition_id == source.id and row.assignment_state != "superseded"
        ]
        chosen_assignment = None
        if fact_assertion_id is not None:
            matches = [row for row in source_assignments if row.fact_assertion_id == fact_assertion_id]
            if len(matches) != 1:
                return CurrentLogicalFactResult(
                    "pending", logical_fact_id, reason_code="assignment_context_missing"
                )
            chosen_assignment = matches[0]
        elif source_group_fingerprint is not None:
            matches = [
                row for row in source_assignments if row.source_group_fingerprint == source_group_fingerprint
            ]
            if len(matches) != 1:
                return CurrentLogicalFactResult(
                    "pending", logical_fact_id, reason_code="assignment_context_missing"
                )
            chosen_assignment = matches[0]
        if chosen_assignment is not None and chosen_assignment.assignment_state == "pending":
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="assignment_pending")
        if chosen_assignment is None and any(
            row.assignment_state == "pending" for row in source_assignments
        ):
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="assignment_pending")

        source_edges = [
            row
            for row in edges
            if row.source_transition_id == source.id and row.edge_state == "applied"
        ]
        if chosen_assignment is not None:
            source_edges = [
                row for row in source_edges if row.id == chosen_assignment.source_target_edge_id
            ]
        if not source_edges:
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="lineage_integrity")
        if len(source_edges) > 1:
            return CurrentLogicalFactResult("forked", logical_fact_id, reason_code="split_context_required")
        edge = source_edges[0]
        slot = slots.get(edge.target_slot_id)
        if (
            slot is None
            or slot.slot_state != "applied"
            or slot.target_logical_fact_id is None
            or slot.target_logical_fact_id not in facts
        ):
            return CurrentLogicalFactResult("pending", logical_fact_id, reason_code="lineage_integrity")
        current = slot.target_logical_fact_id
        target_slot_id = slot.id


FACT_RECONCILIATION_CONTRACT_VERSION = "p3_3_fact_reconciliation/v1"
_COMMAND_SCHEMA = "p3_3_fact_reconciliation_command_v1"
_TARGET_UUID_PREFIX = "vector-kb:fact-reconciliation-target:v1:"
_MAX_IDEMPOTENCY_KEY_LENGTH = 256


class FactReconciliationError(ValueError):
    """A reconciliation request cannot be represented by the frozen contract."""


class FactReconciliationRetryableConflict(RuntimeError):
    """A concurrent graph writer invalidated this reconciliation attempt."""


@dataclass(frozen=True, slots=True)
class FactReconciliationTargetSpec:
    stable_predicate_identity_id: uuid.UUID
    subject_canonical_entity_id: uuid.UUID
    object_kind: str | None
    object_canonical_entity_id: uuid.UUID | None
    object_value: Mapping[str, Any] | None
    identity_qualifiers: Mapping[str, Any]
    temporal_identity_key: str | None
    identity_policy_version: str


@dataclass(frozen=True, slots=True)
class FactReconciliationTargetSlotInput:
    target_key: str
    target_ref_kind: str
    target_spec: FactReconciliationTargetSpec
    existing_logical_fact_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class FactReconciliationTopology:
    source_logical_fact_id: uuid.UUID
    target_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactReconciliationAssignmentInput:
    source_logical_fact_id: uuid.UUID
    fact_assertion_id: uuid.UUID
    state: str
    target_key: str | None
    reason_code: str = "reconciliation_partition"


@dataclass(frozen=True, slots=True)
class FactReconciliationCommand:
    library_id: uuid.UUID
    source_logical_fact_ids: tuple[uuid.UUID, ...]
    source_target_topology: tuple[FactReconciliationTopology, ...]
    target_slots: tuple[FactReconciliationTargetSlotInput, ...]
    assignments: tuple[FactReconciliationAssignmentInput, ...]
    requested_effect: str
    idempotency_key: str
    reason_code: str
    reason_text: str
    method: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    actor_type: str
    actor_id: str
    request_id: str
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class FactReconciliationCancellation:
    command_id: uuid.UUID
    original_idempotency_key: str
    expected_pending_decision_id: uuid.UUID
    reason_code: str
    reason_text: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    actor_type: str
    actor_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class FactReconciliationResult:
    status: str
    reason_code: str | None = None
    command: FactReconciliationCommandRow | None = None
    decision: FactReconciliationDecision | None = None
    source_transitions: tuple[FactReconciliationSource, ...] = ()
    target_slots: tuple[FactReconciliationTargetSlot, ...] = ()
    edges: tuple[FactReconciliationSourceTargetEdge, ...] = ()
    assignments: tuple[FactReconciliationAssertionAssignment, ...] = ()
    reused_decision_id: uuid.UUID | None = None
    effective_outcome: str | None = None
    current_decision_id: uuid.UUID | None = None
    current_decision_status: str | None = None


@dataclass(frozen=True, slots=True)
class _TargetPlan:
    input: FactReconciliationTargetSlotInput
    snapshot: dict[str, Any]
    identity_fingerprint: str
    planned_id: uuid.UUID | None


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise FactReconciliationError("reconciliation strings must be NFC")
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise FactReconciliationError("reconciliation integer is outside I-JSON range")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or (value.is_zero() and value.is_signed()):
            raise FactReconciliationError("reconciliation decimal is invalid")
        return _stable_value(int(value)) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0 and math.copysign(1, value) < 0):
            raise FactReconciliationError("reconciliation number is invalid")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise FactReconciliationError("reconciliation object keys must be strings")
        return {key: _stable_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    raise FactReconciliationError(f"unsupported reconciliation value: {type(value).__name__}")


def _jcs(value: Mapping[str, Any]) -> bytes:
    try:
        return rfc8785.dumps(_stable_value(value))
    except rfc8785.CanonicalizationError as exc:
        raise FactReconciliationError("reconciliation value is not RFC 8785 JSON") from exc


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_jcs(value)).hexdigest()


def _fingerprint_is_valid(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _target_spec_snapshot(spec: FactReconciliationTargetSpec) -> dict[str, Any]:
    return {
        "identity_policy_version": spec.identity_policy_version,
        "identity_qualifiers": dict(spec.identity_qualifiers),
        "object_canonical_entity_id": spec.object_canonical_entity_id,
        "object_kind": spec.object_kind,
        "object_value": dict(spec.object_value) if spec.object_value is not None else None,
        "stable_predicate_identity_id": spec.stable_predicate_identity_id,
        "subject_canonical_entity_id": spec.subject_canonical_entity_id,
        "temporal_identity_key": spec.temporal_identity_key,
    }


def _target_ref(slot: FactReconciliationTargetSlotInput) -> dict[str, Any]:
    if slot.target_ref_kind == "existing" and isinstance(slot.existing_logical_fact_id, uuid.UUID):
        return {"kind": "existing", "logical_fact_id": slot.existing_logical_fact_id}
    if slot.target_ref_kind == "new" and slot.existing_logical_fact_id is None:
        return {"kind": "new"}
    raise FactReconciliationError("target reference is not an exact closed shape")


def _normalized_topology(command: FactReconciliationCommand) -> list[dict[str, Any]]:
    rows = []
    for entry in command.source_target_topology:
        if not isinstance(entry.source_logical_fact_id, uuid.UUID):
            raise FactReconciliationError("topology source must be a UUID")
        keys = tuple(entry.target_keys)
        if not keys or len(set(keys)) != len(keys):
            raise FactReconciliationError("topology target keys must be distinct and nonempty")
        rows.append({"source_logical_fact_id": entry.source_logical_fact_id, "target_keys": sorted(keys, key=lambda item: item.encode("utf-8"))})
    rows.sort(key=lambda row: str(row["source_logical_fact_id"]))
    return rows


def _command_identity_payload(command: FactReconciliationCommand) -> dict[str, Any]:
    slots = []
    for slot in command.target_slots:
        if not isinstance(slot.target_key, str) or not slot.target_key:
            raise FactReconciliationError("target key is required")
        slots.append(
            {
                "target_key": slot.target_key,
                "target_ref": _target_ref(slot),
                "target_spec": _target_spec_snapshot(slot.target_spec),
            }
        )
    if len(slots) != len({slot["target_key"] for slot in slots}):
        raise FactReconciliationError("target keys must be distinct")
    source_ids = tuple(command.source_logical_fact_ids)
    if not source_ids or len(set(source_ids)) != len(source_ids) or not all(
        isinstance(item, uuid.UUID) for item in source_ids
    ):
        raise FactReconciliationError("source logical facts must be distinct UUIDs")
    topology = _normalized_topology(command)
    if {row["source_logical_fact_id"] for row in topology} != set(source_ids):
        raise FactReconciliationError("topology source set must equal command source set")
    slot_keys = {slot["target_key"] for slot in slots}
    if {key for row in topology for key in row["target_keys"]} != slot_keys:
        raise FactReconciliationError("topology target keys must equal command target slots")
    return {
        "library_id": command.library_id,
        "operation": "reconcile",
        "schema": _COMMAND_SCHEMA,
        "source_logical_fact_ids": sorted(source_ids, key=str),
        "source_target_topology": topology,
        "target_slots": sorted(slots, key=lambda row: row["target_key"].encode("utf-8")),
    }


def fact_reconciliation_command_json_bytes(command: FactReconciliationCommand) -> bytes:
    return _jcs(_command_identity_payload(command))


def fact_reconciliation_command_fingerprint(command: FactReconciliationCommand) -> str:
    return hashlib.sha256(fact_reconciliation_command_json_bytes(command)).hexdigest()


def planned_target_logical_fact_id(
    command_identity_fingerprint: str, target_key: str
) -> uuid.UUID:
    if not _fingerprint_is_valid(command_identity_fingerprint):
        raise FactReconciliationError("command identity fingerprint is invalid")
    if not isinstance(target_key, str) or not target_key or unicodedata.normalize("NFC", target_key) != target_key:
        raise FactReconciliationError("target key must be a nonempty NFC string")
    name = f"{_TARGET_UUID_PREFIX}{command_identity_fingerprint}:{target_key}"
    return uuid.uuid5(uuid.NAMESPACE_URL, name)


async def _locked_rows(db: Any, model: type[Any], library_id: uuid.UUID) -> list[Any]:
    result = await db.execute(
        select(model)
        .where(model.library_id == library_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return [row for row in result.scalars().all() if row.library_id == library_id]


async def _command_by_key(
    db: Any,
    library_id: uuid.UUID,
    idempotency_key: str,
    *,
    for_update: bool = True,
) -> FactReconciliationCommandRow | None:
    read_rows = _locked_rows if for_update else _rows
    rows = [
        row
        for row in await read_rows(db, FactReconciliationCommandRow, library_id)
        if row.idempotency_key == idempotency_key
    ]
    if len(rows) > 1:
        raise FactReconciliationRetryableConflict("idempotency_root_integrity")
    return rows[0] if rows else None


async def _command_by_identity(
    db: Any,
    library_id: uuid.UUID,
    fingerprint: str,
    *,
    for_update: bool = True,
) -> FactReconciliationCommandRow | None:
    read_rows = _locked_rows if for_update else _rows
    rows = [
        row
        for row in await read_rows(db, FactReconciliationCommandRow, library_id)
        if row.command_identity_fingerprint == fingerprint
    ]
    if len(rows) > 1:
        raise FactReconciliationRetryableConflict("command_identity_integrity")
    return rows[0] if rows else None


async def _decision_rows(
    db: Any,
    library_id: uuid.UUID,
    command_id: uuid.UUID,
    *,
    for_update: bool = True,
) -> list[FactReconciliationDecision]:
    read_rows = _locked_rows if for_update else _rows
    return [
        row
        for row in await read_rows(db, FactReconciliationDecision, library_id)
        if row.command_id == command_id
    ]


def _current_decision(rows: list[FactReconciliationDecision]) -> FactReconciliationDecision | None:
    superseded = {row.supersedes_decision_id for row in rows if row.supersedes_decision_id is not None}
    heads = [row for row in rows if row.id not in superseded]
    return heads[0] if len(heads) == 1 else None


def _replay_outcome(value: str) -> str:
    return "STALE" if value == "stale" else value.upper()


def _normalized_evidence_refs(
    evidence_refs: tuple[Mapping[str, Any], ...]
) -> list[dict[str, Any]]:
    refs = [dict(item) for item in evidence_refs]
    encoded = [_jcs(item) for item in refs]
    if any(not item for item in refs) or len(set(encoded)) != len(encoded):
        raise FactReconciliationError("evidence references must be distinct nonempty objects")
    return [item for _, item in sorted(zip(encoded, refs, strict=True), key=lambda pair: pair[0])]


def _normal_assignment_payload(
    command: FactReconciliationCommand,
    assignments: Mapping[uuid.UUID, tuple[str, dict[str, Any], FactReconciliationAssignmentInput]],
) -> dict[str, Any]:
    rows = []
    for assertion_id, (group, basis, entry) in assignments.items():
        rows.append(
            {
                "fact_assertion_id": assertion_id,
                "partition_basis_snapshot": basis,
                "reason_code": entry.reason_code,
                "source_group_fingerprint": group,
                "source_logical_fact_id": entry.source_logical_fact_id,
                "state": entry.state,
                "target_key": entry.target_key,
            }
        )
    rows.sort(key=lambda item: (str(item["source_logical_fact_id"]), str(item["fact_assertion_id"])))
    return {"assertion_assignments": rows}


def _decision_fingerprint(
    command: FactReconciliationCommand, operation_payload: Mapping[str, Any]
) -> str:
    if not _fingerprint_is_valid(command.expected_precondition_fingerprint):
        raise FactReconciliationError("expected precondition fingerprint is invalid")
    return _sha256(
        {
            "confidence": command.confidence,
            "evidence_refs": _normalized_evidence_refs(command.evidence_refs),
            "expected_precondition_fingerprint": command.expected_precondition_fingerprint,
            "method": command.method,
            "operation_payload": dict(operation_payload),
            "reason_code": command.reason_code,
            "reason_text": command.reason_text,
            "requested_effect": command.requested_effect,
        }
    )


async def _replay(
    db: Any, root: FactReconciliationCommandRow, command: FactReconciliationCommand
) -> FactReconciliationResult | None:
    rows = await _decision_rows(db, root.library_id, root.id)
    head = _current_decision(rows)
    for decision in rows:
        if decision.requested_effect == "cancel":
            continue
        try:
            fingerprint = _decision_fingerprint(command, decision.operation_payload_snapshot)
        except FactReconciliationError:
            return None
        if hmac.compare_digest(decision.decision_payload_fingerprint, fingerprint):
            return FactReconciliationResult(
                "REUSED",
                command=root,
                decision=decision,
                reused_decision_id=decision.id,
                effective_outcome=_replay_outcome(decision.evaluated_outcome),
                current_decision_id=head.id if head else None,
                current_decision_status=head.lifecycle_status if head else None,
            )
    return None


def _validate_command(command: FactReconciliationCommand) -> str | None:
    if not isinstance(command.library_id, uuid.UUID):
        return "library_id_invalid"
    if command.requested_effect not in {"stage", "apply"}:
        return "requested_effect_invalid"
    if not isinstance(command.idempotency_key, str) or not command.idempotency_key or len(command.idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        return "idempotency_key_invalid"
    if not _fingerprint_is_valid(command.expected_precondition_fingerprint):
        return "expected_precondition_invalid"
    if not isinstance(command.expected_predecessor_decision_id, (uuid.UUID, type(None))):
        return "expected_predecessor_invalid"
    if not all(isinstance(value, str) and value for value in (command.reason_code, command.reason_text, command.method, command.actor_type, command.actor_id, command.request_id)):
        return "audit_metadata_invalid"
    if command.confidence is not None and not (
        isinstance(command.confidence, Decimal) and Decimal(0) <= command.confidence <= Decimal(1)
    ):
        return "confidence_invalid"
    return None


async def _target_plans(
    db: Any,
    command: FactReconciliationCommand,
    command_fingerprint: str,
) -> tuple[tuple[_TargetPlan, ...], str | None]:
    plans: list[_TargetPlan] = []
    for slot in command.target_slots:
        snapshot = _target_spec_snapshot(slot.target_spec)
        predicate = await db.get(StablePredicateIdentity, slot.target_spec.stable_predicate_identity_id)
        if predicate is None or predicate.library_id != command.library_id:
            return (), "target_predicate_scope_mismatch"
        current_predicate = await resolve_current_stable_predicate_identity(
            db, command.library_id, predicate.id
        )
        if (
            current_predicate.status != "resolved"
            or current_predicate.current_predicate_id != predicate.id
            or predicate.resolution_status != "resolved"
        ):
            return (), "target_predicate_not_current"
        for canonical_id in (
            slot.target_spec.subject_canonical_entity_id,
            slot.target_spec.object_canonical_entity_id,
        ):
            if canonical_id is None:
                continue
            canonical = await db.get(CanonicalEntity, canonical_id)
            current_canonical = await resolve_current_canonical_identity(
                db, command.library_id, canonical_id
            )
            if (
                canonical is None
                or canonical.library_id != command.library_id
                or current_canonical.status != "resolved"
                or current_canonical.current_canonical_entity_id != canonical_id
                or not current_canonical.resolution_eligible
            ):
                return (), "target_canonical_not_current"
        if slot.target_spec.identity_policy_version != predicate.identity_policy_version:
            return (), "target_identity_policy_mismatch"
        fingerprint = logical_fact_identity_fingerprint_v1(
            library_id=command.library_id,
            predicate=predicate,
            subject_canonical_entity_id=slot.target_spec.subject_canonical_entity_id,
            object_kind=slot.target_spec.object_kind,
            object_canonical_entity_id=slot.target_spec.object_canonical_entity_id,
            object_value=(dict(slot.target_spec.object_value) if slot.target_spec.object_value else None),
            identity_qualifiers=slot.target_spec.identity_qualifiers,
            temporal_identity_key=slot.target_spec.temporal_identity_key,
        )
        if slot.target_ref_kind == "existing":
            existing = await db.get(LogicalFact, slot.existing_logical_fact_id)
            if existing is None or existing.library_id != command.library_id:
                return (), "existing_target_scope_mismatch"
            if (
                existing.identity_fingerprint != fingerprint
                or existing.stable_predicate_identity_id
                != slot.target_spec.stable_predicate_identity_id
                or existing.subject_canonical_entity_id
                != slot.target_spec.subject_canonical_entity_id
                or existing.object_kind != slot.target_spec.object_kind
                or existing.object_canonical_entity_id
                != slot.target_spec.object_canonical_entity_id
                or existing.object_value
                != (dict(slot.target_spec.object_value) if slot.target_spec.object_value else None)
                or existing.identity_qualifiers != dict(slot.target_spec.identity_qualifiers)
                or existing.temporal_identity_key != slot.target_spec.temporal_identity_key
                or existing.identity_policy_version != slot.target_spec.identity_policy_version
            ):
                return (), "existing_target_identity_mismatch"
            planned = None
        elif slot.target_ref_kind == "new" and slot.existing_logical_fact_id is None:
            planned = planned_target_logical_fact_id(command_fingerprint, slot.target_key)
        else:
            return (), "target_ref_invalid"
        plans.append(_TargetPlan(slot, snapshot, fingerprint, planned))
    if len(plans) != len({plan.input.target_key for plan in plans}):
        return (), "target_key_duplicate"
    if len(plans) != len({plan.identity_fingerprint for plan in plans}):
        return (), "target_identity_duplicate"
    return tuple(plans), None


async def _current_membership(
    db: Any,
    library_id: uuid.UUID,
    source_ids: set[uuid.UUID],
    *,
    for_update: bool = True,
) -> dict[uuid.UUID, list[FactAssertion]]:
    read_rows = _locked_rows if for_update else _rows
    members = {source_id: [] for source_id in source_ids}
    for assertion in await read_rows(db, FactAssertion, library_id):
        current = await resolve_current_logical_fact(
            db, library_id, assertion.logical_fact_id, fact_assertion_id=assertion.id
        )
        if current.status == "resolved" and current.current_logical_fact_id in members:
            members[current.current_logical_fact_id].append(assertion)
        elif current.status == "pending" and assertion.logical_fact_id in members:
            members[assertion.logical_fact_id].append(assertion)
    return members


def _occurrence_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    keys = {
        "schema_version",
        "library_id",
        "document_id",
        "document_revision_id",
        "evidence_id",
        "chunk_id",
        "block_id",
        "source_span",
    }
    value = {key: snapshot.get(key) for key in keys}
    if set(value) != keys or value["schema_version"] != "graph_relation_source_occurrence_v1":
        return None
    if not all(isinstance(value[key], str) for key in keys - {"block_id", "source_span"}):
        return None
    if value["block_id"] is not None and not isinstance(value["block_id"], str):
        return None
    if not isinstance(value["source_span"], Mapping):
        return None
    return value


async def _source_group(
    db: Any,
    library_id: uuid.UUID,
    assertion: FactAssertion,
    *,
    for_update: bool = True,
) -> tuple[str, dict[str, Any], str | None]:
    read_rows = _locked_rows if for_update else _rows
    decisions = [
        row
        for row in await read_rows(db, FactResolutionDecision, library_id)
        if row.fact_assertion_id == assertion.id
    ]
    if decisions:
        if len(decisions) != 1:
            return "", {}, "source_group_fact_resolution_decision_ambiguous"
        decision = decisions[0]
        occurrence = _occurrence_snapshot(decision.source_snapshot)
        if decision.status not in {"resolved", "superseded"} or occurrence is None:
            return "", {}, "source_group_snapshot_invalid"
        return _sha256(occurrence), occurrence, None
    evidence_rows = [
        row
        for row in await read_rows(db, RelationEvidence, library_id)
        if row.fact_assertion_id == assertion.id
    ]
    if not evidence_rows:
        return "", {}, "source_group_missing_occurrence"
    occurrences: list[dict[str, Any]] = []
    for evidence in evidence_rows:
        if not all(getattr(evidence, key, None) is not None for key in ("evidence_id", "document_id", "document_revision_id", "chunk_id", "source_span")) or not isinstance(evidence.source_span, Mapping):
            return "", {}, "source_group_relation_evidence_invalid"
        occurrences.append(
            {
                "block_id": None,
                "chunk_id": str(evidence.chunk_id),
                "document_id": str(evidence.document_id),
                "document_revision_id": str(evidence.document_revision_id),
                "evidence_id": str(evidence.evidence_id),
                "library_id": str(library_id),
                "schema_version": "graph_relation_source_occurrence_v1",
                "source_span": dict(evidence.source_span),
            }
        )
    encoded = [_jcs(value) for value in occurrences]
    if len(set(encoded)) != len(encoded):
        return "", {}, "source_group_relation_evidence_invalid"
    occurrences = [value for _, value in sorted(zip(encoded, occurrences, strict=True), key=lambda pair: pair[0])]
    basis = {"library_id": str(library_id), "occurrences": occurrences, "schema_version": "p3_3_relation_evidence_group_v1"}
    return _sha256(basis), basis, None


async def _lock_reconciliation_scopes(
    db: Any,
    *,
    command: FactReconciliationCommand,
    target_plans: tuple[_TargetPlan, ...],
) -> None:
    scopes: list[GraphIdentityLockScope] = []
    source_ids = set(command.source_logical_fact_ids)
    source_rows = {row.id: row for row in await _rows(db, LogicalFact, command.library_id)}
    if not source_ids <= set(source_rows):
        raise FactReconciliationRetryableConflict("source_scope_mismatch")
    for fact in source_rows.values():
        if fact.id not in source_ids:
            continue
        scopes.extend(
            (
                GraphIdentityLockScope(STABLE_PREDICATE_LOCK_SCOPE, fact.stable_predicate_identity_id),
                GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, fact.subject_canonical_entity_id),
                GraphIdentityLockScope(LOGICAL_FACT_LOCK_SCOPE, fact.identity_fingerprint),
            )
        )
        if fact.object_canonical_entity_id is not None:
            scopes.append(GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, fact.object_canonical_entity_id))
    for plan in target_plans:
        spec = plan.input.target_spec
        scopes.extend(
            (
                GraphIdentityLockScope(STABLE_PREDICATE_LOCK_SCOPE, spec.stable_predicate_identity_id),
                GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, spec.subject_canonical_entity_id),
                GraphIdentityLockScope(LOGICAL_FACT_LOCK_SCOPE, plan.identity_fingerprint),
            )
        )
        if spec.object_canonical_entity_id is not None:
            scopes.append(GraphIdentityLockScope(CANONICAL_ENTITY_LOCK_SCOPE, spec.object_canonical_entity_id))
    await lock_graph_identity_scopes(db, command.library_id, scopes, wait=False)


async def _precondition_fingerprint(
    db: Any,
    *,
    command: FactReconciliationCommand,
    target_plans: tuple[_TargetPlan, ...],
    membership: Mapping[uuid.UUID, list[FactAssertion]],
    for_update: bool = True,
) -> str:
    read_rows = _locked_rows if for_update else _rows
    current_sources = []
    for source_id in sorted(command.source_logical_fact_ids, key=str):
        current = await resolve_current_logical_fact(db, command.library_id, source_id)
        current_sources.append(
            {
                "logical_fact_id": source_id,
                "status": current.status,
                "current_logical_fact_id": current.current_logical_fact_id,
                "reason_code": current.reason_code,
            }
        )
    groups = []
    for source_id, assertions in membership.items():
        for assertion in assertions:
            group, basis, error = await _source_group(
                db, command.library_id, assertion, for_update=for_update
            )
            groups.append(
                {
                    "source_logical_fact_id": source_id,
                    "fact_assertion_id": assertion.id,
                    "source_group_fingerprint": group if error is None else None,
                    "partition_basis_snapshot": basis if error is None else None,
                    "reason_code": error,
                }
            )
    targets = []
    facts = await read_rows(db, LogicalFact, command.library_id)
    by_fingerprint: dict[str, list[LogicalFact]] = {}
    for fact in facts:
        by_fingerprint.setdefault(fact.identity_fingerprint, []).append(fact)
    for plan in target_plans:
        target = (
            await db.get(LogicalFact, plan.input.existing_logical_fact_id)
            if plan.input.target_ref_kind == "existing"
            else None
        )
        targets.append(
            {
                "target_key": plan.input.target_key,
                "target_identity_fingerprint": plan.identity_fingerprint,
                "planned_target_logical_fact_id": plan.planned_id,
                "existing_target_id": target.id if target is not None else None,
                "identity_rows": sorted(str(row.id) for row in by_fingerprint.get(plan.identity_fingerprint, [])),
            }
        )
    heads = []
    for root in await read_rows(db, FactReconciliationCommandRow, command.library_id):
        decision = _current_decision(
            await _decision_rows(db, command.library_id, root.id, for_update=for_update)
        )
        if decision is not None:
            heads.append({"command_id": root.id, "decision_id": decision.id, "status": decision.lifecycle_status})
    return _sha256(
        {
            "command_identity_fingerprint": fact_reconciliation_command_fingerprint(command),
            "sources": current_sources,
            "groups": sorted(groups, key=lambda row: (str(row["source_logical_fact_id"]), str(row["fact_assertion_id"]))),
            "targets": sorted(targets, key=lambda row: row["target_key"].encode("utf-8")),
            "heads": sorted(heads, key=lambda row: str(row["command_id"])),
        }
    )


async def build_fact_reconciliation_precondition_fingerprint(
    db: Any, command: FactReconciliationCommand
) -> str:
    """Build the opaque precondition token from persisted lineage without writing."""

    command_fingerprint = fact_reconciliation_command_fingerprint(command)
    target_plans, error = await _target_plans(db, command, command_fingerprint)
    if error is not None:
        raise FactReconciliationError(error)
    membership = await _current_membership(
        db, command.library_id, set(command.source_logical_fact_ids), for_update=False
    )
    return await _precondition_fingerprint(
        db,
        command=command,
        target_plans=target_plans,
        membership=membership,
        for_update=False,
    )


async def _prepared_assignments(
    db: Any,
    *,
    command: FactReconciliationCommand,
    membership: Mapping[uuid.UUID, list[FactAssertion]],
) -> tuple[
    dict[uuid.UUID, tuple[str, dict[str, Any], FactReconciliationAssignmentInput]], str | None
]:
    inputs = {entry.fact_assertion_id: entry for entry in command.assignments}
    expected = {assertion.id for assertions in membership.values() for assertion in assertions}
    if len(inputs) != len(command.assignments) or set(inputs) != expected:
        return {}, "assertion_partition_incomplete"
    allowed_targets = {
        entry.source_logical_fact_id: set(entry.target_keys)
        for entry in command.source_target_topology
    }
    prepared: dict[uuid.UUID, tuple[str, dict[str, Any], FactReconciliationAssignmentInput]] = {}
    group_targets: dict[tuple[uuid.UUID, str], str | None] = {}
    for source_id, assertions in membership.items():
        for assertion in assertions:
            entry = inputs[assertion.id]
            if entry.source_logical_fact_id != source_id or entry.state not in {"resolved", "pending"}:
                return {}, "assertion_partition_invalid"
            if entry.state == "resolved" and entry.target_key not in allowed_targets[source_id]:
                return {}, "assertion_target_not_allowed"
            if entry.state == "pending" and entry.target_key is not None:
                return {}, "pending_assignment_has_target"
            group, basis, error = await _source_group(db, command.library_id, assertion)
            if error is not None:
                return {}, error
            key = (source_id, group)
            previous_target = group_targets.setdefault(key, entry.target_key)
            if previous_target != entry.target_key:
                return {}, "source_group_target_ambiguous"
            prepared[assertion.id] = (group, basis, entry)
    return prepared, None


def _topology_kind(command: FactReconciliationCommand) -> str:
    source_count = len(command.source_logical_fact_ids)
    target_count = len(command.target_slots)
    if source_count == 1 and target_count == 1:
        return "1_to_1"
    if source_count > 1 and target_count == 1:
        return "n_to_1"
    if source_count == 1 and target_count > 1:
        return "1_to_n"
    return "n_to_m"


async def _admission_error(
    db: Any,
    *,
    command: FactReconciliationCommand,
    target_plans: tuple[_TargetPlan, ...],
    pending_predecessor: FactReconciliationDecision | None,
) -> str | None:
    source_ids = set(command.source_logical_fact_ids)
    facts = {row.id: row for row in await _locked_rows(db, LogicalFact, command.library_id)}
    for source_id in source_ids:
        current = await resolve_current_logical_fact(db, command.library_id, source_id)
        if current.status == "pending" and pending_predecessor is not None:
            continue
        if current.status != "resolved" or current.current_logical_fact_id != source_id:
            return "source_not_current_leaf"
    for plan in target_plans:
        if plan.input.target_ref_kind == "existing":
            assert plan.input.existing_logical_fact_id is not None
            if plan.input.existing_logical_fact_id in source_ids:
                return "source_equals_target"
            current = await resolve_current_logical_fact(
                db, command.library_id, plan.input.existing_logical_fact_id
            )
            if current.status != "resolved" or current.current_logical_fact_id != plan.input.existing_logical_fact_id:
                return "existing_target_not_current_leaf"
            continue
        if plan.planned_id in source_ids:
            return "source_equals_target"
        if any(row.identity_fingerprint == plan.identity_fingerprint for row in facts.values()):
            return "target_identity_already_materialized"
    return None


async def _root_slot_occupied(
    db: Any, command: FactReconciliationCommand
) -> bool:
    sources = await _locked_rows(db, FactReconciliationSource, command.library_id)
    return any(
        row.source_logical_fact_id in set(command.source_logical_fact_ids)
        and row.resolution_state in {"pending", "applied"}
        for row in sources
    )


def _new_root(command: FactReconciliationCommand, fingerprint: str) -> FactReconciliationCommandRow:
    return FactReconciliationCommandRow(
        id=uuid.uuid4(),
        library_id=command.library_id,
        idempotency_key=command.idempotency_key,
        command_identity_fingerprint=fingerprint,
        operation_kind="reconcile",
        command_payload_snapshot=_stable_value(_command_identity_payload(command)),
        contract_version=FACT_RECONCILIATION_CONTRACT_VERSION,
    )


def _new_decision(
    root: FactReconciliationCommandRow,
    command: FactReconciliationCommand,
    operation_payload: Mapping[str, Any],
    outcome: str,
    observed: str,
    predecessor: FactReconciliationDecision | None,
) -> FactReconciliationDecision:
    return FactReconciliationDecision(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        decision_payload_fingerprint=_decision_fingerprint(command, operation_payload),
        operation_kind="reconcile",
        requested_effect=command.requested_effect,
        evaluated_outcome=outcome,
        lifecycle_status=outcome,
        operation_payload_snapshot=_stable_value(dict(operation_payload)),
        expected_precondition_fingerprint=command.expected_precondition_fingerprint,
        observed_precondition_fingerprint=observed,
        reason_code=command.reason_code,
        reason_text=command.reason_text,
        method=command.method,
        confidence=command.confidence,
        evidence_refs=_stable_value(_normalized_evidence_refs(command.evidence_refs)),
        supersedes_decision_id=predecessor.id if predecessor is not None else None,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        request_id=command.request_id,
    )


async def _supersede_pending_proposal(
    db: Any, predecessor: FactReconciliationDecision
) -> tuple[
    dict[uuid.UUID, FactReconciliationSource],
    dict[str, FactReconciliationTargetSlot],
    dict[tuple[uuid.UUID, str], FactReconciliationSourceTargetEdge],
    dict[uuid.UUID, FactReconciliationAssertionAssignment],
]:
    sources = {
        row.source_logical_fact_id: row
        for row in await _locked_rows(db, FactReconciliationSource, predecessor.library_id)
        if row.evolution_decision_id == predecessor.id and row.resolution_state == "pending"
    }
    slots = {
        row.target_key: row
        for row in await _locked_rows(db, FactReconciliationTargetSlot, predecessor.library_id)
        if row.evolution_decision_id == predecessor.id and row.slot_state == "pending"
    }
    edges = {
        (row.source_logical_fact_id, row.target_key): row
        for row in await _locked_rows(db, FactReconciliationSourceTargetEdge, predecessor.library_id)
        if row.evolution_decision_id == predecessor.id and row.edge_state == "pending"
    }
    assignments = {
        row.fact_assertion_id: row
        for row in await _locked_rows(db, FactReconciliationAssertionAssignment, predecessor.library_id)
        if row.evolution_decision_id == predecessor.id
        and row.assignment_state in {"pending", "resolved"}
    }
    predecessor.lifecycle_status = "superseded"
    for row in sources.values():
        row.resolution_state = "superseded"
    for row in slots.values():
        row.slot_state = "superseded"
    for row in edges.values():
        row.edge_state = "superseded"
    for row in assignments.values():
        row.assignment_state = "superseded"
    await db.flush()
    return sources, slots, edges, assignments


async def _create_children(
    db: Any,
    *,
    root: FactReconciliationCommandRow,
    decision: FactReconciliationDecision,
    command: FactReconciliationCommand,
    target_plans: tuple[_TargetPlan, ...],
    assignments: Mapping[uuid.UUID, tuple[str, dict[str, Any], FactReconciliationAssignmentInput]],
    applied: bool,
    previous_sources: Mapping[uuid.UUID, FactReconciliationSource],
    previous_slots: Mapping[str, FactReconciliationTargetSlot],
    previous_edges: Mapping[tuple[uuid.UUID, str], FactReconciliationSourceTargetEdge],
    previous_assignments: Mapping[uuid.UUID, FactReconciliationAssertionAssignment],
) -> tuple[
    tuple[FactReconciliationSource, ...],
    tuple[FactReconciliationTargetSlot, ...],
    tuple[FactReconciliationSourceTargetEdge, ...],
    tuple[FactReconciliationAssertionAssignment, ...],
]:
    source_state = "applied" if applied else "pending"
    sources: dict[uuid.UUID, FactReconciliationSource] = {}
    for source_id in command.source_logical_fact_ids:
        source = FactReconciliationSource(
            id=uuid.uuid4(),
            library_id=root.library_id,
            command_id=root.id,
            evolution_decision_id=decision.id,
            source_logical_fact_id=source_id,
            supersedes_source_transition_id=(
                previous_sources[source_id].id if source_id in previous_sources else None
            ),
            resolution_state=source_state,
        )
        sources[source_id] = source
        db.add(source)
    slots: dict[str, FactReconciliationTargetSlot] = {}
    for plan in target_plans:
        input_slot = plan.input
        target_id = input_slot.existing_logical_fact_id if input_slot.target_ref_kind == "existing" else (
            plan.planned_id if applied else None
        )
        slot = FactReconciliationTargetSlot(
            id=uuid.uuid4(),
            library_id=root.library_id,
            command_id=root.id,
            evolution_decision_id=decision.id,
            target_key=input_slot.target_key,
            target_ref_kind=input_slot.target_ref_kind,
            existing_logical_fact_id=input_slot.existing_logical_fact_id,
            target_spec_snapshot=_stable_value(plan.snapshot),
            target_identity_fingerprint=plan.identity_fingerprint,
            planned_target_logical_fact_id=plan.planned_id,
            target_logical_fact_id=target_id,
            slot_state="applied" if applied else "pending",
            supersedes_target_slot_id=(
                previous_slots[input_slot.target_key].id
                if input_slot.target_key in previous_slots
                else None
            ),
        )
        slots[input_slot.target_key] = slot
        db.add(slot)
    # Edges use composite FKs to both source transitions and target slots.
    await db.flush()
    edges: dict[tuple[uuid.UUID, str], FactReconciliationSourceTargetEdge] = {}
    for topology in command.source_target_topology:
        for target_key in topology.target_keys:
            edge = FactReconciliationSourceTargetEdge(
                id=uuid.uuid4(),
                library_id=root.library_id,
                command_id=root.id,
                evolution_decision_id=decision.id,
                source_logical_fact_id=topology.source_logical_fact_id,
                source_transition_id=sources[topology.source_logical_fact_id].id,
                target_key=target_key,
                target_slot_id=slots[target_key].id,
                edge_state="applied" if applied else "pending",
                supersedes_source_target_edge_id=(
                    previous_edges[(topology.source_logical_fact_id, target_key)].id
                    if (topology.source_logical_fact_id, target_key) in previous_edges
                    else None
                ),
            )
            edges[(topology.source_logical_fact_id, target_key)] = edge
            db.add(edge)
    # ORM models deliberately retain only scalar audit IDs. Flush the referenced
    # source/slot/edge rows before assignments so PostgreSQL sees their composite FKs.
    await db.flush()
    assignment_rows: list[FactReconciliationAssertionAssignment] = []
    for assertion_id, (group, basis, input_assignment) in assignments.items():
        resolved = applied and input_assignment.state == "resolved"
        target_key = input_assignment.target_key if resolved else None
        edge = (
            edges[(input_assignment.source_logical_fact_id, target_key)]
            if target_key is not None
            else None
        )
        row = FactReconciliationAssertionAssignment(
            id=uuid.uuid4(),
            library_id=root.library_id,
            command_id=root.id,
            evolution_decision_id=decision.id,
            source_logical_fact_id=input_assignment.source_logical_fact_id,
            source_transition_id=sources[input_assignment.source_logical_fact_id].id,
            fact_assertion_id=assertion_id,
            source_group_fingerprint=group,
            partition_basis_snapshot=_stable_value(basis),
            assignment_state="resolved" if resolved else "pending",
            target_key=target_key,
            source_target_edge_id=edge.id if edge is not None else None,
            reason_code=input_assignment.reason_code,
            supersedes_assignment_id=(
                previous_assignments[assertion_id].id if assertion_id in previous_assignments else None
            ),
        )
        assignment_rows.append(row)
        db.add(row)
    if applied:
        for plan in target_plans:
            if plan.input.target_ref_kind != "new":
                continue
            spec = plan.input.target_spec
            db.add(
                LogicalFact(
                    id=plan.planned_id,
                    library_id=root.library_id,
                    stable_predicate_identity_id=spec.stable_predicate_identity_id,
                    subject_canonical_entity_id=spec.subject_canonical_entity_id,
                    object_kind=spec.object_kind,
                    object_canonical_entity_id=spec.object_canonical_entity_id,
                    object_value=(dict(spec.object_value) if spec.object_value else None),
                    identity_qualifiers=dict(spec.identity_qualifiers),
                    temporal_identity_key=spec.temporal_identity_key,
                    identity_policy_version=spec.identity_policy_version,
                    identity_fingerprint=plan.identity_fingerprint,
                    reconciliation_target_slot_id=slots[plan.input.target_key].id,
                    status="inactive",
                )
            )
    await db.flush()
    return tuple(sources.values()), tuple(slots.values()), tuple(edges.values()), tuple(assignment_rows)


async def apply_fact_reconciliation(
    db: Any, command: FactReconciliationCommand
) -> FactReconciliationResult:
    """Append one P3.3 Decision; caller owns commit and rollback."""

    validation_error = _validate_command(command)
    if validation_error is not None:
        return FactReconciliationResult("REJECTED", validation_error)
    try:
        command_fingerprint = fact_reconciliation_command_fingerprint(command)
    except FactReconciliationError as exc:
        return FactReconciliationResult("REJECTED", str(exc))
    root: FactReconciliationCommandRow | None = None
    target_plans, target_error = await _target_plans(db, command, command_fingerprint)
    if target_error is not None:
        return FactReconciliationResult("REJECTED", target_error, command=root)
    topology = _topology_kind(command)
    if topology == "n_to_m":
        return FactReconciliationResult("PENDING", "n_to_m_not_authorized", command=root)
    try:
        await _lock_reconciliation_scopes(db, command=command, target_plans=target_plans)
    except GraphIdentityLockBusy as exc:
        raise FactReconciliationRetryableConflict("fact_reconciliation_lock_busy") from exc
    locked_target_plans, target_error = await _target_plans(db, command, command_fingerprint)
    if target_error is not None:
        return FactReconciliationResult("STALE_OPERATION", target_error, command=root)
    if locked_target_plans != target_plans:
        return FactReconciliationResult("STALE_OPERATION", "target_identity_changed", command=root)
    target_plans = locked_target_plans
    root = await _command_by_key(db, command.library_id, command.idempotency_key)
    if root is not None and root.command_identity_fingerprint != command_fingerprint:
        return FactReconciliationResult("REJECTED", "idempotency_key_conflict", command=root)
    if root is None:
        alias = await _command_by_identity(db, command.library_id, command_fingerprint)
        if alias is not None:
            return FactReconciliationResult("REJECTED", "command_identity_alias_key", command=alias)
    elif (replay := await _replay(db, root, command)) is not None:
        return replay
    predecessor = _current_decision(await _decision_rows(db, command.library_id, root.id)) if root else None
    membership = await _current_membership(db, command.library_id, set(command.source_logical_fact_ids))
    admission_error = await _admission_error(
        db,
        command=command,
        target_plans=target_plans,
        pending_predecessor=(
            predecessor if predecessor is not None and predecessor.lifecycle_status == "pending" else None
        ),
    )
    if admission_error is not None:
        status = "REJECTED" if admission_error == "source_equals_target" else "STALE_OPERATION"
        return FactReconciliationResult(status, admission_error, command=root)
    if root is None and await _root_slot_occupied(db, command):
        return FactReconciliationResult("STALE_OPERATION", "source_slot_occupied")
    prepared, assignment_error = await _prepared_assignments(
        db, command=command, membership=membership
    )
    if assignment_error is not None:
        return FactReconciliationResult("REJECTED", assignment_error, command=root)
    has_pending = any(entry.state == "pending" for _, _, entry in prepared.values())
    if topology in {"1_to_1", "n_to_1"} and (command.requested_effect == "stage" or has_pending):
        return FactReconciliationResult("REJECTED", "pending_topology_not_authorized", command=root)
    if command.requested_effect == "stage" and (topology != "1_to_n" or not has_pending):
        return FactReconciliationResult("REJECTED", "stage_requires_pending_split", command=root)
    if command.requested_effect == "apply" and has_pending:
        return FactReconciliationResult("REJECTED", "apply_requires_complete_partition", command=root)
    observed = await _precondition_fingerprint(
        db, command=command, target_plans=target_plans, membership=membership
    )
    if predecessor is not None and command.expected_predecessor_decision_id != predecessor.id:
        return FactReconciliationResult(
            "STALE_OPERATION",
            "expected_predecessor_mismatch",
            command=root,
            current_decision_id=predecessor.id,
            current_decision_status=predecessor.lifecycle_status,
        )
    if predecessor is None and command.expected_predecessor_decision_id is not None:
        return FactReconciliationResult("STALE_OPERATION", "expected_predecessor_mismatch")
    if predecessor is not None and predecessor.lifecycle_status in {"applied", "cancelled"}:
        return FactReconciliationResult("REJECTED", "command_closed", command=root)
    if command.expected_precondition_fingerprint != observed:
        return FactReconciliationResult(
            "STALE_OPERATION",
            "precondition_changed",
            command=root,
            current_decision_id=predecessor.id if predecessor else None,
            current_decision_status=predecessor.lifecycle_status if predecessor else None,
        )
    operation_payload = _normal_assignment_payload(command, prepared)
    outcome = "pending" if command.requested_effect == "stage" else "applied"
    if root is None:
        root = _new_root(command, command_fingerprint)
        db.add(root)
        await db.flush()
    previous_sources: dict[uuid.UUID, FactReconciliationSource] = {}
    previous_slots: dict[str, FactReconciliationTargetSlot] = {}
    previous_edges: dict[tuple[uuid.UUID, str], FactReconciliationSourceTargetEdge] = {}
    previous_assignments: dict[uuid.UUID, FactReconciliationAssertionAssignment] = {}
    if predecessor is not None:
        if predecessor.lifecycle_status != "pending":
            return FactReconciliationResult("REJECTED", "predecessor_transition_invalid", command=root)
        previous_sources, previous_slots, previous_edges, previous_assignments = await _supersede_pending_proposal(db, predecessor)
    decision = _new_decision(root, command, operation_payload, outcome, observed, predecessor)
    db.add(decision)
    await db.flush()
    try:
        sources, slots, edges, rows = await _create_children(
            db,
            root=root,
            decision=decision,
            command=command,
            target_plans=target_plans,
            assignments=prepared,
            applied=outcome == "applied",
            previous_sources=previous_sources,
            previous_slots=previous_slots,
            previous_edges=previous_edges,
            previous_assignments=previous_assignments,
        )
    except IntegrityError as exc:
        raise FactReconciliationRetryableConflict("integrity_conflict") from exc
    if outcome == "applied":
        from app.services.fact_lifecycle import recalculate_current_reconciled_projection_for_assertions

        await recalculate_current_reconciled_projection_for_assertions(
            db,
            library_id=command.library_id,
            assertion_ids=set(prepared),
            scopes_locked=True,
        )
    return FactReconciliationResult(
        outcome.upper(),
        command= root,
        decision=decision,
        source_transitions=sources,
        target_slots=slots,
        edges=edges,
        assignments=rows,
        current_decision_id=decision.id,
        current_decision_status=decision.lifecycle_status,
    )


def _validate_cancellation(cancellation: FactReconciliationCancellation) -> str | None:
    if not isinstance(cancellation.command_id, uuid.UUID) or not isinstance(
        cancellation.expected_pending_decision_id, uuid.UUID
    ):
        return "cancellation_target_invalid"
    if not isinstance(cancellation.original_idempotency_key, str) or not cancellation.original_idempotency_key:
        return "idempotency_key_invalid"
    if not all(
        isinstance(value, str) and value
        for value in (
            cancellation.reason_code,
            cancellation.reason_text,
            cancellation.actor_type,
            cancellation.actor_id,
            cancellation.request_id,
        )
    ):
        return "audit_metadata_invalid"
    return None


def _cancellation_fingerprint(
    cancellation: FactReconciliationCancellation, expected_precondition: str
) -> str:
    return _sha256(
        {
            "confidence": None,
            "evidence_refs": _normalized_evidence_refs(cancellation.evidence_refs),
            "expected_precondition_fingerprint": expected_precondition,
            "method": "authorized_cancellation",
            "operation_payload": {
                "control_kind": "cancel_pending",
                "expected_pending_decision_id": cancellation.expected_pending_decision_id,
            },
            "reason_code": cancellation.reason_code,
            "reason_text": cancellation.reason_text,
            "requested_effect": "cancel",
        }
    )


async def _command_by_id(
    db: Any,
    library_id: uuid.UUID,
    command_id: uuid.UUID,
    *,
    for_update: bool = True,
) -> FactReconciliationCommandRow | None:
    read_rows = _locked_rows if for_update else _rows
    rows = [
        row
        for row in await read_rows(db, FactReconciliationCommandRow, library_id)
        if row.id == command_id
    ]
    return rows[0] if len(rows) == 1 else None


async def cancel_pending_fact_reconciliation(
    db: Any,
    *,
    library_id: uuid.UUID,
    cancellation: FactReconciliationCancellation,
) -> FactReconciliationResult:
    """Append a cancellation Decision and release only its matching pending intent."""

    validation_error = _validate_cancellation(cancellation)
    if validation_error is not None:
        return FactReconciliationResult("REJECTED", validation_error)
    root = await _command_by_id(
        db, library_id, cancellation.command_id, for_update=False
    )
    if root is None:
        return FactReconciliationResult("REJECTED", "command_not_found")
    if not hmac.compare_digest(root.idempotency_key, cancellation.original_idempotency_key):
        return FactReconciliationResult("REJECTED", "cancellation_root_key_mismatch", command=root)
    decisions = await _decision_rows(db, library_id, root.id, for_update=False)
    head = _current_decision(decisions)
    if head is None:
        return FactReconciliationResult("REJECTED", "command_unusable", command=root)
    if head.requested_effect == "cancel":
        cancellation_hash = _cancellation_fingerprint(
            cancellation, head.observed_precondition_fingerprint
        )
        if hmac.compare_digest(head.decision_payload_fingerprint, cancellation_hash):
            return FactReconciliationResult(
                "CANCELLED",
                "cancel_already_committed",
                command=root,
                decision=head,
                reused_decision_id=head.id,
                effective_outcome="CANCELLED",
                current_decision_id=head.id,
                current_decision_status=head.lifecycle_status,
            )
        return FactReconciliationResult("REJECTED", "command_closed", command=root)
    if head.id != cancellation.expected_pending_decision_id or head.lifecycle_status != "pending":
        return FactReconciliationResult(
            "STALE_OPERATION",
            "expected_pending_decision_mismatch",
            command=root,
            current_decision_id=head.id,
            current_decision_status=head.lifecycle_status,
        )
    source_rows = [
        row
        for row in await _rows(db, FactReconciliationSource, library_id)
        if row.evolution_decision_id == head.id and row.resolution_state == "pending"
    ]
    facts = {row.id: row for row in await _rows(db, LogicalFact, library_id)}
    scopes: list[GraphIdentityLockScope] = []
    for row in source_rows:
        fact = facts.get(row.source_logical_fact_id)
        if fact is None:
            raise FactReconciliationRetryableConflict("source_scope_mismatch")
        scopes.extend(
            (
                GraphIdentityLockScope(
                    CANONICAL_ENTITY_LOCK_SCOPE, fact.subject_canonical_entity_id
                ),
                GraphIdentityLockScope(
                    STABLE_PREDICATE_LOCK_SCOPE, fact.stable_predicate_identity_id
                ),
                GraphIdentityLockScope(LOGICAL_FACT_LOCK_SCOPE, fact.identity_fingerprint),
            )
        )
        if fact.object_canonical_entity_id is not None:
            scopes.append(
                GraphIdentityLockScope(
                    CANONICAL_ENTITY_LOCK_SCOPE, fact.object_canonical_entity_id
                )
            )
    try:
        await lock_graph_identity_scopes(db, library_id, scopes, wait=False)
    except GraphIdentityLockBusy as exc:
        raise FactReconciliationRetryableConflict("fact_reconciliation_lock_busy") from exc
    root = await _command_by_id(db, library_id, cancellation.command_id)
    if root is None:
        return FactReconciliationResult("REJECTED", "command_not_found")
    if not hmac.compare_digest(root.idempotency_key, cancellation.original_idempotency_key):
        return FactReconciliationResult("REJECTED", "cancellation_root_key_mismatch", command=root)
    head = _current_decision(await _decision_rows(db, library_id, root.id))
    if head is None or head.id != cancellation.expected_pending_decision_id or head.lifecycle_status != "pending":
        return FactReconciliationResult("STALE_OPERATION", "expected_pending_decision_mismatch", command=root)
    cancellation_hash = _cancellation_fingerprint(
        cancellation, head.observed_precondition_fingerprint
    )
    await _supersede_pending_proposal(db, head)
    decision = FactReconciliationDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=root.id,
        decision_payload_fingerprint=cancellation_hash,
        operation_kind="reconcile",
        requested_effect="cancel",
        evaluated_outcome="cancelled",
        lifecycle_status="cancelled",
        operation_payload_snapshot={
            "control_kind": "cancel_pending",
            "expected_pending_decision_id": str(cancellation.expected_pending_decision_id),
        },
        expected_precondition_fingerprint=head.observed_precondition_fingerprint,
        observed_precondition_fingerprint=head.observed_precondition_fingerprint,
        reason_code=cancellation.reason_code,
        reason_text=cancellation.reason_text,
        method="authorized_cancellation",
        confidence=None,
        evidence_refs=_stable_value(_normalized_evidence_refs(cancellation.evidence_refs)),
        supersedes_decision_id=head.id,
        actor_type=cancellation.actor_type,
        actor_id=cancellation.actor_id,
        request_id=cancellation.request_id,
    )
    db.add(decision)
    await db.flush()
    return FactReconciliationResult(
        "CANCELLED",
        "cancelled_pending_intent",
        command=root,
        decision=decision,
        current_decision_id=decision.id,
        current_decision_status=decision.lifecycle_status,
    )


__all__ = [
    "CurrentLogicalFactResult",
    "FactReconciliationAssignmentInput",
    "FactReconciliationCancellation",
    "FactReconciliationCommand",
    "FactReconciliationError",
    "FactReconciliationResult",
    "FactReconciliationRetryableConflict",
    "FactReconciliationTargetSlotInput",
    "FactReconciliationTargetSpec",
    "FactReconciliationTopology",
    "apply_fact_reconciliation",
    "build_fact_reconciliation_precondition_fingerprint",
    "cancel_pending_fact_reconciliation",
    "fact_reconciliation_command_fingerprint",
    "fact_reconciliation_command_json_bytes",
    "planned_target_logical_fact_id",
    "resolve_current_logical_fact",
]
