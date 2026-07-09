from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','stale','deleted')",
            name="ck_entity_mentions_status",
        ),
        CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_entity_mentions_source_type",
        ),
        Index("ix_entity_mentions_library_entity_status", "library_id", "entity_id", "status"),
        Index("ix_entity_mentions_library_evidence", "library_id", "evidence_id"),
        Index(
            "ix_entity_mentions_library_revision_status",
            "library_id",
            "document_revision_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("evidence_units.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    chunk_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    mention_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    quote_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_text_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_span: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
