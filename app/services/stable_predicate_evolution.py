"""StablePredicate evolution identities and deterministic target allocation."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unicodedata import normalize

import rfc8785

_COMMAND_SCHEMA = "p3_2_stable_predicate_evolution_command_v1"
_TARGET_SCHEMA = "p3_2_stable_predicate_target_v1"
_TARGET_UUID_PREFIX = "urn:vector-kb:p3.2:stable-predicate-identity:v1:"


class StablePredicateEvolutionError(ValueError):
    """The supplied evolution identity is not in the frozen P3.2 domain."""


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
class StablePredicateMergeCommand:
    library_id: uuid.UUID
    source_predicate_ids: tuple[uuid.UUID, ...]
    survivor_predicate_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StablePredicateSplitCommand:
    library_id: uuid.UUID
    source_predicate_id: uuid.UUID
    successor_slots: tuple[StablePredicateSuccessorSlot, ...]


@dataclass(frozen=True, slots=True)
class StablePredicateReassignCommand:
    library_id: uuid.UUID
    mapping_id: uuid.UUID
    from_predicate_id: uuid.UUID
    to_predicate_id: uuid.UUID


StablePredicateCommand = (
    StablePredicateMergeCommand
    | StablePredicateSplitCommand
    | StablePredicateReassignCommand
)


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
        value = int(value) if value == value.to_integral_value() else float(value)
        return _stable_value(value)
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0 and math.copysign(1, value) < 0):
            raise StablePredicateEvolutionError("number is not canonical")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
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
    if not all(
        isinstance(value, str) and value
        for value in (
            target.namespace,
            target.key,
            target.contract_version,
            target.identity_policy_version,
        )
    ):
        raise StablePredicateEvolutionError("target identity fields are required")
    return {
        "schema": _TARGET_SCHEMA,
        "namespace": target.namespace,
        "key": target.key,
        "contract_version": target.contract_version,
        "temporal_class": target.temporal_class,
        "identity_policy_version": target.identity_policy_version,
        "resolution_status": "resolved",
        "resolution_policy": dict(target.resolution_policy),
    }


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


__all__ = [
    "StablePredicateEvolutionError",
    "StablePredicateMergeCommand",
    "StablePredicateReassignCommand",
    "StablePredicateSplitCommand",
    "StablePredicateSuccessorSlot",
    "StablePredicateTargetSpec",
    "stable_predicate_command_fingerprint",
    "stable_predicate_command_json_bytes",
    "stable_predicate_target_fingerprint",
    "stable_predicate_target_id",
]
