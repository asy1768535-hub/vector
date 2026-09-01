from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ENTITY_RESOLUTION_LINK_EXISTING = "link_existing"
ENTITY_RESOLUTION_CREATE_NEW = "create_new"
ENTITY_RESOLUTION_PENDING_REVIEW = "pending_review"
ENTITY_RESOLUTION_REJECTED = "rejected"

ENTITY_RESOLUTION_STATUS_ACTIVE = "active"
ENTITY_RESOLUTION_STATUS_SUPERSEDED = "superseded"


class EntityResolutionDecision(Base):
    __tablename__ = "entity_resolution_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision_kind IN ('link_existing','create_new','pending_review','rejected')",
            name="ck_entity_resolution_decisions_kind",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active','superseded')",
            name="ck_entity_resolution_decisions_lifecycle",
        ),
        CheckConstraint(
            "(decision_kind IN ('link_existing','create_new') AND canonical_entity_id IS NOT NULL) "
            "OR (decision_kind IN ('pending_review','rejected') AND canonical_entity_id IS NULL)",
            name="ck_entity_resolution_decisions_target",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_entity_resolution_decisions_confidence",
        ),
        CheckConstraint(
            "subject_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_entity_resolution_decisions_fingerprints",
        ),
        CheckConstraint(
            "decision_kind IN ('link_existing','create_new') OR reason_code IS NOT NULL",
            name="ck_entity_resolution_decisions_reason",
        ),
        CheckConstraint(
            "identifier_snapshot IS NULL OR jsonb_typeof(identifier_snapshot) = 'object'",
            name="ck_entity_resolution_decisions_identifier_json",
        ),
        CheckConstraint(
            "jsonb_typeof(candidate_snapshot) = 'array' AND jsonb_typeof(evidence_refs) = 'array'",
            name="ck_entity_resolution_decisions_snapshot_json",
        ),
        Index(
            "uq_entity_resolution_decisions_library_fingerprint_active",
            "library_id",
            "decision_fingerprint",
            unique=True,
            postgresql_where=text("lifecycle_status = 'active'"),
        ),
        ForeignKeyConstraint(
            ["canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_entity_resolution_decisions_canonical_entity",
        ),
        Index("ix_entity_resolution_decisions_library_canonical", "library_id", "canonical_entity_id"),
        Index("ix_entity_resolution_decisions_library_subject", "library_id", "subject_fingerprint"),
        Index(
            "uq_entity_resolution_decisions_library_subject_active",
            "library_id",
            "subject_fingerprint",
            unique=True,
            postgresql_where=text("lifecycle_status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subject_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_entity_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_entity_candidates.id",
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_candidate",
        ),
        nullable=True,
    )
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_entity",
        ),
        nullable=True,
    )
    canonical_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    observed_name: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_type_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    identifier_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    candidate_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    decision_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ENTITY_RESOLUTION_STATUS_ACTIVE, server_default="active"
    )
    method: Mapped[str] = mapped_column(String(64), nullable=False, default="none", server_default="none")
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resolver_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="entity_resolution_v1", server_default="entity_resolution_v1"
    )
    supersedes_decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entity_resolution_decisions.id",
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_supersedes",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
