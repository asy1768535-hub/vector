"""管理员接口 Pydantic schemas。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.services.graph_extraction_safety import normalize_allowed_security_levels
from app.services.knowledge_artifact_policy import (
    normalize_knowledge_artifact_security_levels,
)

# 库唯一ID：只允许大小写英文字母和下划线（其它一律不允许）。
# 同时它也是 Dify knowledge_id、URL 路径段、Qdrant collection 名、约定全文源表名，
# 限定为合法 SQL 标识符字符集可避免下游各处转义问题。
_SLUG_RE = re.compile(r"^[A-Za-z_]{2,80}$")


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Users ────────────────────────────────────────────────────────────────
class AdminUserCreate(StrictBaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    is_superuser: bool = False


class AdminUserUpdate(StrictBaseModel):
    username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


class AdminResetPassword(StrictBaseModel):
    # 专用重置密码入参：与 AdminUserUpdate 分开，避免 password 混入普通字段更新。
    password: str = Field(..., min_length=8, max_length=128)


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
    retrieval_mode: Optional[str] = Field(
        default=None, pattern="^(dense|hybrid)$",
        description="检索模式：dense=纯向量(默认)；hybrid=向量+pg_trgm 关键词 RRF 融合。不传按 dense。",
    )
    # 普通 UI 开关：默认开启时后端按约定生成 PGSQL 全文源；高级用户仍可传 source_config 覆盖。
    source_enrichment_enabled: bool = Field(
        default=True,
        description="是否启用按知识库配置的 PGSQL 全文源补全；false 时不写 source_config。",
    )
    # 默认按「约定」自动生成 PGSQL 全文源（表=slug、列=content、外键=text_id、bigint、库=.env）。
    # 显式传入非空 source_config 时使用自定义结构（高级/脚本用法）。
    source_config: Optional[dict[str, Any]] = Field(
        default=None,
        description="高级用法：完整跨库补全配置；不传则按 source_enrichment_enabled 决定是否按约定自动生成。结构见 source_enrichment.parse_source_config。",
    )
    revision_retention_enabled: bool = False
    revision_retention_days: int = Field(default=60, ge=30, le=60)
    revision_retention_notice_days: int = Field(default=7, ge=1, le=14)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("库唯一ID 只能包含大小写英文字母和下划线，长度 2-80")
        return v

    @model_validator(mode="after")
    def _check_chunk_params(self):
        _validate_chunk_overlap(self)
        return _validate_revision_retention_policy(self)


def _validate_chunk_overlap(model):
    """#9：chunk_overlap 必须 < chunk_size（两者都显式给出时）。

    否则建库能成功、上传时 LangChain 才抛 ValueError。仅当两值都非 None 才能交叉校验；
    只改其一时由 splitter 入口对「最终生效值」兜底（见 splitter.split_text）。
    """
    size, overlap = model.chunk_size, model.chunk_overlap
    if size is not None and overlap is not None and overlap >= size:
        raise ValueError(f"chunk_overlap（{overlap}）必须小于 chunk_size（{size}）")
    return model


def _validate_revision_retention_policy(model):
    retention_days = model.revision_retention_days
    notice_days = model.revision_retention_notice_days
    if (
        retention_days is not None
        and notice_days is not None
        and notice_days >= retention_days
    ):
        raise ValueError("revision_retention_notice_days must be less than retention days")
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
    retrieval_mode: Optional[str] = Field(default=None, pattern="^(dense|hybrid)$")
    source_enrichment_enabled: Optional[bool] = Field(default=None)
    # source_config 哨兵：不传=不改；传 {} =清空；传非空 dict=自定义。
    source_config: Optional[dict[str, Any]] = Field(default=None)
    graph_extraction_enabled: Optional[bool] = None
    external_llm_enabled: Optional[bool] = None
    graph_extraction_allowed_security_levels: Optional[list[str]] = None
    knowledge_artifact_auto_enabled: Optional[bool] = None
    summary_artifact_enabled: Optional[bool] = None
    outline_artifact_enabled: Optional[bool] = None
    knowledge_artifact_external_model_enabled: Optional[bool] = None
    knowledge_artifact_allowed_security_levels: Optional[list[str]] = None
    revision_retention_enabled: Optional[bool] = None
    revision_retention_days: Optional[int] = Field(default=None, ge=30, le=60)
    revision_retention_notice_days: Optional[int] = Field(default=None, ge=1, le=14)

    @field_validator("graph_extraction_enabled", "external_llm_enabled", mode="before")
    @classmethod
    def _reject_null_graph_opt_in(cls, value):
        if value is None:
            raise ValueError("graph extraction opt-in cannot be null")
        return value

    @field_validator("graph_extraction_allowed_security_levels", mode="before")
    @classmethod
    def _validate_graph_security_levels(cls, value):
        if value is None:
            raise ValueError("graph extraction security levels cannot be null")
        return normalize_allowed_security_levels(value)

    @field_validator(
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
        mode="before",
    )
    @classmethod
    def _reject_null_artifact_switch(cls, value):
        if value is None:
            raise ValueError("knowledge artifact switch cannot be null")
        return value

    @field_validator("revision_retention_enabled", mode="before")
    @classmethod
    def _reject_null_retention_switch(cls, value):
        if value is None:
            raise ValueError("revision retention switch cannot be null")
        return value

    @field_validator("knowledge_artifact_allowed_security_levels", mode="before")
    @classmethod
    def _validate_artifact_security_levels(cls, value):
        if value is None:
            raise ValueError("knowledge artifact security levels cannot be null")
        return normalize_knowledge_artifact_security_levels(value)

    @model_validator(mode="after")
    def _check_chunk_params(self):
        _validate_chunk_overlap(self)
        return _validate_revision_retention_policy(self)


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
    retrieval_mode: str = "dense"
    qdrant_collection: str
    source_config: Optional[dict[str, Any]] = None
    graph_extraction_enabled: bool = False
    external_llm_enabled: bool = False
    graph_extraction_allowed_security_levels: list[str] = Field(default_factory=list)
    knowledge_artifact_auto_enabled: bool = False
    summary_artifact_enabled: bool = False
    outline_artifact_enabled: bool = False
    knowledge_artifact_external_model_enabled: bool = False
    knowledge_artifact_allowed_security_levels: list[str] = Field(default_factory=list)
    revision_retention_enabled: bool = False
    revision_retention_days: int = 60
    revision_retention_notice_days: int = 7
    lifecycle_mode: str = "managed"                 # #6 managed | external
    index_state: str = "ready"                      # #6 ready | rebuilding | failed
    active_rebuild_operation_id: Optional[uuid.UUID] = None
    created_at: datetime
    deleted_at: Optional[datetime] = None

    @field_validator("revision_retention_enabled", mode="before")
    @classmethod
    def _default_retention_enabled(cls, value):
        return False if value is None else value

    @field_validator("revision_retention_days", mode="before")
    @classmethod
    def _default_retention_days(cls, value):
        return 60 if value is None else value

    @field_validator("revision_retention_notice_days", mode="before")
    @classmethod
    def _default_retention_notice_days(cls, value):
        return 7 if value is None else value

    model_config = {"from_attributes": True}


# ── Library FAQ（常用问题）────────────────────────────────────────────────
def _strip_required_question(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("question 去掉首尾空格后不能为空")
    return v


class LibraryFAQCreate(BaseModel):
    question: str = Field(..., max_length=500)
    sort_order: int = 0
    is_active: bool = True

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        return _strip_required_question(v)


class LibraryFAQUpdate(BaseModel):
    # 不传 = 不改；传 question 必须 strip 后非空（传空白串 → 422）
    question: Optional[str] = Field(default=None, max_length=500)
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

    @field_validator("question")
    @classmethod
    def _q(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _strip_required_question(v)


class LibraryFAQRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    question: str
    sort_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Permissions ──────────────────────────────────────────────────────────
class PermissionGrant(StrictBaseModel):
    user_id: uuid.UUID
    library_slug: str
    actions: list[str] = Field(..., min_length=1, description="Subset of {read, insert, delete, admin}.")


class PermissionRevoke(StrictBaseModel):
    user_id: uuid.UUID
    library_slug: str
    actions: Optional[list[str]] = Field(
        default=None,
        description="If null/omitted, revokes ALL actions for (user, library).",
    )


class PermissionMatrixRow(BaseModel):
    library_slug: str
    actions: list[str]
    # 可选：活动库的真实名称（仅 /me/permissions 填充，向后兼容；缺失时前端回退 slug）。
    library_name: Optional[str] = None


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


# ── Operations status（运行状态监控，docs/26 / 批次 C2） ──────────────────
class ServiceInstanceLatest(BaseModel):
    """某 service_type 最新一个非 stopping 实例（供页面展示 host/pid/最后心跳）。"""
    instance_id: str
    hostname: str
    pid: int
    last_seen_at: datetime
    seconds_since_last_seen: int
    heartbeat_metadata: Optional[dict[str, Any]] = None


class ServiceStatus(BaseModel):
    """单类进程（api / embedding_worker / cleanup_worker）的聚合在线状态。"""
    service_type: str
    status: str                                  # online | degraded | offline
    online_instances: int
    known_instances: int
    latest: Optional[ServiceInstanceLatest] = None


class CleanupOutboxStats(BaseModel):
    """Cleanup Outbox 按状态聚合；dead_letter = 已达 max_attempts 的 failed。"""
    pending: int
    processing: int
    done: int
    failed: int
    dead_letter: int
    total: int


class LibraryIndexStats(BaseModel):
    """按 index_state 计活动库数（未删库）。"""
    rebuilding: int
    failed: int


class RebuildOperationStatus(BaseModel):
    """活动重建 operation 的进度摘要（last_error 截断、不含堆栈）。"""
    library_slug: str
    status: str                                  # preparing | running
    expected_job_count: int
    done_job_count: int
    progress_pct: float
    last_error: Optional[str] = None


class GraphPublicationStats(BaseModel):
    active: int
    degraded: int


class OperationsStatus(BaseModel):
    """GET /admin/operations/status 响应：三类进程在线 + 任务/Outbox/重建聚合。"""
    now: datetime
    offline_threshold_seconds: int
    services: list[ServiceStatus]
    embedding_jobs: EmbeddingJobStats
    cleanup_outbox: CleanupOutboxStats
    libraries: LibraryIndexStats
    graph_publications: GraphPublicationStats
    rebuild_operations: list[RebuildOperationStatus]
