"""文档摄入 / 列表的 Pydantic schemas。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, Field


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
    job_id: Optional[uuid.UUID] = None   # 无新任务（如 no-op upsert / 去重命中）时为 null


class DocumentRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    external_id: Optional[str] = None
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
    location: Optional[dict[str, Any]] = None
    chunk_id: str
    chunk_seq: int
    legacy: bool
    fallback_chunk: str = ""


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

