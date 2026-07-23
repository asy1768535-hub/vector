from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.graph_catalog import (
    GraphCatalogEntityDetailRead,
    GraphCatalogEntityListItemRead,
    GraphCatalogRelationDetailRead,
    GraphCatalogRelationListItemRead,
)
from app.schemas.knowledge_catalog import (
    CatalogDocumentDetailRead,
    CatalogEntityKnowledgeUnitRead,
    CatalogEvidenceDetailRead,
    CatalogRelationKnowledgeUnitRead,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogFactStatus,
    GraphCatalogPublicationState,
    GraphCatalogSourceType,
)


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


LibrarySlug = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$"),
]
TypeKey = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]*$"),
]
RequestId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class PublicScopeSelection(StrictBaseModel):
    library_slugs: list[LibrarySlug] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
    )
    scope_id: uuid.UUID | None = None

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {"library_slugs": ["projects", "compliance"]},
                {"scope_id": "00000000-0000-4000-8000-000000000001"},
            ]
        },
    )

    @model_validator(mode="after")
    def validate_exact_scope(self):
        if (self.library_slugs is None) == (self.scope_id is None):
            raise ValueError("exactly one of library_slugs or scope_id is required")
        if self.library_slugs is not None and len(self.library_slugs) != len(
            set(self.library_slugs)
        ):
            raise ValueError("library_slugs must be unique")
        return self


class PublicScopeRequest(StrictBaseModel):
    scope: PublicScopeSelection


class PublicScopeValidateRequest(PublicScopeRequest):
    channels: list[Literal["text", "graph"]] = Field(
        default_factory=lambda: ["text", "graph"],
        min_length=1,
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_channels(self):
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("channels must be unique")
        return self


class PublicGraphSearchBase(PublicScopeRequest):
    query: str | None = Field(default=None, min_length=1, max_length=160)
    ontology_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    type_keys: list[TypeKey] = Field(default_factory=list, max_length=20)
    statuses: list[GraphCatalogFactStatus] = Field(default_factory=list, max_length=20)
    source_types: list[GraphCatalogSourceType] = Field(default_factory=list, max_length=3)
    publication_state: GraphCatalogPublicationState = "all"
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_unique_filters(self):
        for values in (
            self.ontology_version_ids,
            self.type_keys,
            self.statuses,
            self.source_types,
        ):
            if len(values) != len(set(values)):
                raise ValueError("search filters must be unique")
        return self


class PublicEntitySearchRequest(PublicGraphSearchBase):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "scope": {"library_slugs": ["projects"]},
                    "query": "Acme",
                    "statuses": ["active"],
                    "publication_state": "published",
                    "limit": 25,
                }
            ]
        },
    )


class PublicRelationSearchRequest(PublicGraphSearchBase):
    review_statuses: list[
        Literal["pending_review", "approved", "rejected", "not_required"]
    ] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_review_statuses(self):
        if len(self.review_statuses) != len(set(self.review_statuses)):
            raise ValueError("review_statuses must be unique")
        return self


class PublicRetrievalRequest(PublicScopeRequest):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=50)
    candidate_k: int = Field(default=20, ge=1, le=100)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "scope": {"library_slugs": ["projects", "compliance"]},
                    "query": "Which company invested in the contractor?",
                    "top_k": 8,
                    "candidate_k": 20,
                    "score_threshold": 0.2,
                }
            ]
        },
    )

    @model_validator(mode="after")
    def validate_candidate_limit(self):
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return self


class PublicAnswerRequest(PublicRetrievalRequest):
    pass


class PublicLibraryRead(StrictBaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    index_state: str = Field(min_length=1, max_length=32)


class PublicResolvedScopeRead(StrictBaseModel):
    organization_id: uuid.UUID
    scope_id: uuid.UUID | None = None
    libraries: list[PublicLibraryRead] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_scope_identity(self):
        if any(row.organization_id != self.organization_id for row in self.libraries):
            raise ValueError("resolved scope libraries must share one Organization")
        if len({row.id for row in self.libraries}) != len(self.libraries):
            raise ValueError("resolved scope libraries must be unique")
        return self


class PublicIncompatibilityRead(StrictBaseModel):
    library_slug: str = Field(min_length=1, max_length=80)
    reason_codes: list[str] = Field(min_length=1, max_length=20)


class PublicChannelCompatibilityRead(StrictBaseModel):
    channel: Literal["text", "graph"]
    compatible: bool
    incompatibilities: list[PublicIncompatibilityRead] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_incompatibilities(self):
        if self.compatible != (not self.incompatibilities):
            raise ValueError("compatibility flag must match incompatibilities")
        return self


class PublicLibrariesResponse(StrictBaseModel):
    contract_version: Literal["public-libraries-v1"] = "public-libraries-v1"
    request_id: RequestId
    libraries: list[PublicLibraryRead] = Field(max_length=500)
    truncated: bool


class PublicScopeValidationResponse(StrictBaseModel):
    contract_version: Literal["public-scope-validation-v1"] = (
        "public-scope-validation-v1"
    )
    request_id: RequestId
    scope: PublicResolvedScopeRead
    compatibility: list[PublicChannelCompatibilityRead] = Field(
        min_length=1,
        max_length=2,
    )


class PublicDocumentResponse(StrictBaseModel):
    contract_version: Literal["public-document-v1"] = "public-document-v1"
    request_id: RequestId
    library: PublicLibraryRead
    document: CatalogDocumentDetailRead


class PublicEvidenceResponse(StrictBaseModel):
    contract_version: Literal["public-evidence-v1"] = "public-evidence-v1"
    request_id: RequestId
    library: PublicLibraryRead
    evidence: CatalogEvidenceDetailRead


class PublicEntityResponse(StrictBaseModel):
    contract_version: Literal["public-entity-v1"] = "public-entity-v1"
    request_id: RequestId
    entity: GraphCatalogEntityDetailRead


class PublicRelationResponse(StrictBaseModel):
    contract_version: Literal["public-relation-v1"] = "public-relation-v1"
    request_id: RequestId
    relation: GraphCatalogRelationDetailRead


class PublicEntitySearchResponse(StrictBaseModel):
    contract_version: Literal["public-entity-search-v1"] = "public-entity-search-v1"
    request_id: RequestId
    scope: PublicResolvedScopeRead
    items: list[GraphCatalogEntityListItemRead] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=4096)


class PublicRelationSearchResponse(StrictBaseModel):
    contract_version: Literal["public-relation-search-v1"] = (
        "public-relation-search-v1"
    )
    request_id: RequestId
    scope: PublicResolvedScopeRead
    items: list[GraphCatalogRelationListItemRead] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=4096)


class PublicSourceRead(StrictBaseModel):
    rank: int = Field(ge=1, le=50)
    library_id: uuid.UUID
    library_slug: str = Field(min_length=1, max_length=80)
    library_name: str = Field(min_length=1, max_length=160)
    document_id: uuid.UUID | None = None
    document_revision_id: uuid.UUID | None = None
    document_revision: int | None = Field(default=None, ge=1)
    chunk_id: uuid.UUID | None = None
    seq: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    title_path: list[str] = Field(default_factory=list, max_length=16)
    title: str = Field(default="", max_length=500)
    score: float = Field(ge=0)
    vector_score: float | None = None
    rerank_score: float | None = None


class PublicChunkRead(StrictBaseModel):
    rank: int = Field(ge=1, le=50)
    library_id: uuid.UUID
    library_slug: str = Field(min_length=1, max_length=80)
    document_id: uuid.UUID | None = None
    document_revision_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    title: str = Field(default="", max_length=500)
    content: str = Field(max_length=4000)
    content_truncated: bool
    score: float = Field(ge=0)


class PublicGraphEntityRead(StrictBaseModel):
    library: PublicLibraryRead
    fact: CatalogEntityKnowledgeUnitRead


class PublicGraphRelationRead(StrictBaseModel):
    library: PublicLibraryRead
    fact: CatalogRelationKnowledgeUnitRead


class PublicGraphRead(StrictBaseModel):
    available: bool
    entities: list[PublicGraphEntityRead] = Field(max_length=50)
    relations: list[PublicGraphRelationRead] = Field(max_length=50)
    documents_examined: int = Field(ge=0, le=5)
    truncated: bool

    @model_validator(mode="after")
    def validate_graph_identity(self):
        entity_keys = {(row.library.id, row.fact.entity_id) for row in self.entities}
        relation_keys = {(row.library.id, row.fact.relation_id) for row in self.relations}
        if len(entity_keys) != len(self.entities) or len(relation_keys) != len(
            self.relations
        ):
            raise ValueError("public graph facts must retain unique Library identity")
        if not self.available and (self.entities or self.relations):
            raise ValueError("unavailable graph context must be empty")
        return self


def _validate_grounding_rows(
    sources: list[PublicSourceRead],
    chunks: list[PublicChunkRead],
) -> None:
    source_ranks = [row.rank for row in sources]
    chunk_ranks = [row.rank for row in chunks]
    expected = list(range(1, len(chunks) + 1))
    if source_ranks != expected or chunk_ranks != expected:
        raise ValueError("sources and chunks must use the same contiguous ranks")
    for source, chunk in zip(sources, chunks, strict=True):
        if (
            source.library_id,
            source.library_slug,
            source.document_id,
            source.document_revision_id,
            source.chunk_id,
        ) != (
            chunk.library_id,
            chunk.library_slug,
            chunk.document_id,
            chunk.document_revision_id,
            chunk.chunk_id,
        ):
            raise ValueError("source and chunk identity must match")


class PublicRetrievalResponse(StrictBaseModel):
    contract_version: Literal["public-retrieval-v1"] = "public-retrieval-v1"
    request_id: RequestId
    scope: PublicResolvedScopeRead
    sources: list[PublicSourceRead] = Field(max_length=50)
    chunks: list[PublicChunkRead] = Field(max_length=50)
    graph: PublicGraphRead

    @model_validator(mode="after")
    def validate_grounding(self):
        _validate_grounding_rows(self.sources, self.chunks)
        return self


class PublicAnswerResponse(StrictBaseModel):
    contract_version: Literal["public-answer-v1"] = "public-answer-v1"
    request_id: RequestId
    answer: str = Field(max_length=131072)
    sources: list[PublicSourceRead] = Field(max_length=50)
    chunks: list[PublicChunkRead] = Field(max_length=50)
    graph: PublicGraphRead

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "contract_version": "public-answer-v1",
                    "request_id": "0123456789abcdef0123456789abcdef",
                    "answer": "The source documents identify Acme as the investor.",
                    "sources": [],
                    "chunks": [],
                    "graph": {
                        "available": False,
                        "entities": [],
                        "relations": [],
                        "documents_examined": 0,
                        "truncated": False,
                    },
                }
            ]
        },
    )

    @model_validator(mode="after")
    def validate_grounding(self):
        _validate_grounding_rows(self.sources, self.chunks)
        return self


class PublicStreamMetaRead(StrictBaseModel):
    contract_version: Literal["public-answer-v1"] = "public-answer-v1"
    request_id: RequestId


class PublicStreamDeltaRead(StrictBaseModel):
    request_id: RequestId
    text: str = Field(min_length=1, max_length=2048)


class PublicErrorDetailRead(StrictBaseModel):
    library_slug: str = Field(min_length=1, max_length=80)
    reason_codes: list[str] = Field(min_length=1, max_length=20)


class PublicErrorRead(StrictBaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    request_id: RequestId
    message: str = Field(min_length=1, max_length=255)
    details: list[PublicErrorDetailRead] = Field(default_factory=list, max_length=20)


class PublicErrorEnvelope(StrictBaseModel):
    error: PublicErrorRead

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "scope_incompatible",
                        "request_id": "0123456789abcdef0123456789abcdef",
                        "message": "The selected knowledge libraries are incompatible.",
                        "details": [
                            {
                                "library_slug": "compliance",
                                "reason_codes": ["embedding_profile_mismatch"],
                            }
                        ],
                    }
                }
            ]
        },
    )
