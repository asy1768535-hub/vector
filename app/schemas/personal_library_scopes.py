from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.admin import LIBRARY_SLUG_RE


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeSelection(StrictBaseModel):
    organization_id: uuid.UUID
    library_slugs: list[str] = Field(..., min_length=1, max_length=20)

    @field_validator("library_slugs")
    @classmethod
    def _slugs(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            LIBRARY_SLUG_RE.fullmatch(value) is None for value in values
        ):
            raise ValueError("library_slugs must be unique valid Library slugs")
        return values


class NamedScopeCreate(ScopeSelection):
    name: str = Field(..., min_length=1, max_length=80)


class NamedScopeReplace(NamedScopeCreate):
    expected_updated_at: datetime


class NamedScopeDelete(StrictBaseModel):
    organization_id: uuid.UUID
    expected_updated_at: datetime


class ScopeMetadataRead(StrictBaseModel):
    scope_id: uuid.UUID
    organization_id: uuid.UUID
    scope_kind: str
    name: str | None
    item_count: int
    created_at: datetime
    updated_at: datetime


class ScopeLibraryRead(StrictBaseModel):
    library_id: uuid.UUID
    library_slug: str
    library_name: str


class RemovedScopeItemRead(StrictBaseModel):
    library_id: uuid.UUID
    reason_codes: list[str]


class ResolvedScopeRead(StrictBaseModel):
    scope_id: uuid.UUID
    organization_id: uuid.UUID
    scope_kind: str
    name: str | None
    stored_item_count: int
    libraries: list[ScopeLibraryRead]
    removed: list[RemovedScopeItemRead]
    created_at: datetime
    updated_at: datetime
