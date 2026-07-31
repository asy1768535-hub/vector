"""文档摄入 / 列表的 Pydantic schemas。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class DocumentIngestRequest(BaseModel):
    external_id: Optional[str] = Field(default=None, max_length=255, description="Caller-supplied dedup key.")
    title: Optional[str] = Field(default=None, max_length=512)
    text: str = Field(..., min_length=1, description="Raw text / Markdown / JSON-string to embed.")
    metadata: Optional[dict[str, Any]] = None
    splitter: str = Field(default="text", pattern="^(text|markdown|none)$")


class DocumentIngestResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    chunk_count: int
    document_status: Optional[str] = None
    revision_status: Optional[str] = None
    job_status: Optional[str] = None
    document_revision_id: Optional[uuid.UUID] = None
    job_id: Optional[uuid.UUID] = None   # 无新任务（如 no-op upsert / 去重命中）时为 null


class DocumentRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    external_id: Optional[str] = None
    source_path: Optional[str] = None
    folder_id: Optional[uuid.UUID] = None
    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("doc_metadata", "metadata"),
    )
    content_hash: str
    current_revision: int = 1            # #6 索引版本
    status: str
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LibraryStats(BaseModel):
    library_slug: str
    document_count: int
    chunk_count: int
    pending_jobs: int
    processing_jobs: int
    done_jobs: int
    failed_jobs: int
    total_jobs: int


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="检索词")
    limit: int = Field(default=5, ge=1, le=20, description="最大返回条数，限制在 1-20")


class QueryResultItem(BaseModel):
    text: str = Field(..., description="分片正文")
    similarity: float = Field(..., description="相似度得分")
    document_id: str = Field(..., description="所属文档 ID")
    chunk_id: str = Field(..., description="分片 ID")
    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class QueryResponse(BaseModel):
    results: list[QueryResultItem]


class DocumentSourceLocationResponse(BaseModel):
    document_title: Optional[str] = None
    file_type: Optional[str] = None
    text_window: str = ""
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    source_ranges: list[dict[str, Any]] = Field(default_factory=list)
    location: Optional[dict[str, Any]] = None
    chunk_id: str
    chunk_seq: int
    legacy: bool
    fallback_chunk: str = ""


class DocumentFullSourceResponse(BaseModel):
    document_id: uuid.UUID
    document_title: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    revision: int
    normalized_text: str
    text_length: int
    created_at: datetime
    updated_at: datetime


# ── 文件导入 ──────────────────────────────────────────────────────────

class ImportFileDocResult(BaseModel):
    document_id: str
    title: str
    chunk_count: int
    status: str
    job_id: Optional[str] = None          # 摄入任务 ID，供上传后查任务状态
    external_id: Optional[str] = None     # 调用方去重键（命中 upsert 时回显）
    operation: Optional[str] = None       # created | updated | unchanged（向后兼容，旧客户端可忽略）


class ImportFileDocError(BaseModel):
    index: int                # 在本次解析出的文档列表中的序号（0-based）
    title: Optional[str] = None
    external_id: Optional[str] = None
    error: str                # 失败原因


class ImportFileResponse(BaseModel):
    status: str               # "success" | "partial"（全失败直接返回 400，不会是本响应）
    imported_count: int
    failed_count: int = 0
    documents: list[ImportFileDocResult]
    errors: list[ImportFileDocError] = []


class ImportConfigurationRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(gt=0)
    chunk_bytes: int = Field(gt=0)
    max_files_per_selection: int = Field(gt=0)
    upload_concurrency: int = Field(gt=0)
    allowed_extensions: list[str] = Field(min_length=1)


class ImportSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=512)
    relative_path: str | None = Field(default=None, max_length=2048)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(gt=0)
    last_modified_millis: int | None = Field(default=None, ge=0)
    external_id: str | None = Field(default=None, max_length=512)
    replace_document_id: uuid.UUID | None = None
    security_level: str | None = Field(default=None, max_length=64)
    graph_extraction_requested: bool = False


class ImportJobRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    batch_id: uuid.UUID
    file_name: str
    relative_path: str | None
    size_bytes: int
    upload_offset: int
    status: Literal[
        "uploading",
        "queued",
        "processing",
        "succeeded",
        "failed",
        "cancelled",
        "superseded",
    ]
    current_stage: Literal[
        "uploading",
        "queued",
        "validating",
        "parsing",
        "chunking",
        "embedding",
        "graph",
        "completed",
    ]
    attempt_count: int = Field(ge=0)
    last_error: str | None
    result_operation: str | None
    document_id: uuid.UUID | None
    document_revision_id: uuid.UUID | None
    embedding_job_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    upload_completed_at: datetime | None
    claimed_at: datetime | None
    finished_at: datetime | None


class ImportSessionRead(ImportJobRead):
    chunk_bytes: int = Field(gt=0)

