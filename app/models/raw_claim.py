from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphRawClaim(Base):
    """Immutable, evidence-backed claim content independent of canonical state."""

    __tablename__ = "graph_raw_claims"
    __table_args__ = (
        CheckConstraint(
            "claim_schema_version = 'raw_claim_v1'",
            name="ck_graph_raw_claims_schema_version",
        ),
        CheckConstraint(
            "surface_direction IN ('source_to_target','target_to_source','undirected','unknown')",
            name="ck_graph_raw_claims_direction",
        ),
        CheckConstraint(
            "content_scoped_claim_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_raw_claims_content_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(source_mention) = 'object' AND "
            "jsonb_typeof(target_mention) = 'object' AND "
            "jsonb_typeof(negation) = 'object' AND "
            "jsonb_typeof(modality) = 'object' AND "
            "jsonb_typeof(qualifiers) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_graph_raw_claims_json_shapes",
        ),
        UniqueConstraint(
            "library_id",
            "document_revision_id",
            "content_scoped_claim_fingerprint",
            name="uq_graph_raw_claims_scope_content_fingerprint",
        ),
        Index(
            "ix_graph_raw_claims_library_revision",
            "library_id",
            "document_revision_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    claim_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="raw_claim_v1")
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_graph_raw_claims_library"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT", name="fk_graph_raw_claims_document"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_raw_claims_revision",
        ),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_scoped_claim_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_mention: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    target_mention: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    surface_direction: Mapped[str] = mapped_column(String(32), nullable=False)
    negation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    modality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    qualifiers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    valid_time: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    effective_time: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GraphRawClaimOccurrence(Base):
    """Immutable provenance for one extraction occurrence of a raw claim."""

    __tablename__ = "graph_raw_claim_occurrences"
    __table_args__ = (
        CheckConstraint(
            "extraction_occurrence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_raw_claim_occurrences_fingerprint",
        ),
        CheckConstraint(
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "prompt_content_hash ~ '^[0-9a-f]{64}$' AND "
            "(ontology_snapshot_hash IS NULL OR ontology_snapshot_hash ~ '^[0-9a-f]{64}$')",
            name="ck_graph_raw_claim_occurrences_hashes",
        ),
        UniqueConstraint(
            "extraction_occurrence_id",
            name="uq_graph_raw_claim_occurrences_occurrence_id",
        ),
        UniqueConstraint(
            "extraction_occurrence_fingerprint",
            name="uq_graph_raw_claim_occurrences_fingerprint",
        ),
        Index(
            "ix_graph_raw_claim_occurrences_job_unit",
            "job_id",
            "extraction_unit_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extraction_occurrence_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="RESTRICT", name="fk_graph_raw_claim_occurrences_claim"),
        nullable=False,
    )
    extraction_occurrence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_extraction_jobs.id", ondelete="RESTRICT", name="fk_graph_raw_claim_occurrences_job"),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="RESTRICT",
            name="fk_graph_raw_claim_occurrences_unit",
        ),
        nullable=False,
    )
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
