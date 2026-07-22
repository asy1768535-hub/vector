from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaxonomyBootstrapLabelInput(StrictBaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    parent_key: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    status: Literal["active", "disabled"] = "active"


class TaxonomyTemplateApply(StrictBaseModel):
    request_id: uuid.UUID
    version_key: str = Field(default="document-category", min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)


class TaxonomyBootstrapImport(StrictBaseModel):
    request_id: uuid.UUID
    source_name: str = Field(..., min_length=1, max_length=160)
    source_version: str = Field(..., min_length=1, max_length=64)
    version_key: str = Field(default="document-category", min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    labels: list[TaxonomyBootstrapLabelInput] = Field(..., min_length=1, max_length=500)


class TaxonomyBootstrapLlmRequest(StrictBaseModel):
    request_id: uuid.UUID
    sample_revision_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=20)
    version_key: str = Field(default="document-category", min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)


class TaxonomyBootstrapLlmLabel(StrictBaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    parent_key: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int = Field(..., strict=True, ge=0, le=1_000_000)


class TaxonomyBootstrapLlmOutput(StrictBaseModel):
    description: str | None = Field(default=None, max_length=1000)
    labels: list[TaxonomyBootstrapLlmLabel] = Field(..., min_length=3, max_length=50)


class TaxonomyBootstrapTemplateRead(StrictBaseModel):
    key: str
    version: str
    description: str
    template_hash: str
    label_count: int


class TaxonomyBootstrapWarningRead(StrictBaseModel):
    code: str
    left_key: str
    right_key: str


class TaxonomyBootstrapSourceRead(StrictBaseModel):
    ordinal: int
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_content_hash: str
    security_level: str


class TaxonomyBootstrapRunRead(StrictBaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    request_id: uuid.UUID
    source_type: Literal["builtin_template", "admin_import", "llm_proposal"]
    source_key: str
    source_version: str
    source_hash: str
    status: Literal["processing", "succeeded", "failed"]
    warning_items: list[TaxonomyBootstrapWarningRead]
    output_taxonomy_id: uuid.UUID | None
    model_provider: str | None
    model_name: str | None
    prompt_version: str | None
    error_code: str | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    sources: list[TaxonomyBootstrapSourceRead] = Field(default_factory=list)
