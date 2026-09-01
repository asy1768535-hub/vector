from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


GraphRetrievalDirection = Literal["outbound", "inbound", "both"]
GraphRelationDirection = Literal["directed", "undirected"]
GraphSourceType = Literal["manual", "imported", "extracted"]
GraphRetrievalErrorCode = Literal[
    "library_not_found",
    "seed_not_found",
    "seed_ambiguous",
    "publication_changed",
    "relation_type_not_found",
    "graph_retrieval_disabled",
    "graph_retrieval_invalid_request",
    "graph_retrieval_limit_exceeded",
    "graph_retrieval_internal_error",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
    "graph_retrieval_timeout",
]

def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    if value != value.strip():
        raise ValueError("value must not have leading or trailing whitespace")
    return value


Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TypeKey = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_require_non_blank)]
Confidence = Annotated[float, Field(ge=0, le=1)]


class _StrictGraphRetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphRetrievalSeed(_StrictGraphRetrievalModel):
    entity_id: uuid.UUID | None = None
    canonical_name: str | None = Field(default=None, min_length=1, max_length=512)
    entity_type_key: TypeKey | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> GraphRetrievalSeed:
        has_id = self.entity_id is not None
        has_name = self.canonical_name is not None
        if has_id == has_name:
            raise ValueError("seed must provide exactly one of entity_id and canonical_name")
        if self.canonical_name is not None and not self.canonical_name.strip():
            raise ValueError("canonical_name must not be blank")
        if self.entity_type_key is not None:
            if not has_name:
                raise ValueError("entity_type_key requires canonical_name")
            if not self.entity_type_key.strip():
                raise ValueError("entity_type_key must not be blank")
        return self


class GraphRetrievalQueryRequest(_StrictGraphRetrievalModel):
    ontology_version_id: uuid.UUID
    expected_publication_id: uuid.UUID | None = None
    seeds: list[GraphRetrievalSeed] = Field(min_length=1, max_length=10)
    direction: GraphRetrievalDirection = "both"
    relation_type_keys: list[TypeKey] = Field(default_factory=list, max_length=64)
    max_hops: int = Field(default=1, ge=0, le=3)
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_relations: int = Field(default=200, ge=1, le=200)
    include_evidence_locators: bool = True

    @field_validator("relation_type_keys")
    @classmethod
    def reject_blank_relation_type_keys(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("relation_type_keys must not contain blank values")
        return values

    @model_validator(mode="after")
    def require_room_for_all_seeds(self) -> GraphRetrievalQueryRequest:
        if self.max_nodes < len(self.seeds):
            raise ValueError("max_nodes must cover every seed")
        return self


class GraphRetrievalPublicationRead(_StrictGraphRetrievalModel):
    id: uuid.UUID
    ontology_version_id: uuid.UUID
    manifest_version: Literal["v1"]
    manifest_hash: Sha256Hex
    activated_at: datetime


class GraphRetrievalEntityTypeRead(_StrictGraphRetrievalModel):
    id: uuid.UUID
    key: TypeKey
    label: str = Field(min_length=1, max_length=255)


class GraphRetrievalRelationTypeRead(_StrictGraphRetrievalModel):
    id: uuid.UUID
    key: TypeKey
    label: str = Field(min_length=1, max_length=255)
    direction: GraphRelationDirection


class GraphRetrievalEvidenceLocator(_StrictGraphRetrievalModel):
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    document_block_id: uuid.UUID | None = None
    evidence_kind: str = Field(min_length=1, max_length=32)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> GraphRetrievalEvidenceLocator:
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must be greater than or equal to page_start")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
        ):
            raise ValueError("source_end must be greater than or equal to source_start")
        return self


class GraphRetrievalNodeRead(_StrictGraphRetrievalModel):
    id: uuid.UUID
    item_hash: Sha256Hex
    entity_type: GraphRetrievalEntityTypeRead
    canonical_name: str = Field(min_length=1, max_length=512)
    normalized_name: str = Field(min_length=1, max_length=512)
    source_type: GraphSourceType
    confidence: Confidence | None = None
    depth: int = Field(ge=0, le=3)
    evidence: list[GraphRetrievalEvidenceLocator] = Field(default_factory=list, max_length=20)


class GraphRetrievalRelationRead(_StrictGraphRetrievalModel):
    id: uuid.UUID
    item_hash: Sha256Hex
    relation_type: GraphRetrievalRelationTypeRead
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    source_type: GraphSourceType
    confidence: Confidence | None = None
    depth: int = Field(ge=1, le=3)
    evidence: list[GraphRetrievalEvidenceLocator] = Field(default_factory=list, max_length=20)


class GraphRetrievalSeedMatch(_StrictGraphRetrievalModel):
    input_index: int = Field(ge=0, le=9)
    entity_id: uuid.UUID


class GraphRetrievalCounts(_StrictGraphRetrievalModel):
    seeds: int = Field(ge=1, le=10)
    nodes: int = Field(ge=1, le=100)
    relations: int = Field(ge=0, le=200)
    evidence_locators: int = Field(ge=0, le=6000)


class GraphRetrievalTruncation(_StrictGraphRetrievalModel):
    nodes: bool
    relations: bool
    evidence: bool


class GraphRetrievalQueryResponse(_StrictGraphRetrievalModel):
    contract_version: Literal["v1"]
    publication: GraphRetrievalPublicationRead
    seed_matches: list[GraphRetrievalSeedMatch] = Field(min_length=1, max_length=10)
    nodes: list[GraphRetrievalNodeRead] = Field(min_length=1, max_length=100)
    relations: list[GraphRetrievalRelationRead] = Field(default_factory=list, max_length=200)
    counts: GraphRetrievalCounts
    truncated: GraphRetrievalTruncation

    @model_validator(mode="after")
    def validate_counts(self) -> GraphRetrievalQueryResponse:
        evidence_count = sum(len(row.evidence) for row in (*self.nodes, *self.relations))
        actual = (
            len(self.seed_matches),
            len(self.nodes),
            len(self.relations),
            evidence_count,
        )
        declared = (
            self.counts.seeds,
            self.counts.nodes,
            self.counts.relations,
            self.counts.evidence_locators,
        )
        if actual != declared:
            raise ValueError("response counts must match response collections")
        return self


class GraphRetrievalAmbiguousCandidate(_StrictGraphRetrievalModel):
    entity_id: uuid.UUID
    entity_type_key: TypeKey


class GraphRetrievalErrorResponse(_StrictGraphRetrievalModel):
    detail: GraphRetrievalErrorCode
    candidates: list[GraphRetrievalAmbiguousCandidate] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_candidates(self) -> GraphRetrievalErrorResponse:
        if self.detail == "seed_ambiguous" and not self.candidates:
            raise ValueError("seed_ambiguous requires candidates")
        if self.detail != "seed_ambiguous" and self.candidates:
            raise ValueError("only seed_ambiguous may include candidates")
        return self
