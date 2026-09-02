from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphRawClaimProjectionBinding(Base):
    """Append-only, ontology-specific anchor from one RawClaim occurrence to a candidate."""

    __tablename__ = "graph_raw_claim_projection_bindings"
    __table_args__ = (
        CheckConstraint(
            "binding_contract_version = 'raw_claim_projection_binding_v1'",
            name="ck_raw_claim_projection_bindings_contract_version",
        ),
        CheckConstraint(
            "binding_method = 'explicit_shadow_projection_ref_v1'",
            name="ck_raw_claim_projection_bindings_method",
        ),
        CheckConstraint(
            "binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_raw_claim_projection_bindings_fingerprint",
        ),
        UniqueConstraint(
            "raw_claim_occurrence_id",
            "graph_relation_candidate_id",
            name="uq_raw_claim_projection_bindings_occurrence_candidate",
        ),
        UniqueConstraint(
            "binding_fingerprint",
            name="uq_raw_claim_projection_bindings_fingerprint",
        ),
        Index(
            "ix_raw_claim_projection_bindings_claim_occurrence",
            "raw_claim_id",
            "raw_claim_occurrence_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    raw_claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_raw_claims.id",
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_claim",
        ),
        nullable=False,
    )
    raw_claim_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_raw_claim_occurrences.extraction_occurrence_id",
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_occurrence",
        ),
        nullable=False,
    )
    graph_relation_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_relation_candidates.id",
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_candidate",
        ),
        nullable=False,
    )
    binding_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_method: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
