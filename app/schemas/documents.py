"""文档摄入 / 列表的 Pydantic schemas。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


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
    job_id: uuid.UUID


class DocumentRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    external_id: Optional[str] = None
    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    content_hash: str
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
    failed_jobs: int


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


# ── 文件导入 ──────────────────────────────────────────────────────────

class ImportFileDocResult(BaseModel):
    document_id: str
    title: str
    chunk_count: int
    status: str


class ImportFileResponse(BaseModel):
    status: str               # "success"
    imported_count: int
    documents: list[ImportFileDocResult]

