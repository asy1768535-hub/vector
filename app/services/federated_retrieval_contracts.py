from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.schemas.dify import DifyRecord
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)


FEDERATED_MAX_LIBRARIES = 20
FEDERATED_MAX_QUERY_CHARACTERS = 4_000
FEDERATED_MAX_TOP_K = 50
FEDERATED_MAX_CANDIDATE_K = 100
FEDERATED_CONTENT_EXCERPT_CHARACTERS = 4_000
FEDERATED_TITLE_CHARACTERS = 500


class FederatedRetrievalError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        incompatibilities: tuple[LibraryIncompatibility, ...] = (),
        message: str = "Federated retrieval failed",
    ) -> None:
        self.code = code[:64]
        self.incompatibilities = incompatibilities
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class FederatedRetrievalCommand:
    organization_id: uuid.UUID
    library_slugs: tuple[str, ...]
    query: str
    top_k: int
    candidate_k: int
    score_threshold: float

    def __post_init__(self) -> None:
        query = self.query.strip() if isinstance(self.query, str) else ""
        if (
            not isinstance(self.organization_id, uuid.UUID)
            or not isinstance(self.library_slugs, tuple)
            or not 1 <= len(self.library_slugs) <= FEDERATED_MAX_LIBRARIES
            or len(set(self.library_slugs)) != len(self.library_slugs)
            or any(
                not isinstance(slug, str) or not slug or len(slug) > 80
                for slug in self.library_slugs
            )
            or not query
            or len(query) > FEDERATED_MAX_QUERY_CHARACTERS
            or not isinstance(self.top_k, int)
            or isinstance(self.top_k, bool)
            or not 1 <= self.top_k <= FEDERATED_MAX_TOP_K
            or not isinstance(self.candidate_k, int)
            or isinstance(self.candidate_k, bool)
            or not self.top_k <= self.candidate_k <= FEDERATED_MAX_CANDIDATE_K
            or isinstance(self.score_threshold, bool)
            or not isinstance(self.score_threshold, (int, float))
            or not 0 <= float(self.score_threshold) <= 1
        ):
            raise FederatedRetrievalError("federated_request_invalid")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "score_threshold", float(self.score_threshold))


@dataclass(frozen=True, slots=True)
class FederatedSourceProjection:
    document_id: str | None = None
    document_revision_id: str | None = None
    document_revision: int | None = None
    chunk_id: str | None = None
    seq: int | None = None
    page: int | None = None
    title_path: tuple[str, ...] = ()
    external_id: str | None = None
    vector_score: float | None = None
    rerank_score: float | None = None
    local_rrf_score: float | None = None
    dense_rank: int | None = None
    keyword_rank: int | None = None
    rewrite_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FederatedHit:
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
    source: FederatedSourceProjection


@dataclass(frozen=True, slots=True)
class FederatedLibraryExecution:
    profile: LibraryCompatibilityProfile
    records: tuple[DifyRecord, ...]
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class FederatedLibraryTiming:
    library_slug: str
    candidate_count: int
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class FederatedRetrievalResult:
    assessment: CompatibilityAssessment
    hits: tuple[FederatedHit, ...]
    timings: tuple[FederatedLibraryTiming, ...]
    total_elapsed_ms: int
