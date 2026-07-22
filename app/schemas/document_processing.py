from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.document_processing_contracts import PROCESSING_STAGES, ProcessingStage


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ProcessingStatus = Literal[
    "not_started",
    "queued",
    "processing",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
    "partially_succeeded",
]


class GraphProcessingCountsRead(StrictBaseModel):
    total: int = Field(ge=0)
    queued: int = Field(ge=0)
    processing: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    retryable_failed: int = Field(ge=0)
    model_attempts: int = Field(ge=0)


class DocumentProcessingStageRead(StrictBaseModel):
    stage: ProcessingStage
    availability: Literal["enabled", "disabled"]
    status: ProcessingStatus
    job_id: uuid.UUID | None
    retry_generation: int = Field(ge=0)
    attempt_count: int | None = Field(default=None, ge=0)
    safe_error_code: str | None = Field(default=None, min_length=1, max_length=64)
    retryable: bool
    created_at: datetime | None
    started_at: datetime | None
    updated_at: datetime | None
    finished_at: datetime | None
    graph_counts: GraphProcessingCountsRead | None = None


class DocumentProcessingRead(StrictBaseModel):
    contract_version: Literal["document-processing-v1"] = "document-processing-v1"
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    stages: list[DocumentProcessingStageRead] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_stage_order(self):
        if tuple(item.stage for item in self.stages) != PROCESSING_STAGES:
            raise ValueError("processing stages must use the canonical order")
        return self


class DocumentProcessingRetryRequest(StrictBaseModel):
    source_job_id: uuid.UUID
    retry_generation: int = Field(ge=0)
