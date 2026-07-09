from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


REVISION_STATUS_PENDING = "pending"
REVISION_STATUS_PROCESSING = "processing"
REVISION_STATUS_READY = "ready"
REVISION_STATUS_FAILED = "failed"
REVISION_STATUS_SUPERSEDED = "superseded"
REVISION_STATUS_DELETED = "deleted"


class DocumentRevision(Base):
    __tablename__ = "document_revisions"
    __table_args__ = (
        Index("ix_document_revisions_document_revision_no", "document_id", "revision_no"),
        Index("ix_document_revisions_library_status", "library_id", "status"),
        Index("ix_document_revisions_document_status", "document_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    document_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    normalized_text_storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    chunking_strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    chunking_strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    visibility_scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    security_level: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
