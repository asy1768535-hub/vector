from __future__ import annotations

import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.graph_canonical import (
    canonical_graph_json_v1,
    canonical_graph_value_hash_v1,
)


SchemaLifecycleActionKind = Literal[
    "clone_version",
    "create_item",
    "update_item",
    "disable_item",
    "activate_version",
]
SchemaLifecycleTargetKind = Literal[
    "ontology_version",
    "entity_type",
    "relation_type",
    "attribute",
    "constraint",
]
SCHEMA_LIFECYCLE_ACTION_KINDS = (
    "clone_version",
    "create_item",
    "update_item",
    "disable_item",
    "activate_version",
)
SCHEMA_LIFECYCLE_TARGET_KINDS = (
    "ontology_version",
    "entity_type",
    "relation_type",
    "attribute",
    "constraint",
)
SCHEMA_LIFECYCLE_ERROR_CODES = (
    "schema_lifecycle_forbidden",
    "schema_lifecycle_not_found",
    "schema_lifecycle_request_invalid",
    "schema_lifecycle_state_changed",
    "schema_lifecycle_idempotency_conflict",
    "schema_lifecycle_invalid_draft",
    "schema_lifecycle_dependency_conflict",
    "schema_lifecycle_unavailable",
)
SCHEMA_LIFECYCLE_MAX_PAYLOAD_BYTES = 65_536
SCHEMA_LIFECYCLE_MAX_ISSUES = 100
SCHEMA_LIFECYCLE_MAX_COMPATIBILITY_LIBRARIES = 20
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SchemaLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str = "Schema lifecycle operation failed") -> None:
        if code not in SCHEMA_LIFECYCLE_ERROR_CODES:
            code = "schema_lifecycle_unavailable"
        self.code = code
        super().__init__(message[:255])


def fail_schema_lifecycle(code: str, message: str) -> None:
    raise SchemaLifecycleError(code, message)


def normalize_schema_lifecycle_payload(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 100:
        fail_schema_lifecycle(
            "schema_lifecycle_request_invalid", "Schema command payload is invalid"
        )
    copied = deepcopy(value)
    try:
        encoded = canonical_graph_json_v1(copied).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema command payload is invalid"
        ) from exc
    if len(encoded) > SCHEMA_LIFECYCLE_MAX_PAYLOAD_BYTES:
        fail_schema_lifecycle(
            "schema_lifecycle_request_invalid", "Schema command payload is too large"
        )
    return copied


def canonical_schema_lifecycle_state_hash(value: dict[str, Any]) -> str:
    try:
        return canonical_graph_value_hash_v1(value)
    except (TypeError, ValueError) as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_unavailable", "Stored Schema state is invalid"
        ) from exc


def deterministic_schema_clone_id(
    library_id: uuid.UUID,
    source_version_id: uuid.UUID,
    idempotency_key: str,
    command_hash: str,
) -> uuid.UUID:
    identity = ":".join(
        (str(library_id), str(source_version_id), idempotency_key, command_hash)
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, f"vector-kb:schema-clone:{identity}")


def deterministic_schema_child_id(
    draft_version_id: uuid.UUID,
    source_child_id: uuid.UUID,
    target_kind: SchemaLifecycleTargetKind,
) -> uuid.UUID:
    if target_kind == "ontology_version":
        fail_schema_lifecycle(
            "schema_lifecycle_request_invalid", "Clone child kind is invalid"
        )
    return uuid.uuid5(
        draft_version_id,
        f"{target_kind}:{source_child_id}",
    )


def deterministic_schema_item_id(
    ontology_version_id: uuid.UUID,
    target_kind: SchemaLifecycleTargetKind,
    idempotency_key: str,
) -> uuid.UUID:
    if target_kind == "ontology_version":
        fail_schema_lifecycle(
            "schema_lifecycle_request_invalid", "Schema item kind is invalid"
        )
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        fail_schema_lifecycle(
            "schema_lifecycle_request_invalid", "Idempotency key is invalid"
        )
    return uuid.uuid5(
        ontology_version_id,
        f"schema-item:{target_kind}:{idempotency_key}",
    )


@dataclass(frozen=True, slots=True)
class SchemaLifecycleCommand:
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    actor_user_id: uuid.UUID
    action_kind: SchemaLifecycleActionKind
    target_kind: SchemaLifecycleTargetKind
    target_id: uuid.UUID
    expected_state_hash: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    command_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.action_kind not in SCHEMA_LIFECYCLE_ACTION_KINDS:
            fail_schema_lifecycle(
                "schema_lifecycle_request_invalid", "Schema action kind is invalid"
            )
        if self.target_kind not in SCHEMA_LIFECYCLE_TARGET_KINDS:
            fail_schema_lifecycle(
                "schema_lifecycle_request_invalid", "Schema target kind is invalid"
            )
        if self.action_kind in {"clone_version", "activate_version"} and (
            self.target_kind != "ontology_version"
        ):
            fail_schema_lifecycle(
                "schema_lifecycle_request_invalid", "Schema action target is invalid"
            )
        if not _HASH_RE.fullmatch(self.expected_state_hash):
            fail_schema_lifecycle(
                "schema_lifecycle_request_invalid", "Expected Schema state is invalid"
            )
        if not _IDEMPOTENCY_RE.fullmatch(self.idempotency_key):
            fail_schema_lifecycle(
                "schema_lifecycle_request_invalid", "Idempotency key is invalid"
            )
        payload = normalize_schema_lifecycle_payload(self.payload)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(
            self,
            "command_hash",
            canonical_graph_value_hash_v1(
                {
                    "action_kind": self.action_kind,
                    "expected_state_hash": self.expected_state_hash,
                    "library_id": str(self.library_id),
                    "ontology_version_id": str(self.ontology_version_id),
                    "payload": payload,
                    "target_id": str(self.target_id),
                    "target_kind": self.target_kind,
                }
            ),
        )
