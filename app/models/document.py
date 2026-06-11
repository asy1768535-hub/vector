"""documents：业务文档元信息（chunk 内容在 chunks 表 + Qdrant payload）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_library_hash", "library_id", "content_hash"),
        Index("ix_documents_library_external", "library_id", "external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    doc_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
