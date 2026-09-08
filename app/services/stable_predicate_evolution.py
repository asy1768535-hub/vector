"""StablePredicate evolution identities and deterministic target allocation."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unicodedata import normalize

import rfc8785
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.fact_foundation import StablePredicateIdentity, StablePredicateMapping
from app.models.library import Library
from app.models.stable_predicate_evolution import (
    PREDICATE_EVOLUTION_ASSIGNMENT_PENDING,
    PREDICATE_EVOLUTION_ASSIGNMENT_RESOLVED,
    PREDICATE_EVOLUTION_ASSIGNMENT_SUPERSEDED,
    PREDICATE_EVOLUTION_DECISION_APPLIED,
    PREDICATE_EVOLUTION_DECISION_CANCELLED,
    PREDICATE_EVOLUTION_DECISION_PENDING,
    PREDICATE_EVOLUTION_DECISION_REJECTED,
    PREDICATE_EVOLUTION_DECISION_STALE,
    PREDICATE_EVOLUTION_DECISION_SUPERSEDED,
    PREDICATE_EVOLUTION_SOURCE_APPLIED,
    PREDICATE_EVOLUTION_SOURCE_PENDING,
    PREDICATE_EVOLUTION_SOURCE_SUPERSEDED,
    StablePredicateEvolutionCommand,
    StablePredicateEvolutionDecision,
    StablePredicateEvolutionSource,
    StablePredicateEvolutionSuccessor,
    StablePredicateMappingEvolutionAssignment,
)
from app.services.graph_identity_locks import (
    STABLE_PREDICATE_LOCK_SCOPE,
    GraphIdentityLockBusy,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)
from app.services.stable_predicate_evolution_actor import (
    credential_api_key_audit_identity,
)
from app.services.stable_predicate_resolution_policy import (
    parse_stable_predicate_resolution_policy,
)

_COMMAND_SCHEMA = "p3_2_stable_predicate_evolution_command_v1"
_TARGET_SCHEMA = "p3_2_stable_predicate_target_v1"
_TARGET_UUID_PREFIX = "urn:vector-kb:p3.2:stable-predicate-identity:v1:"
_CONTRACT_VERSION = "p3_2_stable_predicate_evolution/v1"
_TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")


class StablePredicateEvolutionError(ValueError):
    """The supplied evolution identity is not in the frozen P3.2 domain."""


class StablePredicateEvolutionAuthorizationError(StablePredicateEvolutionError):
    """The authenticated principal cannot inspect or mutate this library."""


class StablePredicateEvolutionRetryableConflict(RuntimeError):
    """The caller must roll back the outer transaction before mapping a retry."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class StablePredicateEvolutionContext:
    mapping_id: uuid.UUID | None = None
    relation_type_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CurrentStablePredicateResult:
    status: str
    historical_predicate_id: uuid.UUID
    current_predicate_id: uuid.UUID | None
    current_mapping_id: uuid.UUID | None
    lineage_decision_ids: tuple[uuid.UUID, ...]
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class StablePredicateTargetSpec:
    namespace: str
    key: str
    contract_version: str
    temporal_class: str
    identity_policy_version: str
    resolution_policy: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StablePredicateSuccessorSlot:
    kind: str
    predicate_id: uuid.UUID
    target_spec: StablePredicateTargetSpec | None = None

    @classmethod
    def existing(cls, predicate_id: uuid.UUID) -> StablePredicateSuccessorSlot:
        return cls("existing", predicate_id)

    @classmethod
    def new(
        cls,
        predicate_id: uuid.UUID,
        target_spec: StablePredicateTargetSpec,
    ) -> StablePredicateSuccessorSlot:
        return cls("new", predicate_id, target_spec)


@dataclass(frozen=True, slots=True)
class StablePredicateMappingPartition:
    mapping_id: uuid.UUID
    state: str
    target_predicate_id: uuid.UUID | None
    partition_basis_snapshot: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str = "mapping_partition"


@dataclass(frozen=True, slots=True)
class StablePredicateMergeCommand:
    library_id: uuid.UUID
    source_predicate_ids: tuple[uuid.UUID, ...]
    survivor_predicate_id: uuid.UUID
    idempotency_key: str = ""
    requested_effect: str | None = None
    reason_code: str = ""
    reason_text: str = ""
    method: str = ""
    evidence_refs: tuple[Mapping[str, Any], ...] = ()
    actor_type: str = ""
    actor_id: uuid.UUID | None = None
    request_id: str = ""
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StablePredicateSplitCommand:
    library_id: uuid.UUID
    source_predicate_id: uuid.UUID
    successor_slots: tuple[StablePredicateSuccessorSlot, ...]
    idempotency_key: str = ""
    requested_effect: str | None = None
    reason_code: str = ""
    reason_text: str = ""
    method: str = ""
    evidence_refs: tuple[Mapping[str, Any], ...] = ()
    actor_type: str = ""
    actor_id: uuid.UUID | None = None
    request_id: str = ""
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None
    mapping_assignments: tuple[StablePredicateMappingPartition, ...] = ()


@dataclass(frozen=True, slots=True)
class StablePredicateReassignCommand:
    library_id: uuid.UUID
    mapping_id: uuid.UUID
    from_predicate_id: uuid.UUID
    to_predicate_id: uuid.UUID
    idempotency_key: str = ""
    requested_effect: str | None = None
    reason_code: str = ""
    reason_text: str = ""
    method: str = ""
    evidence_refs: tuple[Mapping[str, Any], ...] = ()
    actor_type: str = ""
    actor_id: uuid.UUID | None = None
    request_id: str = ""
    expected_precondition_fingerprint: str | None = None
    expected_predecessor_decision_id: uuid.UUID | None = None
    confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StablePredicateEvolutionCancellation:
    command_id: uuid.UUID
    original_idempotency_key: str
    expected_pending_decision_id: uuid.UUID
    reason_code: str
    reason_text: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    request_id: str
    actor_type: str = ""
    actor_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class StablePredicateEvolutionResult:
    status: str
    reason_code: str | None = None
    command: StablePredicateEvolutionCommand | None = None
    decision: StablePredicateEvolutionDecision | None = None
    source_transitions: tuple[StablePredicateEvolutionSource, ...] = ()
    successors: tuple[StablePredicateEvolutionSuccessor, ...] = ()
    assignments: tuple[StablePredicateMappingEvolutionAssignment, ...] = ()
    reused_decision_id: uuid.UUID | None = None
    effective_outcome: str | None = None
    current_decision_id: uuid.UUID | None = None
    current_decision_status: str | None = None


StablePredicateCommand = (
    StablePredicateMergeCommand
    | StablePredicateSplitCommand
    | StablePredicateReassignCommand
)


async def _authorize_mutation(
    db,
    *,
    library_id: uuid.UUID,
    user: Any,
    credential_kind: str,
) -> tuple[str, uuid.UUID]:
    if (
        not isinstance(library_id, uuid.UUID)
        or user is None
        or not isinstance(getattr(user, "id", None), uuid.UUID)
        or credential_kind not in {"session", "api_key"}
    ):
        raise StablePredicateEvolutionAuthorizationError("predicate_evolution_forbidden")
    result = await db.execute(
        select(Library).where(
            Library.id == library_id,
            Library.deleted_at.is_(None),
        )
    )
    libraries = [
        row
        for row in result.scalars().all()
        if row.id == library_id and getattr(row, "deleted_at", None) is None
    ]
    if len(libraries) != 1:
        raise StablePredicateEvolutionAuthorizationError("predicate_evolution_forbidden")
    library = libraries[0]
    try:
        await authorize_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise StablePredicateEvolutionAuthorizationError(
            "predicate_evolution_forbidden"
        ) from exc
    api_key_identity = credential_api_key_audit_identity(user)
    if credential_kind == "api_key":
        if (
            api_key_identity is None
            or api_key_identity.organization_id != library.organization_id
        ):
            raise StablePredicateEvolutionAuthorizationError(
                "predicate_evolution_forbidden"
            )
        return "service", api_key_identity.api_key_id
    if api_key_identity is not None:
        raise StablePredicateEvolutionAuthorizationError("predicate_evolution_forbidden")
    return "user", user.id


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if normalize("NFC", value) != value:
            raise StablePredicateEvolutionError("identity strings must be NFC")
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise StablePredicateEvolutionError("integer is outside I-JSON range")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or (value.is_zero() and value.is_signed()):
            raise StablePredicateEvolutionError("decimal is not canonical")
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        raise StablePredicateEvolutionError("binary floats are not canonical")
    if isinstance(value, Mapping):
        if not all(
            isinstance(key, str) and normalize("NFC", key) == key for key in value
        ):
            raise StablePredicateEvolutionError("object keys must be strings")
        return {key: _stable_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    raise StablePredicateEvolutionError(f"unsupported identity value: {type(value).__name__}")


def _jcs(value: Mapping[str, Any]) -> bytes:
    try:
        return rfc8785.dumps(_stable_value(value))
    except rfc8785.CanonicalizationError as exc:
        raise StablePredicateEvolutionError("value is not RFC 8785 JSON") from exc


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_jcs(value)).hexdigest()


def _target_payload(target: StablePredicateTargetSpec) -> dict[str, Any]:
    if target.temporal_class not in {
        "static_fact",
        "state_fact",
        "measurement_slot",
        "event_fact",
    }:
        raise StablePredicateEvolutionError("invalid target temporal class")
    if not (
        _valid_text(target.namespace, scalars=128, utf8_bytes=512)
        and _valid_text(target.key, scalars=128, utf8_bytes=512)
        and _valid_token(target.contract_version)
        and _valid_token(target.identity_policy_version)
        and isinstance(target.resolution_policy, Mapping)
    ):
        raise StablePredicateEvolutionError("target identity fields are required")
    try:
        policy = parse_stable_predicate_resolution_policy(target.resolution_policy)
    except (TypeError, ValueError) as exc:
        raise StablePredicateEvolutionError("target policy is invalid") from exc
    if policy.temporal_class != target.temporal_class:
        raise StablePredicateEvolutionError("target policy temporal class is invalid")
    payload = {
        "schema": _TARGET_SCHEMA,
        "namespace": target.namespace,
        "key": target.key,
        "contract_version": target.contract_version,
        "temporal_class": target.temporal_class,
        "identity_policy_version": target.identity_policy_version,
        "resolution_status": "resolved",
        "resolution_policy": dict(target.resolution_policy),
    }
    if _canonical_json_size(payload) > 65536:
        raise StablePredicateEvolutionError("target specification is too large")
    return payload


def stable_predicate_target_fingerprint(target: StablePredicateTargetSpec) -> str:
    return _sha256(_target_payload(target))


def stable_predicate_target_id(
    library_id: uuid.UUID,
    target: StablePredicateTargetSpec,
) -> uuid.UUID:
    identity_hash = _sha256(
        {
            "contract_version": target.contract_version,
            "key": target.key,
            "library_id": library_id,
            "namespace": target.namespace,
        }
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, _TARGET_UUID_PREFIX + identity_hash)


def _command_payload(command: StablePredicateCommand) -> dict[str, Any]:
    common = {
        "schema": _COMMAND_SCHEMA,
        "library_id": command.library_id,
    }
    if isinstance(command, StablePredicateMergeCommand):
        sources = sorted(command.source_predicate_ids, key=str)
        if not sources or len(set(sources)) != len(sources):
            raise StablePredicateEvolutionError("merge sources must be distinct")
        if command.survivor_predicate_id in sources:
            raise StablePredicateEvolutionError("merge survivor cannot be a source")
        return {
            **common,
            "operation": "merge",
            "source_predicate_ids": sources,
            "survivor_predicate_id": command.survivor_predicate_id,
        }
    if isinstance(command, StablePredicateSplitCommand):
        if len(command.successor_slots) < 2:
            raise StablePredicateEvolutionError("split requires at least two successors")
        predicate_ids = [slot.predicate_id for slot in command.successor_slots]
        if len(set(predicate_ids)) != len(predicate_ids) or command.source_predicate_id in predicate_ids:
            raise StablePredicateEvolutionError("split successor predicates must be distinct")
        slots: list[dict[str, Any]] = []
        target_fingerprints: set[str] = set()
        for slot in command.successor_slots:
            if slot.kind == "existing" and slot.target_spec is None:
                slots.append({"kind": "existing", "predicate_id": slot.predicate_id})
                continue
            if slot.kind != "new" or slot.target_spec is None:
                raise StablePredicateEvolutionError("invalid split successor slot")
            if stable_predicate_target_id(command.library_id, slot.target_spec) != slot.predicate_id:
                raise StablePredicateEvolutionError("new target UUID does not match its identity")
            fingerprint = stable_predicate_target_fingerprint(slot.target_spec)
            if fingerprint in target_fingerprints:
                raise StablePredicateEvolutionError("new target specifications must be distinct")
            target_fingerprints.add(fingerprint)
            slots.append(
                {
                    "kind": "new",
                    "predicate_id": slot.predicate_id,
                    "target_spec_fingerprint": fingerprint,
                }
            )
        slots.sort(key=lambda item: (0 if item["kind"] == "existing" else 1, str(item["predicate_id"])))
        return {
            **common,
            "operation": "split",
            "source_predicate_id": command.source_predicate_id,
            "successor_slots": slots,
        }
    if command.from_predicate_id == command.to_predicate_id:
        raise StablePredicateEvolutionError("reassignment target must differ from source")
    return {
        **common,
        "operation": "reassign",
        "mapping_id": command.mapping_id,
        "from_predicate_id": command.from_predicate_id,
        "to_predicate_id": command.to_predicate_id,
    }


def stable_predicate_command_json_bytes(command: StablePredicateCommand) -> bytes:
    return _jcs(_command_payload(command))


def stable_predicate_command_fingerprint(command: StablePredicateCommand) -> str:
    return hashlib.sha256(stable_predicate_command_json_bytes(command)).hexdigest()


def _normalized_evidence_refs(
    evidence_refs: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    normalized = [dict(item) for item in evidence_refs]
    encoded = [_jcs(item) for item in normalized]
    if any(not item for item in normalized) or len(set(encoded)) != len(encoded):
        raise StablePredicateEvolutionError("evidence references must be distinct nonempty objects")
    return [item for _, item in sorted(zip(encoded, normalized, strict=True), key=lambda pair: pair[0])]


def _normalized_operation_payload(
    command: StablePredicateCommand,
    operation_payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(operation_payload)
    if isinstance(command, StablePredicateMergeCommand):
        assignments = list(payload.get("mapping_assignments", ()))
        policies = list(payload.get("policy_compatibility", ()))
        assignments.sort(key=lambda item: str(item["mapping_id"]))
        policies.sort(key=lambda item: str(item["predicate_id"]))
        payload["mapping_assignments"] = assignments
        payload["policy_compatibility"] = policies
    elif isinstance(command, StablePredicateSplitCommand):
        assignments = list(payload.get("mapping_assignments", ()))
        successors = list(payload.get("successor_slots", ()))
        assignments.sort(key=lambda item: str(item["mapping_id"]))
        successors.sort(
            key=lambda item: (
                0 if item["kind"] == "existing" else 1,
                str(item["predicate_id"]),
            )
        )
        payload["mapping_assignments"] = assignments
        payload["successor_slots"] = successors
    return payload


def _decision_identity_payload(
    command: StablePredicateCommand,
    operation_payload: Mapping[str, Any],
) -> dict[str, Any]:
    expected = command.expected_precondition_fingerprint
    if not _is_fingerprint(expected):
        raise StablePredicateEvolutionError("expected precondition fingerprint is invalid")
    return {
        "confidence": command.confidence,
        "evidence_refs": _normalized_evidence_refs(command.evidence_refs),
        "expected_precondition_fingerprint": expected,
        "method": command.method,
        "operation_payload": _normalized_operation_payload(command, operation_payload),
        "reason_code": command.reason_code,
        "reason_text": command.reason_text,
        "requested_effect": command.requested_effect,
    }


def stable_predicate_decision_fingerprint(
    command: StablePredicateCommand,
    operation_payload: Mapping[str, Any],
) -> str:
    return _sha256(_decision_identity_payload(command, operation_payload))


def stable_predicate_cancellation_fingerprint(
    cancellation: StablePredicateEvolutionCancellation,
    expected_precondition_fingerprint: str,
) -> str:
    return _sha256(
        {
            "confidence": None,
            "evidence_refs": _normalized_evidence_refs(cancellation.evidence_refs),
            "expected_precondition_fingerprint": expected_precondition_fingerprint,
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


def _operation(command: StablePredicateCommand) -> str:
    if isinstance(command, StablePredicateMergeCommand):
        return "merge"
    if isinstance(command, StablePredicateSplitCommand):
        return "split"
    return "reassign"


def _source_predicate_ids(command: StablePredicateCommand) -> tuple[uuid.UUID, ...]:
    if isinstance(command, StablePredicateMergeCommand):
        return command.source_predicate_ids
    if isinstance(command, StablePredicateSplitCommand):
        return (command.source_predicate_id,)
    return (command.from_predicate_id,)


def _existing_target_predicate_ids(
    command: StablePredicateCommand,
) -> tuple[uuid.UUID, ...]:
    if isinstance(command, StablePredicateMergeCommand):
        return (command.survivor_predicate_id,)
    if isinstance(command, StablePredicateSplitCommand):
        return tuple(slot.predicate_id for slot in command.successor_slots if slot.kind == "existing")
    return (command.to_predicate_id,)


def _new_target_slots(
    command: StablePredicateCommand,
) -> tuple[StablePredicateSuccessorSlot, ...]:
    if not isinstance(command, StablePredicateSplitCommand):
        return ()
    return tuple(slot for slot in command.successor_slots if slot.kind == "new")


def _is_fingerprint(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise StablePredicateEvolutionError("string is not valid UTF-8") from exc


def _valid_text(value: Any, *, scalars: int, utf8_bytes: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and normalize("NFC", value) == value
        and len(value) <= scalars
        and _utf8_size(value) <= utf8_bytes
    )


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def _json_depth(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, (tuple, list)):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _canonical_json_size(value: Any) -> int:
    if _json_depth(value) > 32:
        raise StablePredicateEvolutionError("JSON nesting exceeds 32")
    try:
        return len(rfc8785.dumps(_stable_value(value)))
    except rfc8785.CanonicalizationError as exc:
        raise StablePredicateEvolutionError("value is not RFC 8785 JSON") from exc


def _payload_error(exc: StablePredicateEvolutionError) -> str:
    message = str(exc)
    return (
        "payload_too_large"
        if "exceeds 32" in message or "too large" in message
        else "invalid_envelope"
    )


def _valid_request_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and bool(value.strip())
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _validate_decision_payload(
    command: StablePredicateCommand,
    operation_payload: Mapping[str, Any],
) -> str | None:
    try:
        normalized = _decision_identity_payload(command, operation_payload)
        if _canonical_json_size(normalized["operation_payload"]) > 1048576:
            return "payload_too_large"
        if _canonical_json_size(normalized) > 1048576:
            return "payload_too_large"
    except (StablePredicateEvolutionError, TypeError, KeyError) as exc:
        if isinstance(exc, StablePredicateEvolutionError):
            return _payload_error(exc)
        return "invalid_envelope"
    return None


def _validate_cancellation_request(
    library_id: uuid.UUID,
    cancellation: StablePredicateEvolutionCancellation,
    *,
    require_actor: bool,
) -> str | None:
    if (
        not isinstance(library_id, uuid.UUID)
        or not isinstance(cancellation.command_id, uuid.UUID)
        or not isinstance(cancellation.expected_pending_decision_id, uuid.UUID)
        or not _valid_text(
            cancellation.original_idempotency_key,
            scalars=256,
            utf8_bytes=1024,
        )
        or not _valid_token(cancellation.reason_code)
        or not _valid_text(cancellation.reason_text, scalars=512, utf8_bytes=2048)
        or not _valid_request_id(cancellation.request_id)
    ):
        return "invalid_envelope"
    if require_actor:
        if cancellation.actor_type not in {"user", "service"} or not isinstance(
            cancellation.actor_id,
            uuid.UUID,
        ):
            return "invalid_envelope"
    elif cancellation.actor_type != "" or cancellation.actor_id is not None:
        return "invalid_envelope"
    try:
        if len(cancellation.evidence_refs) > 256:
            return "payload_too_large"
        evidence = _normalized_evidence_refs(cancellation.evidence_refs)
        if _canonical_json_size(evidence) > 262144:
            return "payload_too_large"
        payload = {
            "confidence": None,
            "evidence_refs": evidence,
            "expected_precondition_fingerprint": "0" * 64,
            "method": "authorized_cancellation",
            "operation_payload": {
                "control_kind": "cancel_pending",
                "expected_pending_decision_id": cancellation.expected_pending_decision_id,
            },
            "reason_code": cancellation.reason_code,
            "reason_text": cancellation.reason_text,
            "requested_effect": "cancel",
        }
        if _canonical_json_size(payload["operation_payload"]) > 1048576:
            return "payload_too_large"
        if _canonical_json_size(payload) > 1048576:
            return "payload_too_large"
    except (StablePredicateEvolutionError, TypeError) as exc:
        if isinstance(exc, StablePredicateEvolutionError):
            return _payload_error(exc)
        return "invalid_envelope"
    return None


def _is_lock_not_available(exc: DBAPIError) -> bool:
    original = getattr(exc, "orig", None)
    return (
        getattr(original, "sqlstate", None) == "55P03"
        or getattr(original, "pgcode", None) == "55P03"
    )


async def _scoped_rows(
    db,
    model,
    library_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> list[Any]:
    statement = select(model).where(model.library_id == library_id)
    if for_update:
        statement = statement.with_for_update(nowait=True).execution_options(populate_existing=True)
    result = await db.execute(statement)
    return [row for row in result.scalars().all() if row.library_id == library_id]


async def _command_roots(db, library_id: uuid.UUID) -> list[StablePredicateEvolutionCommand]:
    return await _scoped_rows(db, StablePredicateEvolutionCommand, library_id)


async def _root_by_key(
    db, library_id: uuid.UUID, idempotency_key: str
) -> StablePredicateEvolutionCommand | None:
    rows = [row for row in await _command_roots(db, library_id) if row.idempotency_key == idempotency_key]
    return rows[0] if len(rows) == 1 else None


async def _root_by_identity(
    db, library_id: uuid.UUID, fingerprint: str
) -> StablePredicateEvolutionCommand | None:
    rows = [
        row
        for row in await _command_roots(db, library_id)
        if row.command_identity_fingerprint == fingerprint
    ]
    return rows[0] if len(rows) == 1 else None


async def _decision_rows(
    db, library_id: uuid.UUID, command_id: uuid.UUID
) -> list[StablePredicateEvolutionDecision]:
    result = await db.execute(
        select(StablePredicateEvolutionDecision)
        .where(
            StablePredicateEvolutionDecision.library_id == library_id,
            StablePredicateEvolutionDecision.command_id == command_id,
        )
        .execution_options(populate_existing=True)
    )
    return [
        row
        for row in result.scalars().all()
        if row.library_id == library_id and row.command_id == command_id
    ]


def _current_decision(
    rows: list[StablePredicateEvolutionDecision],
) -> StablePredicateEvolutionDecision | None:
    current = [row for row in rows if row.lifecycle_status != PREDICATE_EVOLUTION_DECISION_SUPERSEDED]
    return current[0] if len(current) == 1 else None


async def _active_mappings(
    db, library_id: uuid.UUID, predicate_ids: set[uuid.UUID]
) -> list[StablePredicateMapping]:
    return sorted(
        (
            row
            for row in await _scoped_rows(db, StablePredicateMapping, library_id)
            if row.mapping_status == "active"
            and row.stable_predicate_identity_id in predicate_ids
        ),
        key=lambda row: str(row.id),
    )


def _predicate_state(row: StablePredicateIdentity, current: CurrentStablePredicateResult) -> dict[str, Any]:
    return {
        "contract_version": row.contract_version,
        "identity_policy_version": row.identity_policy_version,
        "key": row.key,
        "lineage_status": current.status,
        "namespace": row.namespace,
        "p2_resolution_status": row.resolution_status,
        "predicate_id": row.id,
        "resolution_policy_fingerprint": (
            _sha256(dict(row.resolution_policy))
            if isinstance(row.resolution_policy, Mapping)
            else None
        ),
        "resolved_predicate_id": current.current_predicate_id,
        "temporal_class": row.temporal_class,
    }


async def _precondition_payload(
    db, command: StablePredicateCommand
) -> dict[str, Any]:
    library_id = command.library_id
    command_fingerprint = stable_predicate_command_fingerprint(command)
    root = await _root_by_identity(db, library_id, command_fingerprint)
    head = _current_decision(await _decision_rows(db, library_id, root.id)) if root else None
    predicate_ids = set(_source_predicate_ids(command)) | set(_existing_target_predicate_ids(command))
    predicates = {
        row.id: row
        for row in await _scoped_rows(db, StablePredicateIdentity, library_id)
        if row.id in predicate_ids
    }
    predicate_states = []
    for predicate_id in sorted(predicate_ids, key=str):
        row = predicates.get(predicate_id)
        if row is None:
            continue
        current = await resolve_current_stable_predicate_identity(db, library_id, predicate_id)
        predicate_states.append(_predicate_state(row, current))

    if isinstance(command, StablePredicateReassignCommand):
        mappings = [
            row
            for row in await _scoped_rows(db, StablePredicateMapping, library_id)
            if row.id == command.mapping_id
        ]
        source_slot_ids = {command.from_predicate_id, command.to_predicate_id}
    else:
        mappings = await _active_mappings(db, library_id, set(_source_predicate_ids(command)))
        source_slot_ids = set(_source_predicate_ids(command))
    sources = await _scoped_rows(db, StablePredicateEvolutionSource, library_id)
    assignments = await _scoped_rows(db, StablePredicateMappingEvolutionAssignment, library_id)
    source_slots = []
    for source_id in sorted(source_slot_ids, key=str):
        live = [
            row
            for row in sources
            if row.source_predicate_id == source_id
            and row.evolution_status in {"pending", "applied", "historical_only"}
        ]
        current_transition = None
        if len(live) == 1:
            row = live[0]
            current_transition = {
                "command_id": row.command_id,
                "decision_id": row.evolution_decision_id,
                "source_transition_id": row.id,
                "status": row.evolution_status,
            }
        source_slots.append(
            {"current_transition": current_transition, "source_predicate_id": source_id}
        )
    mapping_states = [
        {
            "evolution_assignment_id": row.evolution_assignment_id,
            "mapping_id": row.id,
            "mapping_status": row.mapping_status,
            "predicate_id": row.stable_predicate_identity_id,
            "relation_type_id": row.relation_type_id,
        }
        for row in sorted(mappings, key=lambda item: str(item.id))
    ]
    mapping_slots = []
    for mapping in sorted(mappings, key=lambda item: str(item.id)):
        live = [
            row
            for row in assignments
            if row.old_mapping_id == mapping.id
            and row.assignment_state in {"pending", "resolved"}
        ]
        current_assignment = None
        if len(live) == 1:
            row = live[0]
            current_assignment = {
                "assignment_id": row.id,
                "command_id": row.command_id,
                "decision_id": row.evolution_decision_id,
                "state": row.assignment_state,
            }
        mapping_slots.append(
            {"current_assignment": current_assignment, "mapping_id": mapping.id}
        )
    all_predicates = await _scoped_rows(db, StablePredicateIdentity, library_id)
    new_target_states = []
    for slot in sorted(_new_target_slots(command), key=lambda item: str(item.predicate_id)):
        assert slot.target_spec is not None
        occupant = await db.get(StablePredicateIdentity, slot.predicate_id)
        scoped = next(
            (
                row
                for row in all_predicates
                if (row.namespace, row.key, row.contract_version)
                == (
                    slot.target_spec.namespace,
                    slot.target_spec.key,
                    slot.target_spec.contract_version,
                )
            ),
            None,
        )
        new_target_states.append(
            {
                "planned_target_predicate_id": slot.predicate_id,
                "planned_uuid_occupant": (
                    {
                        "contract_version": occupant.contract_version,
                        "key": occupant.key,
                        "library_id": occupant.library_id,
                        "namespace": occupant.namespace,
                        "predicate_id": occupant.id,
                    }
                    if occupant is not None
                    else None
                ),
                "scoped_identity": {
                    "contract_version": slot.target_spec.contract_version,
                    "key": slot.target_spec.key,
                    "namespace": slot.target_spec.namespace,
                },
                "scoped_identity_existing_predicate_id": scoped.id if scoped else None,
                "target_spec_fingerprint": stable_predicate_target_fingerprint(slot.target_spec),
            }
        )
    return {
        "schema": "p3_2_stable_predicate_precondition_v1",
        "library_id": library_id,
        "operation": _operation(command),
        "command_context": (
            {"command_id": root.id, "current_decision_id": head.id}
            if root is not None and head is not None
            else None
        ),
        "predicate_states": predicate_states,
        "mapping_states": mapping_states,
        "source_slots": source_slots,
        "mapping_slots": mapping_slots,
        "new_target_states": new_target_states,
    }


async def _build_stable_predicate_precondition_fingerprint(
    db, command: StablePredicateCommand
) -> str:
    return _sha256(await _precondition_payload(db, command))


async def build_stable_predicate_precondition_fingerprint(
    db,
    command: StablePredicateCommand,
    *,
    user: Any,
    credential_kind: str,
) -> str:
    validation_error = _validate_request(
        command,
        require_actor=False,
        require_precondition=False,
    )
    if validation_error:
        raise StablePredicateEvolutionError(validation_error)
    await _authorize_mutation(
        db,
        library_id=command.library_id,
        user=user,
        credential_kind=credential_kind,
    )
    return await _build_stable_predicate_precondition_fingerprint(db, command)


def _validate_request(
    command: StablePredicateCommand,
    *,
    require_actor: bool = True,
    require_precondition: bool = True,
) -> str | None:
    if not isinstance(command.library_id, uuid.UUID):
        return "invalid_envelope"
    if command.expected_predecessor_decision_id is not None and not isinstance(
        command.expected_predecessor_decision_id,
        uuid.UUID,
    ):
        return "invalid_envelope"
    if isinstance(command, StablePredicateMergeCommand):
        if len(command.source_predicate_ids) > 1024:
            return "payload_too_large"
        if not isinstance(command.survivor_predicate_id, uuid.UUID) or not all(
            isinstance(value, uuid.UUID) for value in command.source_predicate_ids
        ):
            return "invalid_envelope"
    elif isinstance(command, StablePredicateSplitCommand):
        if len(command.successor_slots) > 256 or len(command.mapping_assignments) > 4096:
            return "payload_too_large"
        if not isinstance(command.source_predicate_id, uuid.UUID) or not all(
            isinstance(slot.predicate_id, uuid.UUID) for slot in command.successor_slots
        ):
            return "invalid_envelope"
    elif isinstance(command, StablePredicateReassignCommand):
        if not all(
            isinstance(value, uuid.UUID)
            for value in (
                command.mapping_id,
                command.from_predicate_id,
                command.to_predicate_id,
            )
        ):
            return "invalid_envelope"
    else:
        return "invalid_envelope"
    try:
        command_payload = _command_payload(command)
    except StablePredicateEvolutionError as exc:
        payload_error = _payload_error(exc)
        return payload_error if payload_error == "payload_too_large" else "command_shape_invalid"
    if (
        not _valid_text(command.idempotency_key, scalars=256, utf8_bytes=1024)
        or command.requested_effect not in {"stage", "apply"}
    ):
        return "invalid_envelope"
    if not (
        _valid_token(command.reason_code)
        and _valid_token(command.method)
        and _valid_text(command.reason_text, scalars=512, utf8_bytes=2048)
        and _valid_request_id(command.request_id)
    ):
        return "invalid_envelope"
    if require_actor and (
        command.actor_type not in {"user", "service"}
        or not isinstance(command.actor_id, uuid.UUID)
    ):
        return "invalid_envelope"
    if not require_actor and (
        command.actor_type != "" or command.actor_id is not None
    ):
        return "invalid_envelope"
    if require_precondition and not _is_fingerprint(
        command.expected_precondition_fingerprint
    ):
        return "invalid_envelope"
    if command.confidence is not None and (
        not isinstance(command.confidence, Decimal)
        or not command.confidence.is_finite()
        or (command.confidence.is_zero() and command.confidence.is_signed())
        or command.confidence < 0
        or command.confidence > 1
        or max(0, -command.confidence.as_tuple().exponent) > 6
    ):
        return "invalid_envelope"
    try:
        if len(command.evidence_refs) > 256:
            return "payload_too_large"
        evidence = _normalized_evidence_refs(command.evidence_refs)
        if _canonical_json_size(evidence) > 262144:
            return "payload_too_large"
        if _canonical_json_size(command_payload) > 262144:
            return "payload_too_large"
    except (StablePredicateEvolutionError, TypeError):
        return "invalid_envelope"
    if isinstance(command, StablePredicateSplitCommand):
        mapping_ids = [entry.mapping_id for entry in command.mapping_assignments]
        if len(mapping_ids) != len(set(mapping_ids)):
            return "mapping_partition_invalid"
        if any(
            entry.state not in {"pending", "resolved"}
            or not isinstance(entry.mapping_id, uuid.UUID)
            or (entry.state == "pending" and entry.target_predicate_id is not None)
            or (entry.state == "resolved" and not isinstance(entry.target_predicate_id, uuid.UUID))
            for entry in command.mapping_assignments
        ):
            return "mapping_partition_invalid"
        slot_ids = {slot.predicate_id for slot in command.successor_slots}
        if any(
            entry.state == "resolved" and entry.target_predicate_id not in slot_ids
            for entry in command.mapping_assignments
        ):
            return "mapping_partition_invalid"
        try:
            for entry in command.mapping_assignments:
                if not _valid_token(entry.reason_code):
                    return "invalid_envelope"
                if not isinstance(entry.partition_basis_snapshot, Mapping):
                    return "invalid_envelope"
                if _canonical_json_size(entry.partition_basis_snapshot) > 65536:
                    return "payload_too_large"
        except StablePredicateEvolutionError as exc:
            if "nesting exceeds" in str(exc):
                return "payload_too_large"
            return "invalid_envelope"
    return None


def _policy_snapshot(predicate: StablePredicateIdentity) -> dict[str, Any]:
    return {
        "contract_version": predicate.contract_version,
        "identity_policy_version": predicate.identity_policy_version,
        "key": predicate.key,
        "namespace": predicate.namespace,
        "predicate_id": predicate.id,
        "resolution_policy": dict(predicate.resolution_policy or {}),
        "temporal_class": predicate.temporal_class,
    }


def _slot_snapshot(slot: StablePredicateSuccessorSlot) -> dict[str, Any]:
    if slot.kind == "existing":
        return {"kind": "existing", "predicate_id": slot.predicate_id}
    assert slot.target_spec is not None
    return {
        "kind": "new",
        "predicate_id": slot.predicate_id,
        "target_spec_fingerprint": stable_predicate_target_fingerprint(slot.target_spec),
        "target_spec_snapshot": _target_payload(slot.target_spec),
    }


async def _operation_payload_for_command(
    db, command: StablePredicateCommand
) -> tuple[dict[str, Any] | None, list[StablePredicateMapping], str | None]:
    predicates = {
        row.id: row
        for row in await _scoped_rows(db, StablePredicateIdentity, command.library_id)
    }
    if isinstance(command, StablePredicateMergeCommand):
        mappings = await _active_mappings(db, command.library_id, set(command.source_predicate_ids))
        payload = {
            "mapping_assignments": [
                {
                    "from_predicate_id": mapping.stable_predicate_identity_id,
                    "mapping_id": mapping.id,
                    "partition_basis_snapshot": {},
                    "reason_code": "merge_survivor",
                    "state": "resolved",
                    "target_ref": {
                        "kind": "existing",
                        "predicate_id": command.survivor_predicate_id,
                    },
                }
                for mapping in mappings
            ],
            "policy_compatibility": [
                _policy_snapshot(predicates[predicate_id])
                for predicate_id in sorted(
                    {*command.source_predicate_ids, command.survivor_predicate_id}, key=str
                )
                if predicate_id in predicates
            ],
            "survivor_predicate_id": command.survivor_predicate_id,
        }
        return payload, mappings, None
    if isinstance(command, StablePredicateSplitCommand):
        mappings = await _active_mappings(db, command.library_id, {command.source_predicate_id})
        entries = {entry.mapping_id: entry for entry in command.mapping_assignments}
        if set(entries) != {mapping.id for mapping in mappings}:
            return None, mappings, "predicate_mapping_partition_incomplete"
        slots = {slot.predicate_id: slot for slot in command.successor_slots}
        assignment_payload = []
        for mapping in mappings:
            entry = entries[mapping.id]
            target_ref = None
            if entry.state == "resolved":
                slot = slots.get(entry.target_predicate_id)
                if slot is None:
                    return None, mappings, "predicate_mapping_partition_target_invalid"
                target_ref = {"kind": slot.kind, "predicate_id": slot.predicate_id}
            assignment_payload.append(
                {
                    "from_predicate_id": command.source_predicate_id,
                    "mapping_id": mapping.id,
                    "partition_basis_snapshot": dict(entry.partition_basis_snapshot),
                    "reason_code": entry.reason_code,
                    "state": entry.state,
                    "target_ref": target_ref,
                }
            )
        return {
            "mapping_assignments": assignment_payload,
            "successor_slots": [_slot_snapshot(slot) for slot in command.successor_slots],
        }, mappings, None
    mappings = [
        row
        for row in await _scoped_rows(db, StablePredicateMapping, command.library_id)
        if row.id == command.mapping_id
    ]
    if len(mappings) != 1:
        return None, mappings, "predicate_mapping_not_found"
    mapping = mappings[0]
    return {
        "mapping_assignment": {
            "from_predicate_id": command.from_predicate_id,
            "mapping_id": command.mapping_id,
            "partition_basis_snapshot": {},
            "reason_code": command.reason_code,
            "state": "resolved",
            "target_ref": {"kind": "existing", "predicate_id": command.to_predicate_id},
        }
    }, mappings, None


async def _lock_command_scopes(db, command: StablePredicateCommand) -> None:
    predicate_ids = (
        set(_source_predicate_ids(command))
        | set(_existing_target_predicate_ids(command))
        | {slot.predicate_id for slot in _new_target_slots(command)}
    )
    await lock_graph_identity_scopes(
        db,
        command.library_id,
        tuple(
            GraphIdentityLockScope(STABLE_PREDICATE_LOCK_SCOPE, predicate_id)
            for predicate_id in predicate_ids
        ),
        wait=False,
    )
    if isinstance(command, StablePredicateReassignCommand):
        mapping_ids = {command.mapping_id}
    else:
        mapping_ids = {
            row.id
            for row in await _active_mappings(
                db,
                command.library_id,
                set(_source_predicate_ids(command)),
            )
        }
    if mapping_ids:
        await db.execute(
            select(StablePredicateMapping)
            .where(
                StablePredicateMapping.library_id == command.library_id,
                StablePredicateMapping.id.in_(mapping_ids),
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        await db.execute(
            select(StablePredicateMappingEvolutionAssignment)
            .where(
                StablePredicateMappingEvolutionAssignment.library_id
                == command.library_id,
                StablePredicateMappingEvolutionAssignment.old_mapping_id.in_(
                    mapping_ids
                ),
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    source_ids = set(_source_predicate_ids(command))
    if source_ids:
        await db.execute(
            select(StablePredicateEvolutionSource)
            .where(
                StablePredicateEvolutionSource.library_id == command.library_id,
                StablePredicateEvolutionSource.source_predicate_id.in_(source_ids),
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )


async def _root_slot_occupied(
    db, command: StablePredicateCommand
) -> bool:
    live_sources = [
        row
        for row in await _scoped_rows(db, StablePredicateEvolutionSource, command.library_id)
        if row.evolution_status in {"pending", "applied", "historical_only"}
    ]
    source_ids = set(_source_predicate_ids(command))
    if isinstance(command, StablePredicateReassignCommand):
        source_ids.add(command.to_predicate_id)
    if any(row.source_predicate_id in source_ids for row in live_sources):
        return True
    live_assignments = [
        row
        for row in await _scoped_rows(
            db, StablePredicateMappingEvolutionAssignment, command.library_id
        )
        if row.assignment_state in {"pending", "resolved"}
    ]
    if isinstance(command, StablePredicateReassignCommand):
        mapping_ids = {command.mapping_id}
    else:
        mapping_ids = {
            row.id
            for row in await _active_mappings(
                db, command.library_id, set(_source_predicate_ids(command))
            )
        }
    return any(row.old_mapping_id in mapping_ids for row in live_assignments)


async def _has_lineage_cycle(
    db, library_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> bool:
    sources = {
        row.id: row
        for row in await _scoped_rows(db, StablePredicateEvolutionSource, library_id)
        if row.evolution_status == PREDICATE_EVOLUTION_SOURCE_APPLIED
    }
    successors = await _scoped_rows(db, StablePredicateEvolutionSuccessor, library_id)
    graph: dict[uuid.UUID, set[uuid.UUID]] = {}
    for successor in successors:
        source = sources.get(successor.source_transition_id)
        if source is not None and successor.target_predicate_id is not None:
            graph.setdefault(source.source_predicate_id, set()).add(successor.target_predicate_id)
    pending = [target_id]
    visited: set[uuid.UUID] = set()
    while pending:
        current = pending.pop()
        if current == source_id:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(graph.get(current, ()))
    return False


async def _admission_error(
    db,
    command: StablePredicateCommand,
    *,
    pending_predecessor_id: uuid.UUID | None = None,
) -> str | None:
    predicates = {
        row.id: row
        for row in await _scoped_rows(db, StablePredicateIdentity, command.library_id)
    }
    existing_ids = set(_source_predicate_ids(command)) | set(_existing_target_predicate_ids(command))
    if any(predicate_id not in predicates for predicate_id in existing_ids):
        return "predicate_scope_mismatch"
    pending_sources = {
        row.source_predicate_id
        for row in await _scoped_rows(db, StablePredicateEvolutionSource, command.library_id)
        if row.evolution_decision_id == pending_predecessor_id
        and row.evolution_status == PREDICATE_EVOLUTION_SOURCE_PENDING
    }
    for predicate_id in existing_ids:
        if predicate_id in pending_sources:
            continue
        current = await resolve_current_stable_predicate_identity(
            db, command.library_id, predicate_id
        )
        if current.status != "resolved" or current.current_predicate_id != predicate_id:
            return "predicate_current_identity_changed"
    for slot in _new_target_slots(command):
        assert slot.target_spec is not None
        try:
            policy = parse_stable_predicate_resolution_policy(slot.target_spec.resolution_policy)
        except (TypeError, ValueError):
            return "new_target_policy_invalid"
        if policy.temporal_class != slot.target_spec.temporal_class:
            return "new_target_policy_invalid"
        occupant = await db.get(StablePredicateIdentity, slot.predicate_id)
        if occupant is not None:
            return "target_predicate_uuid_collision"
        if any(
            (row.namespace, row.key, row.contract_version)
            == (
                slot.target_spec.namespace,
                slot.target_spec.key,
                slot.target_spec.contract_version,
            )
            for row in predicates.values()
        ):
            return "new_target_identity_already_exists"
    for source_id in _source_predicate_ids(command):
        for target_id in (
            set(_existing_target_predicate_ids(command))
            | {slot.predicate_id for slot in _new_target_slots(command)}
        ):
            if await _has_lineage_cycle(db, command.library_id, source_id, target_id):
                return "predicate_evolution_cycle"
    if isinstance(command, StablePredicateReassignCommand):
        mapping = await db.get(StablePredicateMapping, command.mapping_id)
        target = predicates.get(command.to_predicate_id)
        if (
            mapping is None
            or mapping.library_id != command.library_id
            or mapping.mapping_status != "active"
            or mapping.stable_predicate_identity_id != command.from_predicate_id
        ):
            return "predicate_mapping_current_identity_changed"
        if (
            target is None
            or target.resolution_status != "resolved"
            or not isinstance(target.resolution_policy, Mapping)
        ):
            return "predicate_not_ready"
    return None


async def resolve_current_stable_predicate_identity(
    db,
    library_id: uuid.UUID,
    predicate_id: uuid.UUID,
    context: StablePredicateEvolutionContext | None = None,
) -> CurrentStablePredicateResult:
    """Follow only persisted Predicate and mapping lineage, failing closed on corruption."""

    if not isinstance(library_id, uuid.UUID) or not isinstance(predicate_id, uuid.UUID):
        raise StablePredicateEvolutionError("library and predicate identifiers must be UUIDs")
    context = context or StablePredicateEvolutionContext()
    predicates = {
        row.id: row for row in await _scoped_rows(db, StablePredicateIdentity, library_id)
    }
    mappings = {
        row.id: row for row in await _scoped_rows(db, StablePredicateMapping, library_id)
    }
    decisions = {
        row.id: row
        for row in await _scoped_rows(db, StablePredicateEvolutionDecision, library_id)
    }
    sources = await _scoped_rows(db, StablePredicateEvolutionSource, library_id)
    successors = await _scoped_rows(db, StablePredicateEvolutionSuccessor, library_id)
    assignments = await _scoped_rows(
        db, StablePredicateMappingEvolutionAssignment, library_id
    )
    lineage: list[uuid.UUID] = []

    def result(
        status: str,
        current_predicate: uuid.UUID | None,
        current_mapping: uuid.UUID | None,
        reason_code: str | None = None,
    ) -> CurrentStablePredicateResult:
        return CurrentStablePredicateResult(
            status,
            predicate_id,
            current_predicate,
            current_mapping,
            tuple(lineage),
            reason_code,
        )

    if predicate_id not in predicates:
        return result("pending", None, None, "predicate_scope_mismatch")
    current_predicate_id = predicate_id
    current_mapping_id = context.mapping_id
    visited_predicates: set[uuid.UUID] = set()
    visited_mappings: set[uuid.UUID] = set()

    while True:
        if current_predicate_id in visited_predicates:
            return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
        visited_predicates.add(current_predicate_id)
        current_mapping = None
        if current_mapping_id is not None:
            if current_mapping_id in visited_mappings:
                return result("pending", None, None, "predicate_evolution_integrity")
            visited_mappings.add(current_mapping_id)
            current_mapping = mappings.get(current_mapping_id)
            if (
                current_mapping is None
                or current_mapping.stable_predicate_identity_id != current_predicate_id
                or (
                    context.relation_type_id is not None
                    and current_mapping.relation_type_id != context.relation_type_id
                )
            ):
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")

        live_sources = [
            row
            for row in sources
            if row.source_predicate_id == current_predicate_id
            and row.evolution_status in {"pending", "applied", "historical_only"}
        ]
        if len(live_sources) > 1:
            return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
        source = live_sources[0] if live_sources else None
        if source is not None:
            decision = decisions.get(source.evolution_decision_id)
            if decision is None:
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
            source_successors = [
                row for row in successors if row.source_transition_id == source.id
            ]
            if source.evolution_status == "pending":
                reason = (
                    "predicate_split_partition_incomplete"
                    if decision.operation_kind == "split"
                    and any(
                        row.evolution_decision_id == decision.id
                        and row.assignment_state == "pending"
                        for row in assignments
                    )
                    else "predicate_evolution_pending"
                )
                return result("pending", None, current_mapping_id, reason)
            if source.evolution_status == "historical_only":
                if source_successors:
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                return result("historical_only", None, current_mapping_id)
            if decision.lifecycle_status != "applied":
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
            lineage.append(decision.id)
            target_ids = [row.target_predicate_id for row in source_successors]
            if any(target is None or target not in predicates for target in target_ids):
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
            if decision.operation_kind == "merge" and len(target_ids) == 1:
                target_id = target_ids[0]
            elif decision.operation_kind == "split" and len(target_ids) >= 2:
                if any(
                    row.evolution_decision_id == decision.id
                    and row.assignment_state == "pending"
                    for row in assignments
                ):
                    return result(
                        "pending",
                        None,
                        current_mapping_id,
                        "predicate_split_partition_incomplete",
                    )
                if current_mapping_id is None:
                    return result("forked", None, None, "predicate_evolution_forked")
                target_id = None
            else:
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
            if current_mapping_id is not None:
                matches = [
                    row
                    for row in assignments
                    if row.source_transition_id == source.id
                    and row.old_mapping_id == current_mapping_id
                ]
                if len(matches) != 1:
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                assignment = matches[0]
                successor_ids = {row.id for row in source_successors}
                if (
                    assignment.evolution_decision_id != decision.id
                    or assignment.assignment_state != "resolved"
                    or assignment.target_predicate_id not in target_ids
                    or assignment.target_successor_id not in successor_ids
                    or assignment.new_mapping_id is None
                    or current_mapping.mapping_status != "superseded"
                ):
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                target_id = assignment.target_predicate_id
                replacement = mappings.get(assignment.new_mapping_id)
                if (
                    replacement is None
                    or replacement.stable_predicate_identity_id != target_id
                    or replacement.relation_type_id != current_mapping.relation_type_id
                    or replacement.mapping_status not in {"active", "superseded"}
                    or replacement.evolution_assignment_id != assignment.id
                ):
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                current_mapping_id = replacement.id
            current_predicate_id = target_id
            continue

        if current_mapping_id is not None:
            mapping_assignments = [
                row
                for row in assignments
                if row.old_mapping_id == current_mapping_id
                and row.assignment_state in {"pending", "resolved"}
            ]
            if len(mapping_assignments) > 1:
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
            if mapping_assignments:
                assignment = mapping_assignments[0]
                decision = decisions.get(assignment.evolution_decision_id)
                if (
                    decision is None
                    or decision.operation_kind != "reassign"
                    or assignment.source_transition_id is not None
                    or assignment.source_predicate_id != current_predicate_id
                ):
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                if decision.lifecycle_status == "pending":
                    return result(
                        "pending",
                        None,
                        current_mapping_id,
                        "predicate_mapping_reassignment_pending",
                    )
                if (
                    decision.lifecycle_status != "applied"
                    or assignment.assignment_state != "resolved"
                    or assignment.target_predicate_id is None
                    or assignment.new_mapping_id is None
                    or current_mapping.mapping_status != "superseded"
                ):
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                replacement = mappings.get(assignment.new_mapping_id)
                if (
                    replacement is None
                    or replacement.stable_predicate_identity_id
                    != assignment.target_predicate_id
                    or replacement.relation_type_id != current_mapping.relation_type_id
                    or replacement.mapping_status not in {"active", "superseded"}
                    or replacement.evolution_assignment_id != assignment.id
                ):
                    return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
                lineage.append(decision.id)
                current_mapping_id = replacement.id
                current_predicate_id = replacement.stable_predicate_identity_id
                continue
            if current_mapping.mapping_status != "active":
                return result("pending", None, current_mapping_id, "predicate_evolution_integrity")
        return result("resolved", current_predicate_id, current_mapping_id)


def _new_root(
    command: StablePredicateCommand, fingerprint: str
) -> StablePredicateEvolutionCommand:
    return StablePredicateEvolutionCommand(
        id=uuid.uuid4(),
        library_id=command.library_id,
        idempotency_key=command.idempotency_key,
        command_identity_fingerprint=fingerprint,
        operation_kind=_operation(command),
        command_payload_snapshot=_stable_value(_command_payload(command)),
        contract_version=_CONTRACT_VERSION,
    )


def _command_from_persisted_decision(
    root: StablePredicateEvolutionCommand,
    decision: StablePredicateEvolutionDecision,
) -> StablePredicateCommand:
    command_payload = root.command_payload_snapshot
    operation_payload = decision.operation_payload_snapshot
    common = {
        "idempotency_key": root.idempotency_key,
        "requested_effect": decision.requested_effect,
        "reason_code": decision.reason_code,
        "reason_text": decision.reason_text,
        "method": decision.method,
        "evidence_refs": tuple(decision.evidence_refs),
        "request_id": decision.request_id,
        "expected_precondition_fingerprint": decision.observed_precondition_fingerprint,
        "expected_predecessor_decision_id": decision.supersedes_decision_id,
        "confidence": decision.confidence,
    }
    try:
        if root.operation_kind == "merge":
            return StablePredicateMergeCommand(
                root.library_id,
                tuple(
                    uuid.UUID(value)
                    for value in command_payload["source_predicate_ids"]
                ),
                uuid.UUID(command_payload["survivor_predicate_id"]),
                **common,
            )
        if root.operation_kind == "split":
            slots = []
            for value in operation_payload["successor_slots"]:
                predicate_id = uuid.UUID(value["predicate_id"])
                if value["kind"] == "existing":
                    slots.append(StablePredicateSuccessorSlot.existing(predicate_id))
                    continue
                target = value["target_spec_snapshot"]
                spec = StablePredicateTargetSpec(
                    namespace=target["namespace"],
                    key=target["key"],
                    contract_version=target["contract_version"],
                    temporal_class=target["temporal_class"],
                    identity_policy_version=target["identity_policy_version"],
                    resolution_policy=target["resolution_policy"],
                )
                slots.append(StablePredicateSuccessorSlot.new(predicate_id, spec))
            partitions = tuple(
                StablePredicateMappingPartition(
                    mapping_id=uuid.UUID(value["mapping_id"]),
                    state=value["state"],
                    target_predicate_id=(
                        uuid.UUID(value["target_ref"]["predicate_id"])
                        if value["target_ref"] is not None
                        else None
                    ),
                    partition_basis_snapshot=value["partition_basis_snapshot"],
                    reason_code=value["reason_code"],
                )
                for value in operation_payload["mapping_assignments"]
            )
            return StablePredicateSplitCommand(
                root.library_id,
                uuid.UUID(command_payload["source_predicate_id"]),
                tuple(slots),
                mapping_assignments=partitions,
                **common,
            )
        if root.operation_kind == "reassign":
            return StablePredicateReassignCommand(
                root.library_id,
                uuid.UUID(command_payload["mapping_id"]),
                uuid.UUID(command_payload["from_predicate_id"]),
                uuid.UUID(command_payload["to_predicate_id"]),
                **common,
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise StablePredicateEvolutionError("persisted_command_invalid") from exc
    raise StablePredicateEvolutionError("persisted_command_invalid")


def _new_decision(
    root: StablePredicateEvolutionCommand,
    command: StablePredicateCommand,
    operation_payload: Mapping[str, Any],
    outcome: str,
    observed_precondition: str,
    *,
    supersedes: uuid.UUID | None,
    reason_code: str | None = None,
) -> StablePredicateEvolutionDecision:
    return StablePredicateEvolutionDecision(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        decision_payload_fingerprint=stable_predicate_decision_fingerprint(
            command, operation_payload
        ),
        operation_kind=root.operation_kind,
        requested_effect=command.requested_effect,
        evaluated_outcome=outcome,
        lifecycle_status=outcome,
        operation_payload_snapshot=_stable_value(dict(operation_payload)),
        reason_code=reason_code or command.reason_code,
        reason_text=command.reason_text,
        method=command.method,
        confidence=command.confidence,
        evidence_refs=_stable_value(_normalized_evidence_refs(command.evidence_refs)),
        expected_precondition_fingerprint=command.expected_precondition_fingerprint,
        observed_precondition_fingerprint=observed_precondition,
        supersedes_decision_id=supersedes,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        request_id=command.request_id,
    )


async def _replay(
    db,
    root: StablePredicateEvolutionCommand,
    command: StablePredicateCommand,
) -> StablePredicateEvolutionResult | None:
    rows = await _decision_rows(db, root.library_id, root.id)
    head = _current_decision(rows)
    for decision in rows:
        if decision.requested_effect == "cancel":
            continue
        if hmac.compare_digest(
            decision.decision_payload_fingerprint,
            stable_predicate_decision_fingerprint(
                command, decision.operation_payload_snapshot
            ),
        ):
            return StablePredicateEvolutionResult(
                "REUSED",
                command=root,
                decision=decision,
                reused_decision_id=decision.id,
                effective_outcome=(
                    "STALE"
                    if decision.evaluated_outcome == "stale"
                    else decision.evaluated_outcome.upper()
                ),
                current_decision_id=head.id if head else None,
                current_decision_status=head.lifecycle_status if head else None,
            )
    return None


async def _supersede_predecessor(
    db, predecessor: StablePredicateEvolutionDecision
) -> tuple[
    dict[uuid.UUID, StablePredicateEvolutionSource],
    dict[uuid.UUID, StablePredicateMappingEvolutionAssignment],
]:
    source_rows = (
        await db.execute(
            select(StablePredicateEvolutionSource)
            .where(
                StablePredicateEvolutionSource.library_id == predecessor.library_id,
                StablePredicateEvolutionSource.command_id == predecessor.command_id,
                StablePredicateEvolutionSource.evolution_decision_id == predecessor.id,
                StablePredicateEvolutionSource.evolution_status
                == PREDICATE_EVOLUTION_SOURCE_PENDING,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    sources = {
        row.source_predicate_id: row
        for row in source_rows
        if row.evolution_decision_id == predecessor.id
        and row.evolution_status == PREDICATE_EVOLUTION_SOURCE_PENDING
    }
    assignment_rows = (
        await db.execute(
            select(StablePredicateMappingEvolutionAssignment)
            .where(
                StablePredicateMappingEvolutionAssignment.library_id
                == predecessor.library_id,
                StablePredicateMappingEvolutionAssignment.command_id
                == predecessor.command_id,
                StablePredicateMappingEvolutionAssignment.evolution_decision_id
                == predecessor.id,
                StablePredicateMappingEvolutionAssignment.assignment_state.in_(
                    {
                        PREDICATE_EVOLUTION_ASSIGNMENT_PENDING,
                        PREDICATE_EVOLUTION_ASSIGNMENT_RESOLVED,
                    }
                ),
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assignments = {
        row.old_mapping_id: row
        for row in assignment_rows
        if row.evolution_decision_id == predecessor.id
        and row.assignment_state
        in {PREDICATE_EVOLUTION_ASSIGNMENT_PENDING, PREDICATE_EVOLUTION_ASSIGNMENT_RESOLVED}
    }
    predecessor.lifecycle_status = PREDICATE_EVOLUTION_DECISION_SUPERSEDED
    for source in sources.values():
        source.evolution_status = PREDICATE_EVOLUTION_SOURCE_SUPERSEDED
    for assignment in assignments.values():
        assignment.assignment_state = PREDICATE_EVOLUTION_ASSIGNMENT_SUPERSEDED
    await db.flush()
    return sources, assignments


def _policies_compatible(operation_payload: Mapping[str, Any]) -> bool:
    policies = list(operation_payload.get("policy_compatibility", ()))
    if not policies:
        return False
    expected = (
        policies[0]["temporal_class"],
        policies[0]["identity_policy_version"],
        _jcs(policies[0]["resolution_policy"]),
    )
    return all(
        (
            row["temporal_class"],
            row["identity_policy_version"],
            _jcs(row["resolution_policy"]),
        )
        == expected
        for row in policies[1:]
    )


def _new_source_transition(
    root: StablePredicateEvolutionCommand,
    decision: StablePredicateEvolutionDecision,
    source_predicate_id: uuid.UUID,
    status: str,
    predecessor: StablePredicateEvolutionSource | None,
) -> StablePredicateEvolutionSource:
    return StablePredicateEvolutionSource(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        evolution_decision_id=decision.id,
        source_predicate_id=source_predicate_id,
        supersedes_source_transition_id=predecessor.id if predecessor else None,
        evolution_status=status,
    )


def _new_successor(
    root: StablePredicateEvolutionCommand,
    decision: StablePredicateEvolutionDecision,
    source: StablePredicateEvolutionSource,
    slot: StablePredicateSuccessorSlot,
    *,
    applied: bool,
) -> StablePredicateEvolutionSuccessor:
    snapshot = _target_payload(slot.target_spec) if slot.target_spec else None
    return StablePredicateEvolutionSuccessor(
        id=uuid.uuid4(),
        library_id=root.library_id,
        command_id=root.id,
        evolution_decision_id=decision.id,
        source_transition_id=source.id,
        source_predicate_id=source.source_predicate_id,
        target_ref_kind=slot.kind,
        planned_target_predicate_id=slot.predicate_id,
        target_predicate_id=(slot.predicate_id if applied or slot.kind == "existing" else None),
        target_spec_fingerprint=(
            stable_predicate_target_fingerprint(slot.target_spec)
            if slot.target_spec
            else None
        ),
        target_spec_snapshot=_stable_value(snapshot) if snapshot else None,
    )


def _mapping_partition_by_id(
    command: StablePredicateCommand,
    mappings: list[StablePredicateMapping],
) -> dict[uuid.UUID, StablePredicateMappingPartition]:
    if isinstance(command, StablePredicateSplitCommand):
        return {entry.mapping_id: entry for entry in command.mapping_assignments}
    target_id = (
        command.survivor_predicate_id
        if isinstance(command, StablePredicateMergeCommand)
        else command.to_predicate_id
    )
    return {
        mapping.id: StablePredicateMappingPartition(
            mapping.id,
            "resolved",
            target_id,
            {},
            "merge_survivor" if isinstance(command, StablePredicateMergeCommand) else command.reason_code,
        )
        for mapping in mappings
    }


async def _create_children_and_effects(
    db,
    root: StablePredicateEvolutionCommand,
    decision: StablePredicateEvolutionDecision,
    command: StablePredicateCommand,
    mappings: list[StablePredicateMapping],
    *,
    applied: bool,
    previous_sources: dict[uuid.UUID, StablePredicateEvolutionSource],
    previous_assignments: dict[uuid.UUID, StablePredicateMappingEvolutionAssignment],
) -> tuple[
    tuple[StablePredicateEvolutionSource, ...],
    tuple[StablePredicateEvolutionSuccessor, ...],
    tuple[StablePredicateMappingEvolutionAssignment, ...],
]:
    if isinstance(command, StablePredicateSplitCommand) and applied:
        for slot in _new_target_slots(command):
            assert slot.target_spec is not None
            target = StablePredicateIdentity(
                id=slot.predicate_id,
                library_id=command.library_id,
                namespace=slot.target_spec.namespace,
                key=slot.target_spec.key,
                contract_version=slot.target_spec.contract_version,
                temporal_class=slot.target_spec.temporal_class,
                identity_policy_version=slot.target_spec.identity_policy_version,
                resolution_status="resolved",
                resolution_policy=_stable_value(dict(slot.target_spec.resolution_policy)),
            )
            db.add(target)
        await db.flush()

    sources: list[StablePredicateEvolutionSource] = []
    successors: list[StablePredicateEvolutionSuccessor] = []
    if isinstance(command, (StablePredicateMergeCommand, StablePredicateSplitCommand)):
        source_ids = _source_predicate_ids(command)
        slots = (
            tuple(
                StablePredicateSuccessorSlot.existing(command.survivor_predicate_id)
                for _source_id in source_ids
            )
            if isinstance(command, StablePredicateMergeCommand)
            else command.successor_slots
        )
        for source_id in source_ids:
            source = _new_source_transition(
                root,
                decision,
                source_id,
                PREDICATE_EVOLUTION_SOURCE_APPLIED
                if applied
                else PREDICATE_EVOLUTION_SOURCE_PENDING,
                previous_sources.get(source_id),
            )
            sources.append(source)
            db.add(source)
            source_slots = slots if isinstance(command, StablePredicateSplitCommand) else slots[:1]
            for slot in source_slots:
                successor = _new_successor(root, decision, source, slot, applied=applied)
                successors.append(successor)
                db.add(successor)

    partitions = _mapping_partition_by_id(command, mappings)
    assignments: list[StablePredicateMappingEvolutionAssignment] = []
    successor_by_target = {row.planned_target_predicate_id: row for row in successors}
    for mapping in mappings:
        partition = partitions[mapping.id]
        target_id = partition.target_predicate_id
        target_successor = successor_by_target.get(target_id)
        materialized_target_id = (
            target_successor.target_predicate_id
            if target_successor is not None
            else target_id
        )
        new_mapping_id = uuid.uuid4() if applied else None
        assignment = StablePredicateMappingEvolutionAssignment(
            id=uuid.uuid4(),
            library_id=root.library_id,
            command_id=root.id,
            evolution_decision_id=decision.id,
            source_transition_id=(
                sources[0].id
                if isinstance(command, StablePredicateSplitCommand)
                else next(
                    (
                        row.id
                        for row in sources
                        if row.source_predicate_id == mapping.stable_predicate_identity_id
                    ),
                    None,
                )
            ),
            old_mapping_id=mapping.id,
            relation_type_id=mapping.relation_type_id,
            source_predicate_id=mapping.stable_predicate_identity_id,
            target_successor_id=(
                target_successor.id if target_successor is not None else None
            ),
            target_predicate_id=materialized_target_id,
            new_mapping_id=new_mapping_id,
            assignment_state=partition.state,
            partition_basis_snapshot=_stable_value(dict(partition.partition_basis_snapshot)),
            reason_code=partition.reason_code,
            supersedes_assignment_id=(
                previous_assignments[mapping.id].id
                if mapping.id in previous_assignments
                else None
            ),
        )
        assignments.append(assignment)
        db.add(assignment)
        if applied:
            mapping.mapping_status = "superseded"
            mapping.superseded_at = datetime.now(timezone.utc)
    await db.flush()
    if applied:
        for mapping, assignment in zip(mappings, assignments, strict=True):
            replacement = StablePredicateMapping(
                id=assignment.new_mapping_id,
                library_id=mapping.library_id,
                stable_predicate_identity_id=assignment.target_predicate_id,
                relation_type_id=mapping.relation_type_id,
                mapping_status="active",
                evolution_assignment_id=assignment.id,
            )
            db.add(replacement)
        await db.flush()
    return tuple(sources), tuple(successors), tuple(assignments)


async def _apply_stable_predicate_evolution(
    db, command: StablePredicateCommand
) -> StablePredicateEvolutionResult:
    """Persist one P3.2 decision while leaving commit and rollback to the caller."""

    if not isinstance(
        command,
        (StablePredicateMergeCommand, StablePredicateSplitCommand, StablePredicateReassignCommand),
    ):
        raise StablePredicateEvolutionError("unsupported stable predicate evolution command")
    validation_error = _validate_request(command)
    if validation_error:
        return StablePredicateEvolutionResult("REJECTED", validation_error)
    command_fingerprint = stable_predicate_command_fingerprint(command)
    root = await _root_by_key(db, command.library_id, command.idempotency_key)
    if root and root.command_identity_fingerprint != command_fingerprint:
        return StablePredicateEvolutionResult("REJECTED", "idempotency_key_conflict", root)
    if root is None:
        alias = await _root_by_identity(db, command.library_id, command_fingerprint)
        if alias is not None:
            return StablePredicateEvolutionResult("REJECTED", "command_identity_alias_key", alias)
    else:
        replay = await _replay(db, root, command)
        if replay is not None:
            return replay
    await _lock_command_scopes(db, command)
    root = await _root_by_key(db, command.library_id, command.idempotency_key)
    if root and root.command_identity_fingerprint != command_fingerprint:
        return StablePredicateEvolutionResult("REJECTED", "idempotency_key_conflict", root)
    if root is None:
        alias = await _root_by_identity(db, command.library_id, command_fingerprint)
        if alias is not None:
            return StablePredicateEvolutionResult("REJECTED", "command_identity_alias_key", alias)
    else:
        replay = await _replay(db, root, command)
        if replay is not None:
            return replay

    predecessor = None
    if root is not None:
        predecessor = _current_decision(
            await _decision_rows(db, command.library_id, root.id)
        )
        if predecessor is None:
            return StablePredicateEvolutionResult("REJECTED", "command_unusable", root)
        if command.expected_predecessor_decision_id != predecessor.id:
            return StablePredicateEvolutionResult(
                "STALE_OPERATION",
                "expected_predecessor_mismatch",
                root,
                current_decision_id=predecessor.id,
                current_decision_status=predecessor.lifecycle_status,
            )
        if predecessor.lifecycle_status in {
            PREDICATE_EVOLUTION_DECISION_APPLIED,
            PREDICATE_EVOLUTION_DECISION_CANCELLED,
        }:
            return StablePredicateEvolutionResult("REJECTED", "command_closed", root)
    elif command.expected_predecessor_decision_id is not None:
        return StablePredicateEvolutionResult("STALE_OPERATION", "expected_predecessor_mismatch")

    admission_error = await _admission_error(
        db,
        command,
        pending_predecessor_id=(
            predecessor.id
            if predecessor is not None
            and predecessor.lifecycle_status == PREDICATE_EVOLUTION_DECISION_PENDING
            else None
        ),
    )
    if admission_error:
        return StablePredicateEvolutionResult("STALE_OPERATION", admission_error, root)
    if root is None and await _root_slot_occupied(db, command):
        return StablePredicateEvolutionResult(
            "STALE_OPERATION", "source_or_mapping_slot_occupied"
        )
    operation_payload, mappings, payload_error = await _operation_payload_for_command(
        db, command
    )
    if payload_error:
        return StablePredicateEvolutionResult("REJECTED", payload_error, root)
    assert operation_payload is not None
    decision_payload_error = _validate_decision_payload(command, operation_payload)
    if decision_payload_error:
        return StablePredicateEvolutionResult("REJECTED", decision_payload_error, root)
    observed = await _build_stable_predicate_precondition_fingerprint(db, command)
    if command.expected_precondition_fingerprint != observed:
        if (
            predecessor is not None
            and predecessor.lifecycle_status == PREDICATE_EVOLUTION_DECISION_PENDING
        ):
            return StablePredicateEvolutionResult(
                "STALE_OPERATION",
                "precondition_changed",
                root,
                current_decision_id=predecessor.id,
                current_decision_status=predecessor.lifecycle_status,
            )
        outcome = PREDICATE_EVOLUTION_DECISION_STALE
        reason_code = "precondition_changed"
    elif isinstance(command, StablePredicateMergeCommand) and not _policies_compatible(
        operation_payload
    ):
        outcome = PREDICATE_EVOLUTION_DECISION_REJECTED
        reason_code = "predicate_policy_incompatible"
    elif command.requested_effect == "stage" or (
        isinstance(command, StablePredicateSplitCommand)
        and any(entry.state == "pending" for entry in command.mapping_assignments)
    ):
        outcome = PREDICATE_EVOLUTION_DECISION_PENDING
        reason_code = command.reason_code
    else:
        outcome = PREDICATE_EVOLUTION_DECISION_APPLIED
        reason_code = command.reason_code
    if (
        predecessor is not None
        and predecessor.lifecycle_status == PREDICATE_EVOLUTION_DECISION_PENDING
        and outcome in {
            PREDICATE_EVOLUTION_DECISION_REJECTED,
            PREDICATE_EVOLUTION_DECISION_STALE,
        }
    ):
        return StablePredicateEvolutionResult(
            "REJECTED" if outcome == PREDICATE_EVOLUTION_DECISION_REJECTED else "STALE_OPERATION",
            reason_code,
            root,
            current_decision_id=predecessor.id,
            current_decision_status=predecessor.lifecycle_status,
        )
    if root is None:
        root = _new_root(command, command_fingerprint)
        db.add(root)
        await db.flush()
    previous_sources: dict[uuid.UUID, StablePredicateEvolutionSource] = {}
    previous_assignments: dict[uuid.UUID, StablePredicateMappingEvolutionAssignment] = {}
    if predecessor is not None:
        previous_sources, previous_assignments = await _supersede_predecessor(
            db, predecessor
        )
    decision = _new_decision(
        root,
        command,
        operation_payload,
        outcome,
        observed,
        supersedes=predecessor.id if predecessor else None,
        reason_code=reason_code,
    )
    db.add(decision)
    await db.flush()
    sources: tuple[StablePredicateEvolutionSource, ...] = ()
    successors: tuple[StablePredicateEvolutionSuccessor, ...] = ()
    assignments: tuple[StablePredicateMappingEvolutionAssignment, ...] = ()
    if outcome in {
        PREDICATE_EVOLUTION_DECISION_PENDING,
        PREDICATE_EVOLUTION_DECISION_APPLIED,
    }:
        try:
            sources, successors, assignments = await _create_children_and_effects(
                db,
                root,
                decision,
                command,
                mappings,
                applied=outcome == PREDICATE_EVOLUTION_DECISION_APPLIED,
                previous_sources=previous_sources,
                previous_assignments=previous_assignments,
            )
        except IntegrityError as exc:
            raise StablePredicateEvolutionRetryableConflict("integrity_conflict") from exc
    return StablePredicateEvolutionResult(
        outcome.upper() if outcome != "stale" else "STALE_OPERATION",
        reason_code,
        root,
        decision,
        sources,
        successors,
        assignments,
        current_decision_id=decision.id,
        current_decision_status=decision.lifecycle_status,
    )


async def apply_stable_predicate_evolution(
    db,
    command: StablePredicateCommand,
    *,
    user: Any,
    credential_kind: str,
) -> StablePredicateEvolutionResult:
    validation_error = _validate_request(command, require_actor=False)
    if validation_error:
        return StablePredicateEvolutionResult("REJECTED", validation_error)
    actor_type, actor_id = await _authorize_mutation(
        db,
        library_id=command.library_id,
        user=user,
        credential_kind=credential_kind,
    )
    try:
        return await _apply_stable_predicate_evolution(
            db,
            replace(command, actor_type=actor_type, actor_id=actor_id),
        )
    except GraphIdentityLockBusy as exc:
        raise StablePredicateEvolutionRetryableConflict(
            "predicate_evolution_lock_busy"
        ) from exc
    except IntegrityError as exc:
        raise StablePredicateEvolutionRetryableConflict("integrity_conflict") from exc
    except DBAPIError as exc:
        if _is_lock_not_available(exc):
            raise StablePredicateEvolutionRetryableConflict(
                "predicate_evolution_lock_busy"
            ) from exc
        raise


async def _cancel_pending_stable_predicate_evolution(
    db,
    library_id: uuid.UUID,
    cancellation: StablePredicateEvolutionCancellation,
) -> StablePredicateEvolutionResult:
    validation_error = _validate_cancellation_request(
        library_id,
        cancellation,
        require_actor=True,
    )
    if validation_error:
        return StablePredicateEvolutionResult("REJECTED", validation_error)
    roots = [
        row
        for row in await _command_roots(db, library_id)
        if row.id == cancellation.command_id
    ]
    if len(roots) != 1:
        return StablePredicateEvolutionResult("REJECTED", "command_not_found")
    root = roots[0]
    if not hmac.compare_digest(root.idempotency_key, cancellation.original_idempotency_key):
        return StablePredicateEvolutionResult("REJECTED", "cancellation_root_key_mismatch", root)
    decisions = await _decision_rows(db, library_id, root.id)
    initial_head = _current_decision(decisions)
    if initial_head is None:
        return StablePredicateEvolutionResult("REJECTED", "command_unusable", root)
    basis = initial_head
    if initial_head.requested_effect == "cancel":
        basis = next(
            (
                row
                for row in decisions
                if row.id == initial_head.supersedes_decision_id
            ),
            None,
        )
    if basis is None or basis.requested_effect == "cancel":
        return StablePredicateEvolutionResult("REJECTED", "command_unusable", root)
    try:
        persisted_command = _command_from_persisted_decision(root, basis)
        await _lock_command_scopes(db, persisted_command)
    except StablePredicateEvolutionError:
        return StablePredicateEvolutionResult("REJECTED", "command_unusable", root)

    roots = [
        row
        for row in await _command_roots(db, library_id)
        if row.id == cancellation.command_id
    ]
    if len(roots) != 1:
        return StablePredicateEvolutionResult("REJECTED", "command_unusable")
    root = roots[0]
    decisions = await _decision_rows(db, library_id, root.id)
    head = _current_decision(decisions)
    if head is None:
        return StablePredicateEvolutionResult("REJECTED", "command_unusable", root)
    expected_hash = basis.observed_precondition_fingerprint
    cancel_hash = stable_predicate_cancellation_fingerprint(
        cancellation,
        expected_hash,
    )
    if head.requested_effect == "cancel" and hmac.compare_digest(
        head.decision_payload_fingerprint,
        cancel_hash,
    ):
        return StablePredicateEvolutionResult(
            "CANCELLED",
            command=root,
            decision=head,
            reused_decision_id=head.id,
            effective_outcome="CANCELLED",
            current_decision_id=head.id,
            current_decision_status=head.lifecycle_status,
        )
    if (
        head.id != cancellation.expected_pending_decision_id
        or head.lifecycle_status != PREDICATE_EVOLUTION_DECISION_PENDING
    ):
        return StablePredicateEvolutionResult(
            "STALE_OPERATION",
            "expected_predecessor_mismatch",
            root,
            current_decision_id=head.id,
            current_decision_status=head.lifecycle_status,
        )
    persisted_command = _command_from_persisted_decision(root, head)
    observed_hash = await _build_stable_predicate_precondition_fingerprint(
        db,
        persisted_command,
    )
    await _supersede_predecessor(db, head)
    decision = StablePredicateEvolutionDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        command_id=root.id,
        decision_payload_fingerprint=cancel_hash,
        operation_kind=root.operation_kind,
        requested_effect="cancel",
        evaluated_outcome=PREDICATE_EVOLUTION_DECISION_CANCELLED,
        lifecycle_status=PREDICATE_EVOLUTION_DECISION_CANCELLED,
        operation_payload_snapshot={
            "control_kind": "cancel_pending",
            "expected_pending_decision_id": str(cancellation.expected_pending_decision_id),
        },
        reason_code=cancellation.reason_code,
        reason_text=cancellation.reason_text,
        method="authorized_cancellation",
        confidence=None,
        evidence_refs=_stable_value(_normalized_evidence_refs(cancellation.evidence_refs)),
        expected_precondition_fingerprint=expected_hash,
        observed_precondition_fingerprint=observed_hash,
        supersedes_decision_id=head.id,
        actor_type=cancellation.actor_type,
        actor_id=cancellation.actor_id,
        request_id=cancellation.request_id,
    )
    db.add(decision)
    await db.flush()
    return StablePredicateEvolutionResult(
        "CANCELLED",
        cancellation.reason_code,
        root,
        decision,
        current_decision_id=decision.id,
        current_decision_status=decision.lifecycle_status,
    )


async def cancel_pending_stable_predicate_evolution(
    db,
    library_id: uuid.UUID,
    cancellation: StablePredicateEvolutionCancellation,
    *,
    user: Any,
    credential_kind: str,
) -> StablePredicateEvolutionResult:
    validation_error = _validate_cancellation_request(
        library_id,
        cancellation,
        require_actor=False,
    )
    if validation_error:
        return StablePredicateEvolutionResult("REJECTED", validation_error)
    actor_type, actor_id = await _authorize_mutation(
        db,
        library_id=library_id,
        user=user,
        credential_kind=credential_kind,
    )
    try:
        return await _cancel_pending_stable_predicate_evolution(
            db,
            library_id,
            replace(cancellation, actor_type=actor_type, actor_id=actor_id),
        )
    except GraphIdentityLockBusy as exc:
        raise StablePredicateEvolutionRetryableConflict(
            "predicate_evolution_lock_busy"
        ) from exc
    except IntegrityError as exc:
        raise StablePredicateEvolutionRetryableConflict("integrity_conflict") from exc
    except DBAPIError as exc:
        if _is_lock_not_available(exc):
            raise StablePredicateEvolutionRetryableConflict(
                "predicate_evolution_lock_busy"
            ) from exc
        raise


__all__ = [
    "CurrentStablePredicateResult",
    "StablePredicateEvolutionCancellation",
    "StablePredicateEvolutionContext",
    "StablePredicateEvolutionError",
    "StablePredicateEvolutionResult",
    "StablePredicateEvolutionRetryableConflict",
    "StablePredicateMappingPartition",
    "StablePredicateMergeCommand",
    "StablePredicateReassignCommand",
    "StablePredicateSplitCommand",
    "StablePredicateSuccessorSlot",
    "StablePredicateTargetSpec",
    "apply_stable_predicate_evolution",
    "build_stable_predicate_precondition_fingerprint",
    "cancel_pending_stable_predicate_evolution",
    "resolve_current_stable_predicate_identity",
    "stable_predicate_cancellation_fingerprint",
    "stable_predicate_command_fingerprint",
    "stable_predicate_command_json_bytes",
    "stable_predicate_decision_fingerprint",
    "stable_predicate_target_fingerprint",
    "stable_predicate_target_id",
]
