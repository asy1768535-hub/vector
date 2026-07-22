from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaxonomyCreate(StrictBaseModel):
    version_key: str = Field(..., min_length=1, max_length=64)
    version_no: int = Field(..., ge=1, le=2_147_483_647)
    description: str | None = Field(default=None, max_length=1000)


class TaxonomyDraftUpdate(StrictBaseModel):
    expected_status: Literal["draft"]
    expected_updated_at: datetime
    description: str | None = Field(default=None, max_length=1000)


class TaxonomyVersionCopy(StrictBaseModel):
    expected_source_status: Literal["active", "disabled"]
    expected_source_updated_at: datetime
    description: str | None = Field(default=None, max_length=1000)


class TaxonomyActivate(StrictBaseModel):
    expected_status: Literal["draft"]
    expected_updated_at: datetime


class ClassificationLabelCreate(StrictBaseModel):
    expected_taxonomy_updated_at: datetime
    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    parent_label_id: uuid.UUID | None = None
    sort_order: int = Field(default=0, ge=0, le=1_000_000)
    status: Literal["active", "disabled"] = "active"


class ClassificationLabelUpdate(ClassificationLabelCreate):
    expected_updated_at: datetime


class LibraryClassificationLabelsReplace(StrictBaseModel):
    taxonomy_version_id: uuid.UUID
    expected_taxonomy_updated_at: datetime
    expected_label_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    label_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)


class TaxonomyRead(StrictBaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    version_key: str
    version_no: int
    status: Literal["draft", "active", "disabled"]
    parent_version_id: uuid.UUID | None
    description: str | None
    created_by_user_id: uuid.UUID | None
    activated_by_user_id: uuid.UUID | None
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClassificationLabelRead(StrictBaseModel):
    id: uuid.UUID
    taxonomy_version_id: uuid.UUID
    key: str
    label: str
    description: str | None
    parent_label_id: uuid.UUID | None
    sort_order: int
    status: Literal["active", "disabled"]
    created_at: datetime
    updated_at: datetime


class LibraryClassificationSelectionRead(StrictBaseModel):
    organization_id: uuid.UUID
    library_id: uuid.UUID
    library_slug: str
    library_name: str
    taxonomy: TaxonomyRead
    labels: list[ClassificationLabelRead]
