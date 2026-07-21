from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphEntityOccurrence(Base):
    __tablename__ = "graph_entity_occurrences"
    __table_args__ = (
        CheckConstraint(
            "model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)",
            name="ck_graph_entity_occurrences_confidence",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND raw_payload IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND raw_payload IS NULL)",
            name="ck_graph_entity_occurrences_payload_or_purged",
        ),
        UniqueConstraint(
            "extraction_unit_id",
            "local_ref",
            name="uq_graph_entity_occurrences_unit_ref",
        ),
        Index(
            "ix_graph_entity_occurrences_job_candidate", "job_id", "entity_candidate_id"
        ),
        Index("ix_graph_entity_occurrences_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_entity_occurrences_job",
        ),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_graph_entity_occurrences_unit",
        ),
        nullable=False,
    )
    local_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_entity_occurrences_candidate",
        ),
        nullable=False,
    )
    model_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GraphRelationOccurrence(Base):
    __tablename__ = "graph_relation_occurrences"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0", name="ck_graph_relation_occurrences_ordinal"
        ),
        CheckConstraint(
            "model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)",
            name="ck_graph_relation_occurrences_confidence",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND raw_payload IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND raw_payload IS NULL)",
            name="ck_graph_relation_occurrences_payload_or_purged",
        ),
        UniqueConstraint(
            "extraction_unit_id",
            "ordinal",
            name="uq_graph_relation_occurrences_unit_ordinal",
        ),
        Index(
            "ix_graph_relation_occurrences_job_candidate",
            "job_id",
            "relation_candidate_id",
        ),
        Index("ix_graph_relation_occurrences_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_relation_occurrences_job",
        ),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_graph_relation_occurrences_unit",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_entity_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_occurrences.id",
            ondelete="CASCADE",
            name="fk_graph_relation_occurrences_source_occurrence",
        ),
        nullable=False,
    )
    target_entity_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_occurrences.id",
            ondelete="CASCADE",
            name="fk_graph_relation_occurrences_target_occurrence",
        ),
        nullable=False,
    )
    relation_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_relation_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_relation_occurrences_candidate",
        ),
        nullable=False,
    )
    model_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
