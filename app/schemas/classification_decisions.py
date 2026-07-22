from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassificationProposalRead(StrictBaseModel):
    id: uuid.UUID
    label_id: uuid.UUID | None
    label_key: str | None
    label: str | None
    proposed_key: str | None
    proposed_label: str | None
    role: Literal["primary", "secondary"]
    rank: int
    confidence_micros: int
    status: str
    reason_codes: list[str]
    created_at: datetime


class ClassificationRunRead(StrictBaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    taxonomy_version_id: uuid.UUID
    generation_no: int
    retry_generation: int
    status: str
    reason_codes: list[str]
    policy_version: str
    min_confidence_micros: int
    min_margin_micros: int
    classifier_version: str
    model_provider: str
    model_name: str
    prompt_version: str
    error_code: str | None
    proposals: list[ClassificationProposalRead]
    created_at: datetime
    finished_at: datetime


class ClassificationReviewPageRead(StrictBaseModel):
    items: list[ClassificationRunRead]
    total: int
    limit: int
    offset: int


class ClassificationDecisionRead(StrictBaseModel):
    id: uuid.UUID
    label_id: uuid.UUID
    label_key: str
    label: str
    role: Literal["primary", "secondary"]
    ordinal: int
    confidence_micros: int | None


class EffectiveClassificationRead(StrictBaseModel):
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    state: Literal["unclassified", "pending_review", "classified", "failed"]
    decision_set_id: uuid.UUID | None
    taxonomy_version_id: uuid.UUID | None
    source: Literal["model", "manual"] | None
    generation_no: int | None
    decisions: list[ClassificationDecisionRead]
    latest_run_id: uuid.UUID | None
    latest_run_status: str | None
    latest_error_code: str | None


class ClassificationReviewRequest(StrictBaseModel):
    expected_run_status: Literal["pending_review", "blocked_manual"]
    expected_effective_decision_set_id: uuid.UUID | None
    action: Literal["accept", "change", "reject"]
    primary_label_id: uuid.UUID | None = None
    secondary_label_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _selection_shape(self):
        has_selection = self.primary_label_id is not None or bool(self.secondary_label_ids)
        if (self.action == "change") != has_selection:
            raise ValueError("change requires a selection and other actions forbid it")
        if self.action == "change" and self.primary_label_id is None:
            raise ValueError("change requires primary_label_id")
        return self


class ManualClassificationSetRequest(StrictBaseModel):
    expected_effective_decision_set_id: uuid.UUID | None
    primary_label_id: uuid.UUID
    secondary_label_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)


class ManualClassificationRemoveRequest(StrictBaseModel):
    expected_effective_decision_set_id: uuid.UUID | None
