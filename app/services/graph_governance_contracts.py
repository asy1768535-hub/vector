from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.models.graph_governance_action import (
    GRAPH_GOVERNANCE_ACTION_KINDS,
    GRAPH_GOVERNANCE_EFFECT_KINDS,
    GRAPH_GOVERNANCE_ITEM_KINDS,
)
from app.services.graph_canonical import (
    canonical_graph_json_v1,
    canonical_graph_value_hash_v1,
)


GRAPH_GOVERNANCE_CONTRACT_VERSION = "graph-governance-v1"
GRAPH_GOVERNANCE_MAX_PAYLOAD_BYTES = 65_536
GRAPH_GOVERNANCE_MAX_ITEMS = 1_000
GRAPH_GOVERNANCE_ERROR_CODES = (
    "graph_governance_request_invalid",
    "graph_governance_not_found",
    "graph_governance_forbidden",
    "graph_governance_state_changed",
    "graph_governance_idempotency_conflict",
    "graph_governance_scope_mismatch",
    "graph_governance_merge_incompatible",
    "graph_governance_merge_conflict",
    "graph_governance_action_in_use",
    "graph_governance_publication_changed",
    "graph_governance_unavailable",
)

GraphGovernanceActionKind = Literal[
    "entity_create",
    "relation_create",
    "entity_correct",
    "relation_correct",
    "entity_disable",
    "entity_restore",
    "relation_disable",
    "relation_restore",
    "relation_review",
    "alias_add",
    "alias_disable",
    "entity_merge",
]
GraphGovernanceActionStatus = Literal[
    "pending_review", "approved", "rejected", "cancelled", "applied"
]
GraphGovernanceItemKind = Literal["entity", "relation", "alias"]
GraphGovernanceEffectKind = Literal[
    "activate", "update", "disable", "restore", "reassign", "retain"
]
GraphGovernanceDecision = Literal["approve", "reject"]
GraphGovernanceLifecycleEvent = Literal["approve", "reject", "cancel", "apply"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class GraphGovernanceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "Graph governance operation failed",
    ) -> None:
        self.code = code if code in GRAPH_GOVERNANCE_ERROR_CODES else "graph_governance_unavailable"
        super().__init__(message[:255])


def fail_graph_governance(
    code: str,
    message: str = "Graph governance operation failed",
) -> None:
    raise GraphGovernanceError(code, message)


def _uuid(value: uuid.UUID) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        fail_graph_governance("graph_governance_request_invalid")
    return value


def _optional_uuid(value: uuid.UUID | None) -> uuid.UUID | None:
    if value is not None:
        _uuid(value)
    return value


def _hash(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        fail_graph_governance("graph_governance_request_invalid")
    return value


def normalize_governance_reason_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip() if isinstance(value, str) else ""
    if _REASON_RE.fullmatch(normalized) is None:
        fail_graph_governance("graph_governance_request_invalid")
    return normalized


def normalize_governance_payload(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail_graph_governance("graph_governance_request_invalid")
    try:
        canonical = canonical_graph_json_v1(value)
    except (TypeError, ValueError) as exc:
        raise GraphGovernanceError("graph_governance_request_invalid") from exc
    if len(canonical.encode("utf-8")) > GRAPH_GOVERNANCE_MAX_PAYLOAD_BYTES:
        fail_graph_governance("graph_governance_request_invalid")
    return json.loads(canonical)


def canonical_governance_expected_state_hash(
    *,
    item_kind: GraphGovernanceItemKind,
    item_id: uuid.UUID,
    state: dict[str, Any],
) -> str:
    if item_kind not in GRAPH_GOVERNANCE_ITEM_KINDS:
        fail_graph_governance("graph_governance_request_invalid")
    _uuid(item_id)
    normalized = normalize_governance_payload(state)
    return canonical_graph_value_hash_v1(
        {
            "contract_version": GRAPH_GOVERNANCE_CONTRACT_VERSION,
            "item_id": str(item_id),
            "item_kind": item_kind,
            "state": normalized,
        }
    )


def canonical_governance_absent_state_hash(
    *, item_kind: GraphGovernanceItemKind, item_id: uuid.UUID
) -> str:
    return canonical_governance_expected_state_hash(
        item_kind=item_kind,
        item_id=item_id,
        state={"exists": False},
    )


def _validate_action_target(
    action_kind: GraphGovernanceActionKind,
    *,
    target_entity_id: uuid.UUID | None,
    target_relation_id: uuid.UUID | None,
    target_alias_id: uuid.UUID | None,
    survivor_entity_id: uuid.UUID | None,
    loser_entity_id: uuid.UUID | None,
) -> None:
    values = (
        target_entity_id,
        target_relation_id,
        target_alias_id,
        survivor_entity_id,
        loser_entity_id,
    )
    for value in values:
        _optional_uuid(value)
    entity_action = action_kind in {
        "entity_create",
        "entity_correct",
        "entity_disable",
        "entity_restore",
    }
    relation_action = action_kind in {
        "relation_create",
        "relation_correct",
        "relation_disable",
        "relation_restore",
        "relation_review",
    }
    alias_action = action_kind in {"alias_add", "alias_disable"}
    if entity_action:
        valid = target_entity_id is not None and all(value is None for value in values[1:])
    elif relation_action:
        valid = target_relation_id is not None and all(
            value is None for index, value in enumerate(values) if index != 1
        )
    elif alias_action:
        valid = target_alias_id is not None and all(
            value is None for index, value in enumerate(values) if index != 2
        )
    else:
        valid = (
            target_entity_id is None
            and target_relation_id is None
            and target_alias_id is None
            and survivor_entity_id is not None
            and loser_entity_id is not None
            and survivor_entity_id != loser_entity_id
        )
    if not valid:
        fail_graph_governance("graph_governance_request_invalid")


@dataclass(frozen=True, slots=True)
class StageGraphGovernanceActionCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    action_kind: GraphGovernanceActionKind
    payload: dict[str, Any]
    expected_state_hash: str
    initial_status: Literal["pending_review", "approved"]
    target_entity_id: uuid.UUID | None = None
    target_relation_id: uuid.UUID | None = None
    target_alias_id: uuid.UUID | None = None
    survivor_entity_id: uuid.UUID | None = None
    loser_entity_id: uuid.UUID | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.ontology_version_id)
        _uuid(self.actor_user_id)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128:
            fail_graph_governance("graph_governance_request_invalid")
        if self.action_kind not in GRAPH_GOVERNANCE_ACTION_KINDS:
            fail_graph_governance("graph_governance_request_invalid")
        if self.initial_status not in {"pending_review", "approved"}:
            fail_graph_governance("graph_governance_request_invalid")
        _validate_action_target(
            self.action_kind,
            target_entity_id=self.target_entity_id,
            target_relation_id=self.target_relation_id,
            target_alias_id=self.target_alias_id,
            survivor_entity_id=self.survivor_entity_id,
            loser_entity_id=self.loser_entity_id,
        )
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "payload", normalize_governance_payload(self.payload))
        object.__setattr__(self, "expected_state_hash", _hash(self.expected_state_hash))
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "action_kind": self.action_kind,
            "expected_state_hash": self.expected_state_hash,
            "library_id": str(self.library_id),
            "loser_entity_id": str(self.loser_entity_id) if self.loser_entity_id else None,
            "ontology_version_id": str(self.ontology_version_id),
            "payload": self.payload,
            "survivor_entity_id": (
                str(self.survivor_entity_id) if self.survivor_entity_id else None
            ),
            "target_alias_id": str(self.target_alias_id) if self.target_alias_id else None,
            "target_entity_id": str(self.target_entity_id) if self.target_entity_id else None,
            "target_relation_id": (
                str(self.target_relation_id) if self.target_relation_id else None
            ),
        }

    @property
    def command_hash(self) -> str:
        return canonical_governance_command_hash(self)


def canonical_governance_command_hash(
    command: StageGraphGovernanceActionCommand,
) -> str:
    if not isinstance(command, StageGraphGovernanceActionCommand):
        fail_graph_governance("graph_governance_request_invalid")
    return canonical_graph_value_hash_v1(
        {
            "contract_version": GRAPH_GOVERNANCE_CONTRACT_VERSION,
            "command": command.identity_payload(),
        }
    )


@dataclass(frozen=True, slots=True)
class SubmitManualEntityCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    entity_type_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    canonical_name: str
    properties: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.ontology_version_id)
        _uuid(self.entity_type_id)
        _uuid(self.actor_user_id)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        name = " ".join(self.canonical_name.split()) if isinstance(self.canonical_name, str) else ""
        if not key or len(key) > 128 or not name or len(name) > 512:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "canonical_name", name)
        if self.properties is not None:
            object.__setattr__(
                self,
                "properties",
                normalize_governance_payload(self.properties),
            )


@dataclass(frozen=True, slots=True)
class SubmitManualRelationCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    relation_type_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    properties: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for value in (
            self.library_id,
            self.ontology_version_id,
            self.relation_type_id,
            self.source_entity_id,
            self.target_entity_id,
            self.actor_user_id,
        ):
            _uuid(value)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        if self.properties is not None:
            object.__setattr__(
                self,
                "properties",
                normalize_governance_payload(self.properties),
            )


@dataclass(frozen=True, slots=True)
class SubmitEntityCorrectionCommand:
    library_id: uuid.UUID
    entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    canonical_name: str | None = None
    properties: dict[str, Any] | None = None
    replace_properties: bool = False

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.entity_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        name = (
            " ".join(self.canonical_name.split())
            if isinstance(self.canonical_name, str)
            else None
        )
        if (
            not key
            or len(key) > 128
            or (name is not None and (not name or len(name) > 512))
            or (name is None and not self.replace_properties)
        ):
            fail_graph_governance("graph_governance_request_invalid")
        if self.replace_properties and self.properties is not None:
            object.__setattr__(
                self,
                "properties",
                normalize_governance_payload(self.properties),
            )
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "canonical_name", name)


@dataclass(frozen=True, slots=True)
class SubmitRelationCorrectionCommand:
    library_id: uuid.UUID
    relation_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    source_entity_id: uuid.UUID | None = None
    target_entity_id: uuid.UUID | None = None
    properties: dict[str, Any] | None = None
    replace_properties: bool = False

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.relation_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        _optional_uuid(self.source_entity_id)
        _optional_uuid(self.target_entity_id)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if (
            not key
            or len(key) > 128
            or (
                self.source_entity_id is None
                and self.target_entity_id is None
                and not self.replace_properties
            )
        ):
            fail_graph_governance("graph_governance_request_invalid")
        if self.replace_properties and self.properties is not None:
            object.__setattr__(
                self,
                "properties",
                normalize_governance_payload(self.properties),
            )
        object.__setattr__(self, "idempotency_key", key)


@dataclass(frozen=True, slots=True)
class SubmitAliasCommand:
    library_id: uuid.UUID
    entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_entity_state_hash: str
    alias: str

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.entity_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_entity_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        alias = " ".join(self.alias.split()) if isinstance(self.alias, str) else ""
        if not key or len(key) > 128 or not alias or len(alias) > 512:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "alias", alias)


@dataclass(frozen=True, slots=True)
class ReviewRelationCommand:
    library_id: uuid.UUID
    relation_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    decision: GraphGovernanceDecision
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.relation_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if (
            not key
            or len(key) > 128
            or self.decision not in {"approve", "reject"}
        ):
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class StageEntityStatusCommand:
    library_id: uuid.UUID
    entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    operation: Literal["disable", "restore"]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.entity_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128 or self.operation not in {"disable", "restore"}:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class StageRelationStatusCommand:
    library_id: uuid.UUID
    relation_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    operation: Literal["disable", "restore"]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.relation_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128 or self.operation not in {"disable", "restore"}:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class StageAliasDisableCommand:
    library_id: uuid.UUID
    alias_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_state_hash: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.alias_id)
        _uuid(self.actor_user_id)
        _hash(self.expected_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class MergeConflictResolutionInput:
    relation_id: uuid.UUID
    resolution: Literal["reassign", "disable", "retain"]
    conflicting_relation_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _uuid(self.relation_id)
        _optional_uuid(self.conflicting_relation_id)
        if (
            self.resolution not in {"reassign", "disable", "retain"}
            or self.conflicting_relation_id == self.relation_id
        ):
            fail_graph_governance("graph_governance_request_invalid")


@dataclass(frozen=True, slots=True)
class StageEntityMergeCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    survivor_entity_id: uuid.UUID
    loser_entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    idempotency_key: str
    expected_survivor_state_hash: str
    expected_loser_state_hash: str
    resolutions: tuple[MergeConflictResolutionInput, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.library_id,
            self.ontology_version_id,
            self.survivor_entity_id,
            self.loser_entity_id,
            self.actor_user_id,
        ):
            _uuid(value)
        _hash(self.expected_survivor_state_hash)
        _hash(self.expected_loser_state_hash)
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if (
            not key
            or len(key) > 128
            or self.survivor_entity_id == self.loser_entity_id
            or not isinstance(self.resolutions, tuple)
            or len(self.resolutions) > GRAPH_GOVERNANCE_MAX_ITEMS
            or any(
                not isinstance(value, MergeConflictResolutionInput)
                for value in self.resolutions
            )
            or len({value.relation_id for value in self.resolutions})
            != len(self.resolutions)
        ):
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(
            self,
            "resolutions",
            tuple(sorted(self.resolutions, key=lambda value: str(value.relation_id))),
        )
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class GraphGovernanceEffect:
    ordinal: int
    item_kind: GraphGovernanceItemKind
    effect_kind: GraphGovernanceEffectKind
    item_id: uuid.UUID
    before_hash: str | None
    after_state: dict[str, Any]

    def __post_init__(self) -> None:
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or not 0 <= self.ordinal < GRAPH_GOVERNANCE_MAX_ITEMS
            or self.item_kind not in GRAPH_GOVERNANCE_ITEM_KINDS
            or self.effect_kind not in GRAPH_GOVERNANCE_EFFECT_KINDS
        ):
            fail_graph_governance("graph_governance_request_invalid")
        _uuid(self.item_id)
        if self.before_hash is not None:
            _hash(self.before_hash)
        object.__setattr__(self, "after_state", normalize_governance_payload(self.after_state))

    @property
    def after_hash(self) -> str:
        return canonical_governance_expected_state_hash(
            item_kind=self.item_kind,
            item_id=self.item_id,
            state=self.after_state,
        )

    @property
    def item_hash(self) -> str:
        return canonical_governance_item_hash(self)


def canonical_governance_item_hash(effect: GraphGovernanceEffect) -> str:
    if not isinstance(effect, GraphGovernanceEffect):
        fail_graph_governance("graph_governance_request_invalid")
    return canonical_graph_value_hash_v1(
        {
            "after_hash": effect.after_hash,
            "before_hash": effect.before_hash,
            "contract_version": GRAPH_GOVERNANCE_CONTRACT_VERSION,
            "effect_kind": effect.effect_kind,
            "item_id": str(effect.item_id),
            "item_kind": effect.item_kind,
            "ordinal": effect.ordinal,
        }
    )


@dataclass(frozen=True, slots=True)
class GraphGovernanceActionBinding:
    action_id: uuid.UUID
    command_hash: str
    item_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.action_id)
        _hash(self.command_hash)
        if (
            not isinstance(self.item_hashes, tuple)
            or not 1 <= len(self.item_hashes) <= GRAPH_GOVERNANCE_MAX_ITEMS
            or any(_SHA256_RE.fullmatch(value) is None for value in self.item_hashes)
        ):
            fail_graph_governance("graph_governance_request_invalid")


def canonicalize_governance_action_bindings(
    bindings: tuple[GraphGovernanceActionBinding, ...],
) -> tuple[GraphGovernanceActionBinding, ...]:
    if (
        not isinstance(bindings, tuple)
        or not bindings
        or len(bindings) > GRAPH_GOVERNANCE_MAX_ITEMS
        or any(not isinstance(item, GraphGovernanceActionBinding) for item in bindings)
        or len({item.action_id for item in bindings}) != len(bindings)
    ):
        fail_graph_governance("graph_governance_request_invalid")
    return tuple(sorted(bindings, key=lambda item: str(item.action_id)))


def canonical_governance_action_set_hash(
    bindings: tuple[GraphGovernanceActionBinding, ...],
) -> str:
    ordered = canonicalize_governance_action_bindings(bindings)
    return canonical_graph_value_hash_v1(
        {
            "actions": [
                {
                    "action_id": str(item.action_id),
                    "command_hash": item.command_hash,
                    "item_hashes": list(item.item_hashes),
                }
                for item in ordered
            ],
            "contract_version": GRAPH_GOVERNANCE_CONTRACT_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class DecideGraphGovernanceActionCommand:
    library_id: uuid.UUID
    action_id: uuid.UUID
    actor_user_id: uuid.UUID
    expected_status: Literal["pending_review"]
    decision: GraphGovernanceDecision
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.action_id)
        _uuid(self.actor_user_id)
        if self.expected_status != "pending_review" or self.decision not in {
            "approve",
            "reject",
        }:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class CancelGraphGovernanceActionCommand:
    library_id: uuid.UUID
    action_id: uuid.UUID
    actor_user_id: uuid.UUID
    expected_status: Literal["pending_review", "approved"]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.action_id)
        _uuid(self.actor_user_id)
        if self.expected_status not in {"pending_review", "approved"}:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(
            self,
            "reason_code",
            normalize_governance_reason_code(self.reason_code),
        )


@dataclass(frozen=True, slots=True)
class PlanGraphGovernancePublicationCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    actor_user_id: uuid.UUID
    action_ids: tuple[uuid.UUID, ...]
    expected_parent_publication_id: uuid.UUID | None
    idempotency_key: str

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.ontology_version_id)
        _uuid(self.actor_user_id)
        _optional_uuid(self.expected_parent_publication_id)
        if (
            not isinstance(self.action_ids, tuple)
            or not 1 <= len(self.action_ids) <= GRAPH_GOVERNANCE_MAX_ITEMS
            or any(not isinstance(value, uuid.UUID) for value in self.action_ids)
            or len(set(self.action_ids)) != len(self.action_ids)
        ):
            fail_graph_governance("graph_governance_request_invalid")
        key = self.idempotency_key.strip() if isinstance(self.idempotency_key, str) else ""
        if not key or len(key) > 128:
            fail_graph_governance("graph_governance_request_invalid")
        object.__setattr__(
            self,
            "action_ids",
            tuple(sorted(self.action_ids, key=str)),
        )
        object.__setattr__(self, "idempotency_key", key)


_TRANSITIONS: dict[
    tuple[GraphGovernanceActionStatus, GraphGovernanceLifecycleEvent],
    GraphGovernanceActionStatus,
] = {
    ("pending_review", "approve"): "approved",
    ("pending_review", "reject"): "rejected",
    ("pending_review", "cancel"): "cancelled",
    ("approved", "cancel"): "cancelled",
    ("approved", "apply"): "applied",
}


def graph_governance_transition(
    current: GraphGovernanceActionStatus,
    event: GraphGovernanceLifecycleEvent,
) -> GraphGovernanceActionStatus:
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError as exc:
        raise GraphGovernanceError("graph_governance_state_changed") from exc
