from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GraphExtractionCreate(BaseModel):
    document_id: uuid.UUID
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class GraphExtractionRerun(BaseModel):
    client_idempotency_key: str = Field(min_length=8, max_length=128)


class GraphExtractionJobRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    ontology_version_id: uuid.UUID
    trigger_type: str
    execution_mode: str
    status: str
    current_stage: str | None = None
    input_fingerprint: str
    rerun_of_job_id: uuid.UUID | None = None
    retry_generation: int
    model_provider: str
    model_name: str
    prompt_version: str
    extractor_version: str
    output_parser_version: str
    context_policy_version: str
    extraction_policy_version: str
    normalization_rule_version: str
    confidence_policy_version: str
    document_parser_version: str
    chunking_strategy_version: str
    counts: dict[str, Any]
    statistics: dict[str, Any]
    error_code: str | None = None
    sensitive_payload_purged_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class GraphExtractionJobList(BaseModel):
    items: list[GraphExtractionJobRead]
    total: int
    limit: int
    offset: int


class GraphExtractionUnitRead(BaseModel):
    id: uuid.UUID
    ordinal: int
    center_chunk_id: uuid.UUID
    center_evidence_id: uuid.UUID
    status: str
    model_attempt_count: int
    retryable: bool
    error_code: str | None = None
    latest_attempt_status: str | None = None
    latest_parse_status: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class GraphExtractionUnitList(BaseModel):
    items: list[GraphExtractionUnitRead]
    total: int
    limit: int
    offset: int


class GraphExtractionCandidateRead(BaseModel):
    candidate_type: Literal["entity", "relation"]
    id: uuid.UUID
    candidate_key: str
    ontology_type_key: str
    status: str
    final_confidence: float | None = None
    review_reason: str | None = None
    matched_formal_id: uuid.UUID | None = None
    materialized_formal_id: uuid.UUID | None = None
    created_at: datetime | None = None


class GraphExtractionCandidateList(BaseModel):
    items: list[GraphExtractionCandidateRead]
    total: int
    limit: int
    offset: int
