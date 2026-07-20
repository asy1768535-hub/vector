from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.graph_normalization import normalize_graph_name_v1


EntityLinkingStatus = Literal["linked", "ambiguous", "not_found"]
EntityLinkingMethod = Literal["exact_canonical", "lexical_v1"]
EntityLinkingErrorCode = Literal[
    "library_not_found",
    "publication_changed",
    "entity_type_not_found",
    "entity_linking_disabled",
    "entity_linking_invalid_request",
    "entity_linking_limit_exceeded",
    "entity_linking_internal_error",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
    "entity_linking_timeout",
]


def _require_type_key(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("entity type key must be non-blank and trimmed")
    return value


Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TypeKey = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(_require_type_key),
]


class _StrictEntityLinkingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityLinkingMention(_StrictEntityLinkingModel):
    text: str
    entity_type_key: TypeKey | None = None

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed or len(trimmed) > 512:
            raise ValueError("mention text must contain 1 to 512 trimmed code points")
        if not normalize_graph_name_v1(value):
            raise ValueError("mention text must not normalize to empty")
        return value


class EntityLinkingResolveRequest(_StrictEntityLinkingModel):
    ontology_version_id: uuid.UUID
    expected_publication_id: uuid.UUID | None = None
    mentions: list[EntityLinkingMention] = Field(min_length=1, max_length=10)
    max_candidates_per_mention: int = Field(default=5, ge=1, le=10)


class EntityLinkingPublicationRead(_StrictEntityLinkingModel):
    id: uuid.UUID
    ontology_version_id: uuid.UUID
    manifest_version: Literal["v1"]
    manifest_hash: Sha256Hex
    ontology_schema_hash: Sha256Hex
    activated_at: datetime


class EntityLinkingCandidateRead(_StrictEntityLinkingModel):
    entity_id: uuid.UUID
    item_hash: Sha256Hex
    entity_type_id: uuid.UUID
    entity_type_key: TypeKey
    entity_type_label: str = Field(min_length=1, max_length=255)
    canonical_name: str = Field(min_length=1, max_length=512)
    score_micros: int = Field(ge=0, le=1_000_000)


class EntityLinkingResult(_StrictEntityLinkingModel):
    input_index: int = Field(ge=0, le=9)
    status: EntityLinkingStatus
    method: EntityLinkingMethod | None = None
    selected: EntityLinkingCandidateRead | None = None
    candidates: list[EntityLinkingCandidateRead] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_decision(self) -> EntityLinkingResult:
        if self.status == "linked":
            if self.method is None or self.selected is None or self.candidates:
                raise ValueError("linked results require method and selected only")
        elif self.status == "ambiguous":
            if self.method is not None or self.selected is not None or not self.candidates:
                raise ValueError("ambiguous results require candidates only")
        elif self.method is not None or self.selected is not None or self.candidates:
            raise ValueError("not_found results must not include a match")
        return self


class EntityLinkingCounts(_StrictEntityLinkingModel):
    mentions: int = Field(ge=1, le=10)
    linked_exact: int = Field(ge=0, le=10)
    linked_lexical: int = Field(ge=0, le=10)
    ambiguous: int = Field(ge=0, le=10)
    not_found: int = Field(ge=0, le=10)
    candidates: int = Field(ge=0, le=100)


class EntityLinkingResolveResponse(_StrictEntityLinkingModel):
    contract_version: Literal["v1"]
    policy_version: str = Field(min_length=1, max_length=128)
    publication: EntityLinkingPublicationRead
    results: list[EntityLinkingResult] = Field(min_length=1, max_length=10)
    counts: EntityLinkingCounts

    @model_validator(mode="after")
    def validate_counts(self) -> EntityLinkingResolveResponse:
        actual = (
            len(self.results),
            sum(row.method == "exact_canonical" for row in self.results),
            sum(row.method == "lexical_v1" for row in self.results),
            sum(row.status == "ambiguous" for row in self.results),
            sum(row.status == "not_found" for row in self.results),
            sum(len(row.candidates) for row in self.results),
        )
        declared = (
            self.counts.mentions,
            self.counts.linked_exact,
            self.counts.linked_lexical,
            self.counts.ambiguous,
            self.counts.not_found,
            self.counts.candidates,
        )
        if actual != declared:
            raise ValueError("response counts must match results")
        if [row.input_index for row in self.results] != list(range(len(self.results))):
            raise ValueError("results must preserve contiguous input order")
        return self


class EntityLinkingErrorResponse(_StrictEntityLinkingModel):
    detail: EntityLinkingErrorCode
