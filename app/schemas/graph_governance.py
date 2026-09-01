from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.graph_governance_contracts import (
    GRAPH_GOVERNANCE_MAX_ITEMS,
    GraphGovernanceActionStatus,
    GraphGovernanceError,
    normalize_governance_payload,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StrictGovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


def _bounded_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if len(value) > 100:
        raise ValueError("graph governance payload has too many keys")
    try:
        return normalize_governance_payload(value)
    except GraphGovernanceError as exc:
        raise ValueError("graph governance payload is invalid") from exc


def _nonblank(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


class GraphGovernanceCommandRequest(StrictGovernanceModel):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        return _nonblank(value)


class GraphGovernanceManualEntityRequest(GraphGovernanceCommandRequest):
    ontology_version_id: uuid.UUID
    entity_type_id: uuid.UUID
    canonical_name: str = Field(min_length=1, max_length=512)
    properties: dict[str, Any] | None = None

    _name = field_validator("canonical_name")(_nonblank)
    _properties = field_validator("properties")(_bounded_payload)


class GraphGovernanceManualRelationRequest(GraphGovernanceCommandRequest):
    ontology_version_id: uuid.UUID
    relation_type_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    properties: dict[str, Any] | None = None

    _properties = field_validator("properties")(_bounded_payload)


class GraphGovernanceEntityCorrectionRequest(GraphGovernanceCommandRequest):
    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_name: str | None = Field(default=None, min_length=1, max_length=512)
    properties: dict[str, Any] | None = None

    _name = field_validator("canonical_name")(
        lambda value: _nonblank(value) if value is not None else None
    )
    _properties = field_validator("properties")(_bounded_payload)

    @model_validator(mode="after")
    def require_change(self):
        if not {"canonical_name", "properties"}.intersection(self.model_fields_set):
            raise ValueError("at least one entity field must be supplied")
        return self


class GraphGovernanceRelationCorrectionRequest(GraphGovernanceCommandRequest):
    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_entity_id: uuid.UUID | None = None
    target_entity_id: uuid.UUID | None = None
    properties: dict[str, Any] | None = None

    _properties = field_validator("properties")(_bounded_payload)

    @model_validator(mode="after")
    def require_change(self):
        if not {
            "source_entity_id",
            "target_entity_id",
            "properties",
        }.intersection(self.model_fields_set):
            raise ValueError("at least one relation field must be supplied")
        return self


class GraphGovernanceAliasRequest(GraphGovernanceCommandRequest):
    expected_entity_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    alias: str = Field(min_length=1, max_length=512)

    _alias = field_validator("alias")(_nonblank)


class GraphGovernanceDecisionRequest(StrictGovernanceModel):
    expected_status: Literal["pending_review"]
    decision: Literal["approve", "reject"]
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class GraphGovernanceCancelRequest(StrictGovernanceModel):
    expected_status: Literal["pending_review", "approved"]
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class GraphGovernanceRelationReviewRequest(GraphGovernanceCommandRequest):
    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class GraphGovernanceStateRequest(GraphGovernanceCommandRequest):
    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class GraphGovernanceMergeResolution(StrictGovernanceModel):
    relation_id: uuid.UUID
    resolution: Literal["reassign", "disable", "retain"]
    conflicting_relation_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_conflict_identity(self):
        if self.conflicting_relation_id == self.relation_id:
            raise ValueError("conflicting relation must be distinct")
        return self


class GraphGovernanceEntityMergeRequest(GraphGovernanceCommandRequest):
    ontology_version_id: uuid.UUID
    survivor_entity_id: uuid.UUID
    loser_entity_id: uuid.UUID
    expected_survivor_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_loser_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resolutions: list[GraphGovernanceMergeResolution] = Field(
        default_factory=list,
        max_length=GRAPH_GOVERNANCE_MAX_ITEMS,
    )

    @model_validator(mode="after")
    def validate_merge(self):
        if self.survivor_entity_id == self.loser_entity_id:
            raise ValueError("merge entities must be distinct")
        relation_ids = [item.relation_id for item in self.resolutions]
        if len(set(relation_ids)) != len(relation_ids):
            raise ValueError("merge resolutions must be unique")
        return self


class GraphGovernancePublicationPlanRequest(GraphGovernanceCommandRequest):
    ontology_version_id: uuid.UUID
    action_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=GRAPH_GOVERNANCE_MAX_ITEMS,
    )
    expected_parent_publication_id: uuid.UUID | None = None
    dry_run: bool = False

    @field_validator("action_ids")
    @classmethod
    def validate_action_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("action IDs must be unique")
        return sorted(value, key=str)


class GraphGovernancePublicationPlanRead(StrictGovernanceModel):
    publication_id: uuid.UUID
    status: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_publication_id: uuid.UUID | None
    entity_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)
    blocked_counts: dict[str, int]
    dry_run: bool
    reused: bool


class GraphGovernanceEntityTypeOptionRead(StrictGovernanceModel):
    id: uuid.UUID
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)


class GraphGovernanceRelationTypeOptionRead(GraphGovernanceEntityTypeOptionRead):
    direction: Literal["directed", "undirected"]
    default_review_policy: Literal["auto_active", "pending_review", "manual_only"]
    requires_evidence: bool


class GraphGovernanceOntologyOptionRead(StrictGovernanceModel):
    id: uuid.UUID
    version_key: str = Field(min_length=1, max_length=128)
    version_no: int = Field(ge=1)
    entity_types: list[GraphGovernanceEntityTypeOptionRead] = Field(max_length=100)
    relation_types: list[GraphGovernanceRelationTypeOptionRead] = Field(max_length=100)


class GraphGovernanceWriteContextRead(StrictGovernanceModel):
    contract_version: Literal["graph-governance-context-v1"] = (
        "graph-governance-context-v1"
    )
    library_id: uuid.UUID
    library_slug: str = Field(min_length=1, max_length=80)
    ontology_versions: list[GraphGovernanceOntologyOptionRead] = Field(max_length=20)


class GraphGovernanceActionItemRead(StrictGovernanceModel):
    id: uuid.UUID
    action_id: uuid.UUID
    ordinal: int
    item_kind: Literal["entity", "relation", "alias"]
    effect_kind: Literal["activate", "update", "disable", "restore", "reassign", "retain"]
    entity_id: uuid.UUID | None
    relation_id: uuid.UUID | None
    alias_id: uuid.UUID | None
    before_hash: str | None
    after_hash: str
    effect_payload: dict[str, Any]
    status: Literal["planned", "applied"]
    applied_at: datetime | None
    created_at: datetime

    _before_hash = field_validator("before_hash")(
        lambda value: value
        if value is None or _SHA256_RE.fullmatch(value)
        else (_ for _ in ()).throw(ValueError("invalid hash"))
    )
    _after_hash = field_validator("after_hash")(
        lambda value: value
        if _SHA256_RE.fullmatch(value)
        else (_ for _ in ()).throw(ValueError("invalid hash"))
    )
    _payload = field_validator("effect_payload")(_bounded_payload)


class GraphGovernanceActionRead(StrictGovernanceModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    action_kind: str
    status: GraphGovernanceActionStatus
    target_entity_id: uuid.UUID | None
    target_relation_id: uuid.UUID | None
    target_alias_id: uuid.UUID | None
    survivor_entity_id: uuid.UUID | None
    loser_entity_id: uuid.UUID | None
    payload: dict[str, Any]
    expected_state_hash: str
    command_hash: str
    reason_code: str | None
    planned_publication_id: uuid.UUID | None
    applied_publication_id: uuid.UUID | None
    requested_by_user_id: uuid.UUID | None
    decided_by_user_id: uuid.UUID | None
    cancelled_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    cancelled_at: datetime | None
    applied_at: datetime | None
    items: list[GraphGovernanceActionItemRead] = Field(
        default_factory=list,
        max_length=GRAPH_GOVERNANCE_MAX_ITEMS,
    )

    _payload = field_validator("payload")(_bounded_payload)

    @field_validator("expected_state_hash", "command_hash")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("invalid hash")
        return value

    @field_validator("reason_code")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is not None and _REASON_RE.fullmatch(value) is None:
            raise ValueError("invalid reason code")
        return value


class GraphGovernanceActionPageRead(StrictGovernanceModel):
    items: list[GraphGovernanceActionRead]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
