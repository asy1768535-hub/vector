from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    sort_order: int = 0


class FolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    sort_order: int | None = None


class FolderRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    path: str
    sort_order: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class DocumentFolderUpdate(BaseModel):
    folder_id: uuid.UUID | None = None


class SyncSourceCreate(BaseModel):
    source_key: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    display_name: str = Field(..., min_length=1, max_length=255)
    source_type: str | None = Field(default=None, max_length=64)
    config: dict[str, Any] | None = None
    status: Literal["active", "disabled"] = "active"


class SyncSourceUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    source_type: str | None = Field(default=None, max_length=64)
    config: dict[str, Any] | None = None
    status: Literal["active", "disabled"] | None = None


class SyncSourceRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    source_key: str
    display_name: str
    source_type: str | None = None
    config: dict[str, Any] | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class SyncDocumentUpsertRequest(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=512)
    text: str = Field(..., min_length=1)
    metadata: dict[str, Any] | None = None
    management: dict[str, Any] | None = None
    folder_path: str | None = None
    visibility_scope: str | None = Field(default=None, max_length=64)
    security_level: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    source_event_id: str | None = Field(default=None, max_length=128)
    splitter: Literal["text", "markdown", "none"] = "text"


class SyncDocumentDeleteRequest(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=512)
    request_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    source_event_id: str | None = Field(default=None, max_length=128)


class SyncDocumentResult(BaseModel):
    external_id: str
    operation: str
    document_id: uuid.UUID | None = None
    document_revision_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    document_status: str | None = None
    revision_status: str | None = None
    job_status: str | None = None
    chunk_count: int = 0


class SyncBatchItem(BaseModel):
    action: Literal["upsert", "delete"]
    external_id: str = Field(..., min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=512)
    text: str | None = None
    metadata: dict[str, Any] | None = None
    management: dict[str, Any] | None = None
    folder_path: str | None = None
    visibility_scope: str | None = Field(default=None, max_length=64)
    security_level: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    source_event_id: str | None = Field(default=None, max_length=128)
    splitter: Literal["text", "markdown", "none"] = "text"


class SyncBatchRequest(BaseModel):
    items: list[SyncBatchItem] = Field(..., min_length=1)


class SyncBatchError(BaseModel):
    index: int
    external_id: str | None = None
    action: str | None = None
    error: str


class SyncBatchResponse(BaseModel):
    status: Literal["success", "partial", "failed"]
    succeeded_count: int
    failed_count: int
    results: list[SyncDocumentResult] = Field(default_factory=list)
    errors: list[SyncBatchError] = Field(default_factory=list)


class RevisionRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    library_id: uuid.UUID
    revision_no: int
    title: str | None = None
    status: str
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class BlockRead(BaseModel):
    id: uuid.UUID
    document_revision_id: uuid.UUID
    seq: int
    block_kind: str
    title_path: list[Any] | None = None
    page_start: int | None = None
    page_end: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    text: str | None = None
    content: dict[str, Any] | None = None
    position: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class ChunkRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID | None = None
    block_id: uuid.UUID | None = None
    evidence_id: uuid.UUID | None = None
    seq: int
    chunk_kind: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    title_path: list[Any] | None = None
    source_start: int | None = None
    source_end: int | None = None
    position: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class EvidenceRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    document_block_id: uuid.UUID | None = None
    evidence_kind: str
    status: str
    text_quote: str | None = None
    text_window: str = ""
    window_start: int | None = None
    window_end: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    title_path: list[Any] | None = None
    position: dict[str, Any] | None = None
    evidence_metadata: dict[str, Any] | None = None


class ChunkSourceRead(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID | None = None
    evidence_id: uuid.UUID | None = None
    chunk_seq: int
    chunk_text: str
    text_window: str = ""
    window_start: int | None = None
    window_end: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    title_path: list[Any] | None = None
    position: dict[str, Any] | None = None
