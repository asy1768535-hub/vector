from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.services.graph_catalog_contracts import (
    GraphCatalogFactStatus,
    GraphCatalogPublicationState,
    GraphCatalogSourceType,
)


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


LibrarySlug = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")]
TypeKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]*$")]


def _validate_properties_size(value: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("graph catalog properties are invalid") from exc
    if len(payload) > 65_536:
        raise ValueError("graph catalog properties exceed the response limit")
    return value


GraphCatalogProperties = Annotated[
    dict[str, Any],
    Field(max_length=100),
    AfterValidator(_validate_properties_size),
]


class GraphCatalogSearchRequest(StrictBaseModel):
    library_slugs: list[LibrarySlug] | None = Field(default=None, min_length=1, max_length=20)
    scope_id: uuid.UUID | None = None
    query: str | None = Field(default=None, min_length=1, max_length=160)
    ontology_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    type_keys: list[TypeKey] = Field(default_factory=list, max_length=20)
    statuses: list[GraphCatalogFactStatus] = Field(default_factory=list, max_length=20)
    source_types: list[GraphCatalogSourceType] = Field(default_factory=list, max_length=3)
    publication_state: GraphCatalogPublicationState = "all"
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_scope_and_uniqueness(self):
        if (self.library_slugs is None) == (self.scope_id is None):
            raise ValueError("exactly one graph catalog scope is required")
        for values in (
            self.library_slugs or [],
            self.ontology_version_ids,
            self.type_keys,
            self.statuses,
            self.source_types,
        ):
            if len(values) != len(set(values)):
                raise ValueError("graph catalog filters must be unique")
        return self


class GraphRelationCatalogSearchRequest(GraphCatalogSearchRequest):
    review_statuses: list[
        Literal["pending_review", "approved", "rejected", "not_required"]
    ] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_review_statuses(self):
        if len(self.review_statuses) != len(set(self.review_statuses)):
            raise ValueError("graph catalog filters must be unique")
        return self


class GraphCatalogLibraryRead(StrictBaseModel):
    id: uuid.UUID
    slug: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)


class GraphCatalogTypeRead(StrictBaseModel):
    id: uuid.UUID
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)


class GraphCatalogPublicationRead(StrictBaseModel):
    id: uuid.UUID
    status: Literal["active", "degraded"]
    item_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GraphCatalogEvidenceCountsRead(StrictBaseModel):
    evidence: int = Field(ge=0)
    documents: int = Field(ge=0)


class GraphCatalogExtractionRead(StrictBaseModel):
    job_id: uuid.UUID
    model_provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)


class GraphCatalogEntityListItemRead(StrictBaseModel):
    id: uuid.UUID
    library: GraphCatalogLibraryRead
    ontology_version_id: uuid.UUID
    entity_type: GraphCatalogTypeRead
    canonical_name: str = Field(min_length=1, max_length=512)
    normalized_name: str = Field(min_length=1, max_length=512)
    status: GraphCatalogFactStatus
    source_type: GraphCatalogSourceType
    authority_level: str | None = Field(default=None, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)
    governance_state_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    publication_state: Literal["published", "staged"]
    publication: GraphCatalogPublicationRead | None
    counts: GraphCatalogEvidenceCountsRead
    created_at: datetime
    updated_at: datetime


class GraphCatalogEntityPageRead(StrictBaseModel):
    contract_version: Literal["graph-catalog-entities-v1"] = "graph-catalog-entities-v1"
    items: list[GraphCatalogEntityListItemRead] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=4096)


class GraphCatalogRelationEndpointRead(StrictBaseModel):
    id: uuid.UUID
    canonical_name: str = Field(min_length=1, max_length=512)
    normalized_name: str = Field(min_length=1, max_length=512)
    entity_type: GraphCatalogTypeRead


class GraphCatalogRelationListItemRead(StrictBaseModel):
    id: uuid.UUID
    library: GraphCatalogLibraryRead
    ontology_version_id: uuid.UUID
    relation_type: GraphCatalogTypeRead
    direction: Literal["directed", "undirected"]
    source: GraphCatalogRelationEndpointRead
    target: GraphCatalogRelationEndpointRead
    status: GraphCatalogFactStatus
    review_status: Literal["pending_review", "approved", "rejected", "not_required"] | None
    source_type: GraphCatalogSourceType
    authority_level: str | None = Field(default=None, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)
    governance_state_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    publication_state: Literal["published", "staged"]
    publication: GraphCatalogPublicationRead | None
    counts: GraphCatalogEvidenceCountsRead
    created_at: datetime
    updated_at: datetime


class GraphCatalogRelationPageRead(StrictBaseModel):
    contract_version: Literal["graph-catalog-relations-v1"] = "graph-catalog-relations-v1"
    items: list[GraphCatalogRelationListItemRead] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=4096)


class GraphCatalogEvidenceLocatorRead(StrictBaseModel):
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    evidence_kind: str = Field(min_length=1, max_length=32)
    support_type: str | None = Field(default=None, max_length=32)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    title_path: list[str] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.page_start is not None and self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("page range is invalid")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
        ):
            raise ValueError("source range is invalid")
        return self


class GraphCatalogRelatedDocumentRead(StrictBaseModel):
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    title: str | None = Field(default=None, max_length=512)
    evidence_count: int = Field(ge=1)


class GraphCatalogAliasRead(StrictBaseModel):
    id: uuid.UUID
    alias: str = Field(min_length=1, max_length=512)
    normalized_alias: str = Field(min_length=1, max_length=512)
    source_type: GraphCatalogSourceType
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: Literal["pending_review", "active", "rejected", "disabled"]
    governance_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GraphCatalogExternalMappingRead(StrictBaseModel):
    id: uuid.UUID
    source_key: str = Field(min_length=1, max_length=128)
    external_type: str = Field(min_length=1, max_length=128)
    external_id: str = Field(min_length=1, max_length=512)
    lifecycle: Literal["active", "stale", "tombstoned"]
    source_version: str | None = Field(default=None, max_length=128)
    evidence_id: uuid.UUID | None = None
    source_locator: dict = Field(default_factory=dict)


class GraphCatalogEntityDetailRead(StrictBaseModel):
    contract_version: Literal["graph-catalog-entity-detail-v1"] = (
        "graph-catalog-entity-detail-v1"
    )
    entity: GraphCatalogEntityListItemRead
    properties: GraphCatalogProperties | None = None
    aliases: list[GraphCatalogAliasRead] = Field(max_length=100)
    alias_count: int = Field(ge=0)
    aliases_truncated: bool
    evidence: list[GraphCatalogEvidenceLocatorRead] = Field(max_length=100)
    evidence_count: int = Field(ge=0)
    evidence_truncated: bool
    documents: list[GraphCatalogRelatedDocumentRead] = Field(max_length=100)
    document_count: int = Field(ge=0)
    documents_truncated: bool
    related_relations: list[GraphCatalogRelationListItemRead] = Field(max_length=100)
    relation_count: int = Field(ge=0)
    relations_truncated: bool
    extraction: GraphCatalogExtractionRead | None = None
    external_mappings: list[GraphCatalogExternalMappingRead] = Field(
        default_factory=list, max_length=100
    )


class GraphCatalogRelationDetailRead(StrictBaseModel):
    contract_version: Literal["graph-catalog-relation-detail-v1"] = (
        "graph-catalog-relation-detail-v1"
    )
    relation: GraphCatalogRelationListItemRead
    properties: GraphCatalogProperties | None = None
    evidence: list[GraphCatalogEvidenceLocatorRead] = Field(max_length=100)
    evidence_count: int = Field(ge=0)
    evidence_truncated: bool
    documents: list[GraphCatalogRelatedDocumentRead] = Field(max_length=100)
    document_count: int = Field(ge=0)
    documents_truncated: bool
    extraction: GraphCatalogExtractionRead | None = None
    external_mappings: list[GraphCatalogExternalMappingRead] = Field(
        default_factory=list, max_length=100
    )
