from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphClaimDecision(Base):
    """Immutable pending decision event attached to one raw claim."""

    __tablename__ = "graph_claim_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision_schema_version = 'claim_decision_projection_v1'",
            name="ck_graph_claim_decisions_schema_version",
        ),
        CheckConstraint(
            "decision_kind IN ('mapping_candidate','schema_extension_candidate')",
            name="ck_graph_claim_decisions_kind",
        ),
        CheckConstraint(
            "status = 'pending'",
            name="ck_graph_claim_decisions_status",
        ),
        CheckConstraint(
            "decision_version >= 1 AND revision_no >= 1",
            name="ck_graph_claim_decisions_positive_versions",
        ),
        CheckConstraint(
            "created_by_kind IN ('system','human','external')",
            name="ck_graph_claim_decisions_producer_kind",
        ),
        CheckConstraint(
            "btrim(producer_key) <> ''",
            name="ck_graph_claim_decisions_producer_key",
        ),
        CheckConstraint(
            "((decision_kind = 'mapping_candidate' AND reason_code IN "
            "('unknown_predicate','unknown_direction','ambiguous_mapping')) OR "
            "(decision_kind = 'schema_extension_candidate' AND reason_code IN "
            "('unknown_source_type','unknown_target_type'))) ",
            name="ck_graph_claim_decisions_reason_kind",
        ),
        CheckConstraint(
            "reason_code <> 'unknown_direction' OR proposal->>'surface_direction' = 'unknown'",
            name="ck_graph_claim_decisions_unknown_direction",
        ),
        CheckConstraint(
            "decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_claim_decisions_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(proposal) = 'object'",
            name="ck_graph_claim_decisions_proposal_shape",
        ),
        CheckConstraint(
            "pg_column_size(proposal) <= 8192",
            name="ck_graph_claim_decisions_proposal_size",
        ),
        UniqueConstraint(
            "library_id",
            "document_revision_id",
            "decision_fingerprint",
            name="uq_graph_claim_decisions_scope_fingerprint",
        ),
        Index(
            "ix_graph_claim_decisions_scope_claim",
            "library_id",
            "document_revision_id",
            "claim_id",
            "created_at",
        ),
        Index(
            "ix_graph_claim_decisions_kind_status",
            "decision_kind",
            "status",
            "created_at",
        ),
        Index(
            "ix_graph_claim_decisions_occurrence",
            "extraction_occurrence_id",
            "created_at",
        ),
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True
    )
    decision_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_graph_claim_decisions_library"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT", name="fk_graph_claim_decisions_document"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_claim_decisions_revision",
        ),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="RESTRICT", name="fk_graph_claim_decisions_claim"),
        nullable=False,
    )
    extraction_occurrence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_raw_claim_occurrences.extraction_occurrence_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_decisions_occurrence",
        ),
        nullable=True,
    )
    decision_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    producer_key: Mapped[str] = mapped_column(String(128), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
