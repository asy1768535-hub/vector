from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


GraphFactWriteStatus = Literal["draft", "pending_review", "active"]
GraphSourceType = Literal["manual", "imported", "extracted"]
RelationSupportType = Literal["supports", "contradicts", "mentions", "source"]


class GraphEntityCreate(BaseModel):
    ontology_version_id: uuid.UUID
    entity_type_id: uuid.UUID
    canonical_name: str = Field(..., min_length=1, max_length=512)
    properties: dict[str, Any] | None = None
    status: GraphFactWriteStatus = "active"
    source_type: GraphSourceType = "manual"
    confidence: float | None = None


class GraphEntityRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    entity_type_id: uuid.UUID
    canonical_name: str
    normalized_name: str
    properties: dict[str, Any] | None = None
    status: str
    source_type: str
    authority_level: str | None = None
    confidence: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class GraphRelationCreate(BaseModel):
    relation_type_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    properties: dict[str, Any] | None = None
    status: GraphFactWriteStatus = "pending_review"
    source_type: GraphSourceType = "manual"
    confidence: float | None = None
    schema_boundary_clear: bool = True


class GraphRelationRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    relation_type_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    properties: dict[str, Any] | None = None
    status: str
    review_status: str | None = None
    source_type: str
    authority_level: str | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RelationEvidenceCreate(BaseModel):
    evidence_id: uuid.UUID
    support_type: RelationSupportType = "supports"
    chunk_id: uuid.UUID | None = None
    source_span: dict[str, Any] | None = None
    confidence: float | None = None


class RelationEvidenceRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    relation_id: uuid.UUID
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    support_type: str
    quote_text: str | None = None
    evidence_text_snapshot: str | None = None
    source_span: dict[str, Any] | None = None
    confidence: float | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
