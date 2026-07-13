from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExtractionContextSnapshot(Base):
    __tablename__ = "extraction_context_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "extraction_unit_id",
            name="uq_extraction_context_snapshots_unit",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND context_json IS NOT NULL AND context_text IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND context_json IS NULL AND context_text IS NULL "
            "AND document_metadata IS NULL AND chunk_title_path IS NULL "
            "AND block_title_path IS NULL AND effective_title_path IS NULL)",
            name="ck_extraction_context_snapshots_payload_or_purged",
        ),
        Index("ix_extraction_context_snapshots_job", "job_id"),
        Index("ix_extraction_context_snapshots_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_extraction_context_snapshots_job",
        ),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_extraction_context_snapshots_unit",
        ),
        nullable=False,
    )
    center_chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "chunks.id",
            ondelete="RESTRICT",
            name="fk_extraction_context_snapshots_chunk",
        ),
        nullable=False,
    )
    center_evidence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "evidence_units.id",
            ondelete="RESTRICT",
            name="fk_extraction_context_snapshots_evidence",
        ),
        nullable=False,
    )

    previous_chunk_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    next_chunk_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    block_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    title_path_source: Mapped[str] = mapped_column(String(32), nullable=False)
    ontology_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    context_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    context_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    context_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    chunk_title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    block_title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    effective_title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
