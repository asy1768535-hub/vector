from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


_CANDIDATE_STATUSES = (
    "'extracted','aggregated','validated','pending_review','rejected',"
    "'materialized','superseded'"
)


class GraphEntityCandidate(Base):
    __tablename__ = "graph_entity_candidates"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_CANDIDATE_STATUSES})",
            name="ck_graph_entity_candidates_status",
        ),
        CheckConstraint(
            "(model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)) AND "
            "(evidence_quality_score IS NULL OR "
            "(evidence_quality_score >= 0 AND evidence_quality_score <= 1)) AND "
            "(schema_validation_score IS NULL OR "
            "(schema_validation_score >= 0 AND schema_validation_score <= 1)) AND "
            "(final_confidence IS NULL OR "
            "(final_confidence >= 0 AND final_confidence <= 1))",
            name="ck_graph_entity_candidates_confidence",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND canonical_name IS NOT NULL "
            "AND normalized_name IS NOT NULL AND proposed_aliases IS NOT NULL "
            "AND proposed_properties IS NOT NULL AND external_mapping_hints IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND canonical_name IS NULL "
            "AND normalized_name IS NULL AND proposed_aliases IS NULL "
            "AND proposed_properties IS NULL AND external_mapping_hints IS NULL "
            "AND review_reason IS NULL AND validation_errors IS NULL)",
            name="ck_graph_entity_candidates_payload_or_purged",
        ),
        UniqueConstraint(
            "job_id", "candidate_key", name="uq_graph_entity_candidates_job_key"
        ),
        Index(
            "ix_graph_entity_candidates_job_status_key",
            "job_id",
            "status",
            "candidate_key",
        ),
        Index("ix_graph_entity_candidates_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_entity_candidates_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_entity_candidates_library",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_entity_candidates_ontology",
        ),
        nullable=False,
    )
    entity_type_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    normalized_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    proposed_aliases: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    proposed_properties: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    external_mapping_hints: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    candidate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    matched_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="SET NULL",
            name="fk_graph_entity_candidates_matched_entity",
        ),
        nullable=True,
    )
    materialized_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="SET NULL",
            name="fk_graph_entity_candidates_materialized_entity",
        ),
        nullable=True,
    )
    normalization_method: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    model_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evidence_quality_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    schema_validation_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    final_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="extracted", server_default="extracted"
    )
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validation_errors: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, nullable=True
    )
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


class GraphRelationCandidate(Base):
    __tablename__ = "graph_relation_candidates"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_CANDIDATE_STATUSES})",
            name="ck_graph_relation_candidates_status",
        ),
        CheckConstraint(
            "evidence_support_mode IN ('single_evidence','evidence_group')",
            name="ck_graph_relation_candidates_support_mode",
        ),
        CheckConstraint(
            "ontology_validation_status IS NULL OR ontology_validation_status IN "
            "('valid','warning','invalid','boundary_unclear')",
            name="ck_graph_relation_candidates_ontology_status",
        ),
        CheckConstraint(
            "(model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)) AND "
            "(evidence_quality_score IS NULL OR "
            "(evidence_quality_score >= 0 AND evidence_quality_score <= 1)) AND "
            "(schema_validation_score IS NULL OR "
            "(schema_validation_score >= 0 AND schema_validation_score <= 1)) AND "
            "(normalization_score IS NULL OR "
            "(normalization_score >= 0 AND normalization_score <= 1)) AND "
            "(final_confidence IS NULL OR "
            "(final_confidence >= 0 AND final_confidence <= 1))",
            name="ck_graph_relation_candidates_confidence",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND proposed_properties IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND proposed_properties IS NULL "
            "AND review_reason IS NULL AND validation_errors IS NULL)",
            name="ck_graph_relation_candidates_payload_or_purged",
        ),
        UniqueConstraint(
            "job_id", "candidate_key", name="uq_graph_relation_candidates_job_key"
        ),
        Index(
            "ix_graph_relation_candidates_job_status_key",
            "job_id",
            "status",
            "candidate_key",
        ),
        Index("ix_graph_relation_candidates_source", "source_candidate_id"),
        Index("ix_graph_relation_candidates_target", "target_candidate_id"),
        Index("ix_graph_relation_candidates_purge", "purged_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidates_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidates_library",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_relation_candidates_ontology",
        ),
        nullable=False,
    )
    source_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidates_source_candidate",
        ),
        nullable=False,
    )
    relation_type_key: Mapped[str] = mapped_column(String(128), nullable=False)
    target_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="CASCADE",
            name="fk_graph_relation_candidates_target_candidate",
        ),
        nullable=False,
    )
    proposed_properties: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    candidate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    matched_relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_relations.id",
            ondelete="SET NULL",
            name="fk_graph_relation_candidates_matched_relation",
        ),
        nullable=True,
    )
    materialized_relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_relations.id",
            ondelete="SET NULL",
            name="fk_graph_relation_candidates_materialized_relation",
        ),
        nullable=True,
    )
    evidence_support_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    model_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evidence_quality_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    schema_validation_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    normalization_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    final_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ontology_validation_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    has_conflict: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="extracted", server_default="extracted"
    )
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validation_errors: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, nullable=True
    )
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
