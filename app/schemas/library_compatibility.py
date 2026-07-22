from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.admin import LIBRARY_SLUG_RE


CompatibilityChannel = Literal["text", "graph"]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryCompatibilityCheckRequest(StrictBaseModel):
    library_slugs: list[str] = Field(..., min_length=1, max_length=20)
    channels: list[CompatibilityChannel] = Field(default_factory=lambda: ["text"])

    @field_validator("library_slugs")
    @classmethod
    def _validate_library_slugs(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("library_slugs must be unique")
        if any(LIBRARY_SLUG_RE.fullmatch(value) is None for value in values):
            raise ValueError("library_slugs must contain valid Library slugs")
        return values

    @field_validator("channels")
    @classmethod
    def _validate_channels(
        cls, values: list[CompatibilityChannel]
    ) -> list[CompatibilityChannel]:
        if not values or len(set(values)) != len(values):
            raise ValueError("channels must be a non-empty unique list")
        return values


class LibraryCompatibilityProfileRead(StrictBaseModel):
    library_id: uuid.UUID
    library_slug: str
    library_name: str
    index_state: str
    embedding_ready: bool
    retrieval_ready: bool
    graph_ready: bool | None = None
    embedding_profile_sha256: str | None = None
    retrieval_profile_sha256: str
    graph_profile_sha256: str | None = None


class LibraryIncompatibilityRead(StrictBaseModel):
    library_slug: str
    reason_codes: list[str]


class LibraryCompatibilityAssessmentRead(StrictBaseModel):
    contract_version: str
    organization_id: uuid.UUID
    reference_library_slug: str
    channels: list[CompatibilityChannel]
    compatible: bool
    libraries: list[LibraryCompatibilityProfileRead]
    incompatibilities: list[LibraryIncompatibilityRead]


class EmbeddingProfileVerificationRead(StrictBaseModel):
    library_id: uuid.UUID
    library_slug: str
    contract_version: str
    model: str
    dimension: int
    probe_fingerprint: str
    verified_at: datetime
