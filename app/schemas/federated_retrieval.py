from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.admin import LIBRARY_SLUG_RE
from app.services.federated_retrieval_contracts import (
    FEDERATED_MAX_CANDIDATE_K,
    FEDERATED_MAX_LIBRARIES,
    FEDERATED_MAX_QUERY_CHARACTERS,
    FEDERATED_MAX_TOP_K,
)


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FederatedRetrievalTestRequest(StrictBaseModel):
    library_slugs: list[str] = Field(
        ...,
        min_length=1,
        max_length=FEDERATED_MAX_LIBRARIES,
    )
    query: str = Field(..., min_length=1, max_length=FEDERATED_MAX_QUERY_CHARACTERS)
    top_k: int = Field(default=10, ge=1, le=FEDERATED_MAX_TOP_K)
    candidate_k: int = Field(default=30, ge=1, le=FEDERATED_MAX_CANDIDATE_K)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("library_slugs")
    @classmethod
    def _validate_library_slugs(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("library_slugs must be unique")
        if any(LIBRARY_SLUG_RE.fullmatch(value) is None for value in values):
            raise ValueError("library_slugs must contain valid Library slugs")
        return values

    @field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_candidate_bound(self):
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return self


class FederatedLibraryProfileRead(StrictBaseModel):
    library_id: uuid.UUID
    library_slug: str
    library_name: str
    embedding_profile_sha256: str
    retrieval_profile_sha256: str


class FederatedSourceRead(StrictBaseModel):
    document_id: str | None = None
    document_revision_id: str | None = None
    document_revision: int | None = None
    chunk_id: str | None = None
    seq: int | None = None
    page: int | None = None
    title_path: list[str] = Field(default_factory=list)
    external_id: str | None = None
    vector_score: float | None = None
    rerank_score: float | None = None
    local_rrf_score: float | None = None
    dense_rank: int | None = None
    keyword_rank: int | None = None
    rewrite_sources: list[Literal["original", "rule", "llm"]] = Field(default_factory=list)


class FederatedHitRead(StrictBaseModel):
    rank: int
    fusion_score: float
    library_id: uuid.UUID
    library_slug: str
    library_name: str
    local_rank: int
    local_score: float
    title: str
    content_excerpt: str
    content_truncated: bool
    source: FederatedSourceRead


class FederatedLibraryTimingRead(StrictBaseModel):
    library_slug: str
    candidate_count: int
    elapsed_ms: int


class FederatedRetrievalTestResponse(StrictBaseModel):
    contract_version: str
    fusion_contract_version: str
    organization_id: uuid.UUID
    library_slugs: list[str]
    query: str
    top_k: int
    candidate_k: int
    score_threshold: float
    profiles: list[FederatedLibraryProfileRead]
    hits: list[FederatedHitRead]
    timings: list[FederatedLibraryTimingRead]
    total_elapsed_ms: int
