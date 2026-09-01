"""documents：业务文档元信息（chunk 内容在 chunks 表 + Qdrant payload）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Document(Base):
    __tablename__ = "documents"
    # #14/#15：文档身份的「活动行」部分唯一索引（只约束未删行）。
    #   - 带 external_id：库内 (library_id, external_id) 唯一 → external_id 即身份。
    #   - 不带 external_id：库内 (library_id, content_hash) 唯一 → 内容去重。
    # 两个索引互斥（按 external_id 是否为空划分），同时充当对应查询的加速索引。
    __table_args__ = (
        Index(
            "uq_documents_library_external_no_source_active",
            "library_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "sync_source_id IS NULL AND external_id IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
        Index(
            "uq_documents_library_sync_external_active",
            "library_id",
            "sync_source_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "sync_source_id IS NOT NULL AND external_id IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
        Index(
            "uq_documents_library_hash_active",
            "library_id",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "sync_source_id IS NULL AND external_id IS NULL "
                "AND source_path IS NULL AND deleted_at IS NULL"
            ),
        ),
        Index(
            "uq_documents_library_source_path_active",
            "library_id",
            "source_path",
            unique=True,
            postgresql_where=text(
                "source_path IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
        Index("ix_documents_folder", "folder_id"),
        Index("ix_documents_current_revision_id", "current_revision_id"),
        Index("ix_documents_latest_revision_id", "latest_revision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    folder_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    sync_source_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    doc_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 索引版本（#6）：内容/title/metadata/任何入 payload 字段变更或重建都 +1。
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    current_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    latest_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    visibility_scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    security_level: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
