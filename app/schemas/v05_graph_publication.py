from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


GraphPublicationStatus = Literal[
    "planned",
    "activating",
    "active",
    "degraded",
    "superseded",
    "cancelled",
    "failed",
]
GraphPublicationSourceMode = Literal[
    "initial_seed",
    "manual_plan",
    "rollback",
    "coordinated_purge",
]
GraphPublicationPlanSourceMode = Literal["initial_seed", "manual_plan"]
GraphPublicationItemKind = Literal["entity", "relation"]
GraphPublicationItemStatus = Literal["planned", "active", "stale", "degraded", "superseded"]


class GraphPublicationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_mode: GraphPublicationPlanSourceMode = "manual_plan"
    ontology_version_id: uuid.UUID | None = None
    include_drafts: bool = False
    dry_run: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    expected_parent_publication_id: uuid.UUID | None = None


class GraphPublicationActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    expected_manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class GraphPublicationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
    )


class GraphPublicationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    dry_run: bool = False


class GraphPublicationRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    status: GraphPublicationStatus
    healthy: bool
    publication_enabled: bool
    source_mode: GraphPublicationSourceMode
    manifest_version: str
    policy_version: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    include_drafts: bool
    parent_publication_id: uuid.UUID | None = None
    rollback_target_publication_id: uuid.UUID | None = None
    planned_by_user_id: uuid.UUID | None = None
    activated_by_user_id: uuid.UUID | None = None
    cancelled_by_user_id: uuid.UUID | None = None
    superseded_by_publication_id: uuid.UUID | None = None
    entity_count: int
    relation_count: int
    blocked_counts: dict[str, int]
    error_code: str | None = None
    planned_at: datetime | None = None
    activated_at: datetime | None = None
    superseded_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GraphPublicationCommandRead(GraphPublicationRead):
    policy_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dry_run: bool = False
    reused: bool = False


class GraphPublicationList(BaseModel):
    items: list[GraphPublicationRead]
    total: int
    page: int
    page_size: int
    publication_enabled: bool


class GraphPublicationItemRead(BaseModel):
    id: uuid.UUID
    publication_id: uuid.UUID
    item_kind: GraphPublicationItemKind
    entity_id: uuid.UUID | None = None
    relation_id: uuid.UUID | None = None
    item_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: GraphPublicationItemStatus
    support_evidence_ids: list[uuid.UUID]
    support_counts: dict[str, int]
    source_job_ids: list[uuid.UUID]
    fact_snapshot: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GraphPublicationItemList(BaseModel):
    items: list[GraphPublicationItemRead]
    total: int
    page: int
    page_size: int
