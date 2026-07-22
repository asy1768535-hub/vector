from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


CLASSIFICATION_RUN_STATUSES = (
    "pending_review",
    "auto_applied",
    "blocked_manual",
    "manual_applied",
    "rejected",
    "failed",
    "stale",
)
CLASSIFICATION_PROPOSAL_STATUSES = (
    "auto_selected",
    "not_selected",
    "pending_review",
    "blocked_manual",
    "accepted",
    "rejected",
    "invalid",
    "stale",
)


class DocumentClassificationRun(Base):
    __tablename__ = "document_classification_runs"
    __table_args__ = (
        CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND "
            "enabled_label_set_hash ~ '^[0-9a-f]{64}$' AND "
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_document_classification_runs_hashes",
        ),
        CheckConstraint(
            "btrim(policy_version) <> '' AND btrim(classifier_version) <> '' AND "
            "btrim(model_provider) <> '' AND btrim(model_name) <> '' AND "
            "btrim(prompt_version) <> ''",
            name="ck_document_classification_runs_identities",
        ),
        CheckConstraint(
            "min_confidence_micros BETWEEN 0 AND 1000000 AND "
            "min_margin_micros BETWEEN 0 AND 1000000 AND "
            "max_secondary_labels BETWEEN 0 AND 8",
            name="ck_document_classification_runs_policy",
        ),
        CheckConstraint(
            "generation_no > 0 AND retry_generation >= 0",
            name="ck_document_classification_runs_generation",
        ),
        CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_document_classification_runs_trigger",
        ),
        CheckConstraint(
            "status IN ('pending_review','auto_applied','blocked_manual',"
            "'manual_applied','rejected','failed','stale')",
            name="ck_document_classification_runs_status",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) <= 16",
            name="ck_document_classification_runs_reason_codes",
        ),
        CheckConstraint(
            "((status = 'failed' AND error_code IS NOT NULL) OR "
            "(status <> 'failed' AND error_code IS NULL))",
            name="ck_document_classification_runs_error_shape",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_document_classification_runs_idempotency_key",
        ),
        UniqueConstraint(
            "document_revision_id",
            "generation_no",
            name="uq_document_classification_runs_revision_generation",
        ),
        Index(
            "ix_document_classification_runs_library_status_created",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_document_classification_runs_revision_created",
            "document_revision_id",
            "created_at",
        ),
        Index(
            "ix_document_classification_runs_taxonomy_status",
            "taxonomy_version_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE", name="fk_document_classification_runs_library"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT", name="fk_document_classification_runs_document"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="RESTRICT", name="fk_document_classification_runs_revision"),
        nullable=False,
    )
    revision_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classification_taxonomies.id", ondelete="RESTRICT", name="fk_document_classification_runs_taxonomy"),
        nullable=False,
    )
    enabled_label_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    min_confidence_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    min_margin_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    max_secondary_labels: Mapped[int] = mapped_column(Integer, nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_document_classification_runs_requested_by"),
        nullable=True,
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentClassificationProposal(Base):
    __tablename__ = "document_classification_proposals"
    __table_args__ = (
        CheckConstraint(
            "((label_id IS NOT NULL AND proposed_key IS NULL AND proposed_label IS NULL) OR "
            "(label_id IS NULL AND proposed_key IS NOT NULL AND proposed_label IS NOT NULL))",
            name="ck_document_classification_proposals_target_shape",
        ),
        CheckConstraint(
            "proposed_key IS NULL OR proposed_key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_document_classification_proposals_key",
        ),
        CheckConstraint("proposed_label IS NULL OR btrim(proposed_label) <> ''", name="ck_document_classification_proposals_label"),
        CheckConstraint("role IN ('primary','secondary')", name="ck_document_classification_proposals_role"),
        CheckConstraint("rank BETWEEN 0 AND 49 AND confidence_micros BETWEEN 0 AND 1000000", name="ck_document_classification_proposals_score"),
        CheckConstraint(
            "status IN ('auto_selected','not_selected','pending_review','blocked_manual',"
            "'accepted','rejected','invalid','stale')",
            name="ck_document_classification_proposals_status",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) <= 16",
            name="ck_document_classification_proposals_reason_codes",
        ),
        CheckConstraint(
            "((status IN ('accepted','rejected') AND reviewed_at IS NOT NULL) OR "
            "(status NOT IN ('accepted','rejected') AND reviewed_at IS NULL AND reviewed_by_user_id IS NULL))",
            name="ck_document_classification_proposals_review_shape",
        ),
        UniqueConstraint("run_id", "role", "rank", name="uq_document_classification_proposals_run_role_rank"),
        Index("ix_document_classification_proposals_run_status", "run_id", "status", "role", "rank"),
        Index("ix_document_classification_proposals_label", "label_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_classification_runs.id", ondelete="CASCADE", name="fk_document_classification_proposals_run"),
        nullable=False,
    )
    label_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classification_labels.id", ondelete="RESTRICT", name="fk_document_classification_proposals_label"),
        nullable=True,
    )
    proposed_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    proposed_label: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_document_classification_proposals_reviewed_by"),
        nullable=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class DocumentClassificationDecisionSet(Base):
    __tablename__ = "document_classification_decision_sets"
    __table_args__ = (
        CheckConstraint("source IN ('model','manual')", name="ck_document_classification_decision_sets_source"),
        CheckConstraint("lifecycle IN ('effective','superseded','removed')", name="ck_document_classification_decision_sets_lifecycle"),
        CheckConstraint("generation_no > 0", name="ck_document_classification_decision_sets_generation"),
        CheckConstraint(
            "((lifecycle = 'effective' AND superseded_at IS NULL AND removed_at IS NULL) OR "
            "(lifecycle = 'superseded' AND superseded_at IS NOT NULL AND removed_at IS NULL) OR "
            "(lifecycle = 'removed' AND removed_at IS NOT NULL AND superseded_at IS NULL))",
            name="ck_document_classification_decision_sets_lifecycle_shape",
        ),
        CheckConstraint("supersedes_decision_set_id IS NULL OR supersedes_decision_set_id <> id", name="ck_document_classification_decision_sets_supersedes_not_self"),
        CheckConstraint(
            "((source = 'model' AND source_run_id IS NOT NULL AND policy_version IS NOT NULL "
            "AND classifier_version IS NOT NULL AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL "
            "AND prompt_version IS NOT NULL AND input_fingerprint IS NOT NULL "
            "AND reviewed_at IS NULL AND reviewed_by_user_id IS NULL) OR "
            "(source = 'manual' AND policy_version IS NULL AND classifier_version IS NULL "
            "AND model_provider IS NULL AND model_name IS NULL AND model_config_hash IS NULL "
            "AND prompt_version IS NULL AND input_fingerprint IS NULL AND reviewed_at IS NOT NULL))",
            name="ck_document_classification_decision_sets_source_shape",
        ),
        CheckConstraint("lifecycle <> 'removed' OR source = 'manual'", name="ck_document_classification_decision_sets_removed_manual"),
        UniqueConstraint("document_revision_id", "generation_no", name="uq_document_classification_decision_sets_revision_generation"),
        Index("uq_document_classification_decision_sets_effective_revision", "document_revision_id", unique=True, postgresql_where=text("lifecycle = 'effective'")),
        Index("ix_doc_class_decision_sets_library_lifecycle", "library_id", "lifecycle", "created_at"),
        Index("ix_document_classification_decision_sets_document_revision", "document_id", "document_revision_id", "generation_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE", name="fk_document_classification_decision_sets_library"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT", name="fk_document_classification_decision_sets_document"), nullable=False)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("document_revisions.id", ondelete="RESTRICT", name="fk_document_classification_decision_sets_revision"), nullable=False)
    taxonomy_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("classification_taxonomies.id", ondelete="RESTRICT", name="fk_document_classification_decision_sets_taxonomy"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), ForeignKey("document_classification_runs.id", ondelete="RESTRICT", name="fk_document_classification_decision_sets_source_run"), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    classifier_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model_config_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    supersedes_decision_set_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), ForeignKey("document_classification_decision_sets.id", ondelete="RESTRICT", name="fk_document_classification_decision_sets_supersedes"), nullable=True)
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_document_classification_decision_sets_reviewed_by"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentClassificationDecision(Base):
    __tablename__ = "document_classification_decisions"
    __table_args__ = (
        CheckConstraint(
            "((role = 'primary' AND ordinal = 0) OR "
            "(role = 'secondary' AND ordinal BETWEEN 1 AND 8))",
            name="ck_document_classification_decisions_role_ordinal",
        ),
        CheckConstraint("confidence_micros IS NULL OR confidence_micros BETWEEN 0 AND 1000000", name="ck_document_classification_decisions_confidence"),
        UniqueConstraint("decision_set_id", "ordinal", name="uq_document_classification_decisions_set_ordinal"),
        UniqueConstraint("decision_set_id", "label_id", name="uq_document_classification_decisions_set_label"),
        Index("ix_document_classification_decisions_label", "label_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_set_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("document_classification_decision_sets.id", ondelete="CASCADE", name="fk_document_classification_decisions_set"), nullable=False)
    label_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("classification_labels.id", ondelete="RESTRICT", name="fk_document_classification_decisions_label"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_micros: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
