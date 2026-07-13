from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphEntityMergeCandidate(Base):
    __tablename__ = "graph_entity_merge_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review','rejected','superseded')",
            name="ck_graph_entity_merge_candidates_status",
        ),
        CheckConstraint(
            "purged_at IS NULL OR "
            "(details IS NULL AND description IS NULL AND evidence IS NULL)",
            name="ck_graph_entity_merge_candidates_payload_or_purged",
        ),
        UniqueConstraint(
            "job_id",
            "merge_key",
            name="uq_graph_entity_merge_candidates_job_key",
        ),
        Index(
            "ix_graph_entity_merge_candidates_job_status", "job_id", "status"
        ),
        Index(
            "ix_graph_entity_merge_candidates_purge", "purged_at", "created_at"
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
            name="fk_graph_entity_merge_candidates_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_entity_merge_candidates_library",
        ),
        nullable=False,
    )
    entity_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_entity_merge_candidates_candidate",
        ),
        nullable=False,
    )
    suggested_target_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="SET NULL",
            name="fk_graph_entity_merge_candidates_target_entity",
        ),
        nullable=True,
    )
    merge_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending_review",
        server_default="pending_review",
    )
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GraphExtractionConflict(Base):
    __tablename__ = "graph_extraction_conflicts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','superseded')",
            name="ck_graph_extraction_conflicts_status",
        ),
        CheckConstraint(
            "jsonb_array_length(entity_candidate_ids) > 0 OR "
            "jsonb_array_length(relation_candidate_ids) > 0",
            name="ck_graph_extraction_conflicts_members",
        ),
        CheckConstraint(
            "purged_at IS NULL OR "
            "(details IS NULL AND description IS NULL AND evidence IS NULL)",
            name="ck_graph_extraction_conflicts_payload_or_purged",
        ),
        UniqueConstraint(
            "job_id", "conflict_key", name="uq_graph_extraction_conflicts_job_key"
        ),
        Index("ix_graph_extraction_conflicts_job_status", "job_id", "status"),
        Index("ix_graph_extraction_conflicts_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_extraction_conflicts_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_extraction_conflicts_library",
        ),
        nullable=False,
    )
    conflict_key: Mapped[str] = mapped_column(String(64), nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_candidate_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    relation_candidate_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    conflicting_fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
