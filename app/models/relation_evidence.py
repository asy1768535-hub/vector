from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RelationEvidence(Base):
    __tablename__ = "relation_evidence"
    __table_args__ = (
        CheckConstraint(
            "support_type IN ('supports','contradicts','mentions','source')",
            name="ck_relation_evidence_support_type",
        ),
        CheckConstraint(
            "status IN ('active','stale','deleted')",
            name="ck_relation_evidence_status",
        ),
        UniqueConstraint(
            "library_id",
            "relation_id",
            "evidence_id",
            name="uq_relation_evidence_library_relation_evidence",
        ),
        Index("ix_relation_evidence_library_relation_status", "library_id", "relation_id", "status"),
        Index("ix_relation_evidence_library_evidence", "library_id", "evidence_id"),
        Index(
            "ix_relation_evidence_library_revision_status",
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
    relation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_relations.id", ondelete="CASCADE"),
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
    support_type: Mapped[str] = mapped_column(String(32), nullable=False, default="supports")
    quote_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_text_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_span: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="SET NULL",
            name="fk_relation_evidence_created_by_job",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
