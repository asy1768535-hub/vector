from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


EVIDENCE_STATUS_ACTIVE = "active"


class EvidenceUnit(Base):
    __tablename__ = "evidence_units"
    __table_args__ = (
        Index("ix_evidence_units_revision", "document_revision_id"),
        Index("ix_evidence_units_block", "document_block_id"),
        Index("ix_evidence_units_library_status", "library_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_block_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    position: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    text_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_quote_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    visibility_scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    security_level: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
