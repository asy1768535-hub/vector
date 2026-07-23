from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


ExternalType = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
ExternalId = Annotated[str, Field(min_length=1, max_length=512)]
BoundedId = Annotated[str, Field(min_length=1, max_length=128)]
FactKind = Literal["entity", "relation"]
_RESERVED_KEYS = {
    "authorization",
    "api_key",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "credential",
    "credentials",
    "secret",
    "text",
    "quote",
}


def _contains_reserved_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _RESERVED_KEYS
            or _contains_reserved_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_reserved_key(child) for child in value)
    return False


class GraphSyncPolicyWrite(StrictModel):
    authority_rank: int = Field(ge=1, le=999)
    status: Literal["active", "disabled"] = "active"
    stale_after_seconds: int = Field(default=86400, ge=60, le=31536000)


class GraphSyncPolicyRead(GraphSyncPolicyWrite):
    id: uuid.UUID
    library_id: uuid.UUID
    sync_source_id: uuid.UUID
    source_key: str
    created_at: datetime
    updated_at: datetime


class ExternalEntityRef(StrictModel):
    external_type: ExternalType
    external_id: ExternalId


class ExternalGraphItemBase(StrictModel):
    action: Literal["upsert", "delete"]
    fact_kind: FactKind
    external_type: ExternalType
    external_id: ExternalId
    source_version: str | None = Field(default=None, max_length=128)
    evidence_id: uuid.UUID | None = None
    source_locator: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_size_and_locator(self):
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 65536:
            raise ValueError("sync item exceeds 65536 bytes")
        if _contains_reserved_key(self.source_locator) or _contains_reserved_key(
            getattr(self, "properties", None)
        ):
            raise ValueError("sync item contains a reserved key")
        return self


class ExternalEntitySyncItem(ExternalGraphItemBase):
    fact_kind: Literal["entity"]
    ontology_version_id: uuid.UUID | None = None
    entity_type_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    canonical_name: str | None = Field(default=None, min_length=1, max_length=512)
    normalized_name: str | None = Field(default=None, min_length=1, max_length=512)
    properties: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_upsert_fields(self):
        required = (
            self.ontology_version_id,
            self.entity_type_key,
            self.canonical_name,
            self.normalized_name,
        )
        if self.action == "upsert" and any(value is None for value in required):
            raise ValueError("entity upsert fields are required")
        if self.action == "delete" and any(
            value is not None
            for value in (
                *required,
                self.properties,
                self.evidence_id,
            )
        ):
            raise ValueError("entity delete must contain identity only")
        return self


class ExternalRelationSyncItem(ExternalGraphItemBase):
    fact_kind: Literal["relation"]
    ontology_version_id: uuid.UUID | None = None
    relation_type_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    source_entity: ExternalEntityRef | None = None
    target_entity: ExternalEntityRef | None = None
    properties: dict[str, Any] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def require_upsert_fields(self):
        required = (
            self.ontology_version_id,
            self.relation_type_key,
            self.source_entity,
            self.target_entity,
        )
        if self.action == "upsert" and any(value is None for value in required):
            raise ValueError("relation upsert fields are required")
        if self.action == "delete" and any(
            value is not None
            for value in (
                *required,
                self.properties,
                self.valid_from,
                self.valid_to,
                self.evidence_id,
            )
        ):
            raise ValueError("relation delete must contain identity only")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


ExternalGraphSyncItem = Annotated[
    ExternalEntitySyncItem | ExternalRelationSyncItem,
    Field(discriminator="fact_kind"),
]


class ExternalGraphSyncBatchRequest(StrictModel):
    idempotency_key: BoundedId
    source_event_id: str | None = Field(default=None, max_length=128)
    snapshot_id: str | None = Field(default=None, max_length=128)
    complete_snapshot: bool = False
    items: list[ExternalGraphSyncItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_batch(self):
        if self.complete_snapshot and not self.snapshot_id:
            raise ValueError("complete_snapshot requires snapshot_id")
        identities = [
            (item.fact_kind, item.external_type, item.external_id)
            for item in self.items
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("batch external identities must be unique")
        return self


class ExternalGraphSyncItemResult(StrictModel):
    fact_kind: FactKind
    external_type: str
    external_id: str
    operation: Literal[
        "created", "updated", "unchanged", "deleted", "conflict", "not_found"
    ]
    mapping_id: uuid.UUID | None = None
    fact_id: uuid.UUID | None = None
    conflict_id: uuid.UUID | None = None


class ExternalGraphSyncBatchResponse(StrictModel):
    operation_id: uuid.UUID
    replayed: bool
    status: Literal["applied", "conflicted"]
    created_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    items: list[ExternalGraphSyncItemResult] = Field(max_length=100)


class ExternalGraphMappingRead(StrictModel):
    id: uuid.UUID
    source_key: str
    fact_kind: FactKind
    external_type: str
    external_id: str
    fact_id: uuid.UUID
    lifecycle: Literal["active", "stale", "tombstoned"]
    source_version: str | None = None
    evidence_id: uuid.UUID | None = None
    source_locator: dict[str, Any]
    last_seen_at: datetime


class ExternalGraphConflictRead(StrictModel):
    id: uuid.UUID
    source_key: str
    mapping_id: uuid.UUID
    operation_id: uuid.UUID
    reason_code: str
    incoming_hash: str
    current_hash: str | None = None
    incoming_authority_rank: int
    current_authority_rank: int | None = None
    status: Literal["open", "resolved", "dismissed"]
    created_at: datetime


class ExternalGraphConflictDecision(StrictModel):
    decision: Literal["resolved", "dismissed"]
