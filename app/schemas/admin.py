"""管理员接口 Pydantic schemas。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# 库唯一ID：只允许大小写英文字母和下划线（其它一律不允许）。
# 同时它也是 Dify knowledge_id、URL 路径段、Qdrant collection 名、约定全文源表名，
# 限定为合法 SQL 标识符字符集可避免下游各处转义问题。
_SLUG_RE = re.compile(r"^[A-Za-z_]{2,80}$")


# ── Users ────────────────────────────────────────────────────────────────
class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    is_superuser: bool = False


class AdminUserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


class AdminUserRead(BaseModel):
    id: uuid.UUID
    email: EmailStr
    username: Optional[str] = None
    display_name: Optional[str] = None
    is_active: bool
    is_superuser: bool
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Libraries ────────────────────────────────────────────────────────────
class LibraryCreate(BaseModel):
    slug: str = Field(..., description="库唯一ID（大小写字母+下划线）；也是 Dify knowledge_id。")
    name: str = Field(..., min_length=1, max_length=160)
    description: Optional[str] = None
    embedding_model: Optional[str] = Field(default=None, description="Defaults to global EMBEDDING_MODEL.")
    embedding_dim: Optional[int] = Field(default=None, ge=64, le=8192)
    embedding_base_url: Optional[str] = Field(
        default=None, max_length=512,
        description="Per-library embedding endpoint URL; null = use global setting.",
    )
    vector_distance: str = Field(default="cosine", pattern="^(cosine|euclid|dot)$")
    chunk_size: Optional[int] = Field(default=None, ge=200, le=8000)
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=2000)
    embed_batch_size: Optional[int] = Field(
        default=None, ge=1, le=256,
        description="Per-library embedding batch size; null = use global EMBED_BATCH_SIZE.",
    )
    rerank_enabled: Optional[bool] = Field(
        default=None, description="Per-library rerank toggle; null = inherit global RERANK_ENABLED.",
    )
    ocr_enabled: Optional[bool] = Field(
        default=None, description="Per-library image OCR toggle; null = inherit global OCR_ENABLED.",
    )
    docx_table_aware: Optional[bool] = Field(
        default=None, description="Per-library docx table-aware chunking; null = inherit global DOCX_TABLE_AWARE.",
    )
    # 默认按「约定」自动生成 PGSQL 全文源（表=slug、列=content、外键=text_id、bigint、库=.env）。
    # 仅当显式传入 source_config 时才用自定义结构（高级/脚本用法）。
    source_config: Optional[dict[str, Any]] = Field(
        default=None,
        description="高级用法：完整跨库补全配置；不传则按约定自动生成。结构见 source_enrichment.parse_source_config。",
    )

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("库唯一ID 只能包含大小写英文字母和下划线，长度 2-80")
        return v

    @model_validator(mode="after")
    def _check_chunk_params(self):
        return _validate_chunk_overlap(self)


def _validate_chunk_overlap(model):
    """#9：chunk_overlap 必须 < chunk_size（两者都显式给出时）。

    否则建库能成功、上传时 LangChain 才抛 ValueError。仅当两值都非 None 才能交叉校验；
    只改其一时由 splitter 入口对「最终生效值」兜底（见 splitter.split_text）。
    """
    size, overlap = model.chunk_size, model.chunk_overlap
    if size is not None and overlap is not None and overlap >= size:
        raise ValueError(f"chunk_overlap（{overlap}）必须小于 chunk_size（{size}）")
    return model


class LibraryUpdate(BaseModel):
    """所有字段可改。注意：改 embedding_dim/vector_distance 后 Qdrant 现有 collection 结构对不上，
    需要走 POST /admin/libraries/{slug}/rebuild-collection 重建。"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = None
    chunk_size: Optional[int] = Field(default=None, ge=200, le=8000)
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=2000)
    embedding_model: Optional[str] = Field(default=None, max_length=80)
    embedding_dim: Optional[int] = Field(default=None, ge=64, le=8192)
    vector_distance: Optional[str] = Field(default=None, pattern="^(cosine|euclid|dot)$")
    embedding_base_url: Optional[str] = Field(default=None, max_length=512)
    embed_batch_size: Optional[int] = Field(default=None, ge=1, le=256)
    rerank_enabled: Optional[bool] = Field(default=None)
    ocr_enabled: Optional[bool] = Field(default=None)
    docx_table_aware: Optional[bool] = Field(default=None)
    # source_config 哨兵：不传=不改；传 {} =清空；传非空 dict=自定义。
    source_config: Optional[dict[str, Any]] = Field(default=None)

    @model_validator(mode="after")
    def _check_chunk_params(self):
        return _validate_chunk_overlap(self)


class LibraryRead(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: Optional[str] = None
    embedding_model: str
    embedding_dim: int
    vector_distance: str
    embedding_base_url: Optional[str] = None
    chunk_size: int
    chunk_overlap: int
    embed_batch_size: Optional[int] = None
    rerank_enabled: Optional[bool] = None
    ocr_enabled: Optional[bool] = None
    docx_table_aware: Optional[bool] = None
    qdrant_collection: str
    source_config: Optional[dict[str, Any]] = None
    lifecycle_mode: str = "managed"                 # #6 managed | external
    index_state: str = "ready"                      # #6 ready | rebuilding | failed
    active_rebuild_operation_id: Optional[uuid.UUID] = None
    created_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Permissions ──────────────────────────────────────────────────────────
class PermissionGrant(BaseModel):
    user_id: uuid.UUID
    library_slug: str
    actions: list[str] = Field(..., min_length=1, description="Subset of {read, insert, delete, admin}.")


class PermissionRevoke(BaseModel):
    user_id: uuid.UUID
    library_slug: str
    actions: Optional[list[str]] = Field(
        default=None,
        description="If null/omitted, revokes ALL actions for (user, library).",
    )


class PermissionMatrixRow(BaseModel):
    library_slug: str
    actions: list[str]


# ── Audit log ────────────────────────────────────────────────────────────
class AuditLogRead(BaseModel):
    id: uuid.UUID
    actor_user_id: Optional[uuid.UUID] = None
    action: str
    target: Optional[dict] = None
    at: datetime

    model_config = {"from_attributes": True}


# ── Embedding jobs ───────────────────────────────────────────────────────
class EmbeddingJobRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision: int = 1                      # #6 该 job 对应的索引版本
    rebuild_operation_id: Optional[uuid.UUID] = None
    status: str                                     # pending|processing|done|failed|superseded
    worker_id: Optional[str] = None
    attempt_count: int
    last_error: Optional[str] = None
    created_at: datetime
    claimed_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EmbeddingJobStats(BaseModel):
    """按状态聚合的任务计数（任务监控统计条用）。"""
    pending: int
    processing: int
    done: int
    failed: int
    total: int
