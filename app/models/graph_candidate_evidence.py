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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _candidate_evidence_constraints(prefix: str) -> tuple[Any, ...]:
    nullable_resolution = (
        "resolved_evidence_id IS NULL AND resolved_document_id IS NULL "
        "AND resolved_document_revision_id IS NULL AND resolved_chunk_id IS NULL "
        "AND resolved_block_id IS NULL AND resolved_source_span IS NULL "
        "AND evidence_type IS NULL AND evidence_quality_score IS NULL"
    )
    return (
        CheckConstraint(
            "validation_status IN ('valid','ambiguous','invalid')",
            name=f"ck_{prefix}_status",
        ),
        CheckConstraint(
            "evidence_type IS NULL OR evidence_type IN "
            "('direct_statement','table_cell')",
            name=f"ck_{prefix}_type",
        ),
        CheckConstraint(
            "evidence_quality_score IS NULL OR evidence_quality_score BETWEEN 0 AND 1",
            name=f"ck_{prefix}_score",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND quote_text IS NOT NULL AND "
            "((validation_status = 'valid' "
            "AND jsonb_array_length(candidate_matches) = 1 "
            "AND resolved_evidence_id IS NOT NULL AND resolved_document_id IS NOT NULL "
            "AND resolved_document_revision_id IS NOT NULL AND resolved_chunk_id IS NOT NULL "
            "AND resolved_source_span IS NOT NULL AND evidence_type IS NOT NULL "
            "AND evidence_quality_score IS NOT NULL "
            "AND candidate_matches -> 0 ->> 'evidence_id' = resolved_evidence_id::text "
            "AND candidate_matches -> 0 ->> 'document_id' = resolved_document_id::text "
            "AND candidate_matches -> 0 ->> 'revision_id' = "
            "resolved_document_revision_id::text "
            "AND candidate_matches -> 0 ->> 'chunk_id' = resolved_chunk_id::text "
            "AND (candidate_matches -> 0 ->> 'block_id') IS NOT DISTINCT FROM "
            "resolved_block_id::text "
            "AND candidate_matches -> 0 -> 'source_span' = resolved_source_span) OR "
            "(validation_status = 'ambiguous' "
            "AND jsonb_array_length(candidate_matches) >= 2 AND "
            f"{nullable_resolution}) OR "
            "(validation_status = 'invalid' "
            "AND candidate_matches = '[]'::jsonb AND "
            f"{nullable_resolution}))) OR "
            "(purged_at IS NOT NULL AND quote_text IS NULL "
            "AND resolved_source_span IS NULL AND validation_error IS NULL "
            "AND candidate_matches = '[]'::jsonb AND "
            "((validation_status = 'valid' "
            "AND resolved_evidence_id IS NOT NULL AND resolved_document_id IS NOT NULL "
            "AND resolved_document_revision_id IS NOT NULL AND resolved_chunk_id IS NOT NULL "
            "AND evidence_type IS NOT NULL AND evidence_quality_score IS NOT NULL) OR "
            "(validation_status IN ('ambiguous','invalid') AND "
            f"{nullable_resolution})))",
            name=f"ck_{prefix}_resolution",
        ),
        UniqueConstraint(
            "candidate_id",
            "extraction_unit_id",
            "claim_key",
            name=f"uq_{prefix}_claim",
        ),
        Index(f"ix_{prefix}_job_status", "job_id", "validation_status"),
        Index(f"ix_{prefix}_candidate", "candidate_id"),
        Index(f"ix_{prefix}_unit", "extraction_unit_id"),
        Index(f"ix_{prefix}_purge", "purged_at", "created_at"),
    )


class _CandidateEvidenceBase(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID]
    extraction_unit_id: Mapped[uuid.UUID]
    claim_key: Mapped[str] = mapped_column(String(64), nullable=False)
    context_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_evidence_id: Mapped[Optional[uuid.UUID]]
    resolved_document_id: Mapped[Optional[uuid.UUID]]
    resolved_document_revision_id: Mapped[Optional[uuid.UUID]]
    resolved_chunk_id: Mapped[Optional[uuid.UUID]]
    resolved_block_id: Mapped[Optional[uuid.UUID]]
    resolved_source_span: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    candidate_matches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    evidence_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    evidence_quality_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GraphEntityCandidateEvidence(_CandidateEvidenceBase):
    __tablename__ = "graph_entity_candidate_evidence"
    __table_args__ = _candidate_evidence_constraints(
        "graph_entity_candidate_evidence"
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_entity_candidate_evidence_job",
        ),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_graph_entity_candidate_evidence_unit",
        ),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_entity_candidate_evidence_candidate",
        ),
        nullable=False,
    )
    resolved_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "evidence_units.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidate_evidence_evidence",
        ),
        nullable=True,
    )
    resolved_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidate_evidence_document",
        ),
        nullable=True,
    )
    resolved_document_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidate_evidence_revision",
        ),
        nullable=True,
    )
    resolved_chunk_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "chunks.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidate_evidence_chunk",
        ),
        nullable=True,
    )
    resolved_block_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_blocks.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidate_evidence_block",
        ),
        nullable=True,
    )


class GraphRelationCandidateEvidence(_CandidateEvidenceBase):
    __tablename__ = "graph_relation_candidate_evidence"
    __table_args__ = _candidate_evidence_constraints(
        "graph_relation_candidate_evidence"
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidate_evidence_job",
        ),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidate_evidence_unit",
        ),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_relation_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidate_evidence_candidate",
        ),
        nullable=False,
    )
    resolved_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "evidence_units.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidate_evidence_evidence",
        ),
        nullable=True,
    )
    resolved_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidate_evidence_document",
        ),
        nullable=True,
    )
    resolved_document_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidate_evidence_revision",
        ),
        nullable=True,
    )
    resolved_chunk_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "chunks.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidate_evidence_chunk",
        ),
        nullable=True,
    )
    resolved_block_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_blocks.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidate_evidence_block",
        ),
        nullable=True,
    )
