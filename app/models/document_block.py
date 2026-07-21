from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (
        Index("ix_document_blocks_revision_seq", "document_revision_id", "seq"),
        Index("ix_document_blocks_library_revision_kind", "library_id", "document_revision_id", "block_kind"),
        Index("ix_document_blocks_parent", "parent_block_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    parent_block_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    block_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    position: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
