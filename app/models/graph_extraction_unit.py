from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphExtractionUnit(Base):
    __tablename__ = "graph_extraction_units"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled')",
            name="ck_graph_extraction_units_status",
        ),
        CheckConstraint(
            "model_attempt_count >= 0",
            name="ck_graph_extraction_units_attempt_count",
        ),
        CheckConstraint(
            "(status = 'processing' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_graph_extraction_units_claim_fields",
        ),
        UniqueConstraint(
            "job_id",
            "center_chunk_id",
            name="uq_graph_extraction_units_job_chunk",
        ),
        UniqueConstraint(
            "job_id",
            "ordinal",
            name="uq_graph_extraction_units_job_ordinal",
        ),
        UniqueConstraint(
            "job_id",
            "unit_fingerprint",
            name="uq_graph_extraction_units_job_fingerprint",
        ),
        Index(
            "ix_graph_extraction_units_claimable",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_graph_extraction_units_job_status_ordinal",
            "job_id",
            "status",
            "ordinal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_extraction_units_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_extraction_units_library",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_units_revision",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    center_chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "chunks.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_units_center_chunk",
        ),
        nullable=False,
    )
    center_evidence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "evidence_units.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_units_center_evidence",
        ),
        nullable=False,
    )
    unit_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    model_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    claim_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
