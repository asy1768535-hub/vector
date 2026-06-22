"""sys_libraries：知识库定义。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Library(Base):
    __tablename__ = "sys_libraries"
    __table_args__ = (
        CheckConstraint("lifecycle_mode IN ('managed','external')", name="ck_lib_lifecycle_mode"),
        CheckConstraint("index_state IN ('ready','rebuilding','failed')", name="ck_lib_index_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    embedding_model: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_distance: Mapped[str] = mapped_column(String(16), nullable=False, default="cosine")
    # 库级 embedding 服务 URL；null = 用全局 settings.embedding_base_url
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    # 库级 embedding 单批大小；null = 用全局 settings.embed_batch_size
    embed_batch_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 库级 rerank 开关；null = 继承全局 settings.rerank_enabled
    rerank_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # 库级图片 OCR 开关；null = 继承全局 settings.ocr_enabled（默认关）
    ocr_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # 库级 docx 表格感知切块开关；null = 继承全局 settings.docx_table_aware（默认关）。
    # 开启后 docx 上传走 extract_docx_segments + chunk_segments（每表单独成块带表头/章节上下文），
    # 表格召回更稳但 chunk 数/成本上升；散文为主的库默认扁平更优（实测）。
    docx_table_aware: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    qdrant_collection: Mapped[str] = mapped_column(String(128), nullable=False)

    # 生命周期归属（#6/#7 设计 §4.5）：managed=本系统管理(参与 revision/tombstone 过滤)；
    # external=外部系统/源库补全管理(绕过生命周期回查、且本系统禁止写/rebuild→409)。
    lifecycle_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="managed")
    # 索引状态：ready=正常；rebuilding=重建中(写/检索 503)；failed=重建失败(检索 503)。
    index_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    # 当前进行中的 rebuild operation（rebuilding 时非空；ready 时 NULL）。循环 FK 用 use_alter。
    active_rebuild_operation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rebuild_operations.id", use_alter=True, name="fk_lib_active_rebuild_op"),
        nullable=True,
    )

    # 源数据补全配置（JSONB）：当 Qdrant payload 只存外键、正文在别的业务库时，
    # 检索后按外键回查源库大表把正文拼回。null = 不补全（走 payload.text）。
    # 结构见 app/services/source_enrichment.py:parse_source_config
    source_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
