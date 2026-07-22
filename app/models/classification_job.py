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
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentClassificationJob(Base):
    __tablename__ = "document_classification_jobs"
    __table_args__ = (
        CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND "
            "enabled_label_set_hash ~ '^[0-9a-f]{64}$' AND "
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_document_classification_jobs_hashes",
        ),
        CheckConstraint(
            "btrim(classifier_version) <> '' AND btrim(model_provider) <> '' AND "
            "btrim(model_name) <> '' AND btrim(prompt_version) <> ''",
            name="ck_document_classification_jobs_identities",
        ),
        CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_document_classification_jobs_trigger",
        ),
        CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_document_classification_jobs_status",
        ),
        CheckConstraint(
            "retry_generation >= 0 AND attempt_count >= 0",
            name="ck_document_classification_jobs_attempts",
        ),
        CheckConstraint(
            "((status = 'processing' AND claim_token IS NOT NULL "
            "AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND last_heartbeat_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL "
            "AND claimed_by IS NULL AND lease_expires_at IS NULL "
            "AND last_heartbeat_at IS NULL))",
            name="ck_document_classification_jobs_claim_state",
        ),
        CheckConstraint(
            "((status = 'succeeded' AND result_run_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('cancelled','superseded') AND result_run_id IS NULL "
            "AND error_code IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('queued','processing') AND result_run_id IS NULL "
            "AND finished_at IS NULL))",
            name="ck_document_classification_jobs_result_state",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_document_classification_jobs_idempotency_key",
        ),
        UniqueConstraint(
            "result_run_id",
            name="uq_document_classification_jobs_result_run",
        ),
        Index(
            "ix_document_classification_jobs_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_doc_class_jobs_library_status_created",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_doc_class_jobs_revision_created",
            "document_revision_id",
            "created_at",
        ),
        Index(
            "ix_document_classification_jobs_claimable",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        Index("ix_document_classification_jobs_rerun", "rerun_of_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_document_classification_jobs_library",
        ),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_document_classification_jobs_document",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_document_classification_jobs_revision",
        ),
        nullable=False,
    )
    revision_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomies.id",
            ondelete="RESTRICT",
            name="fk_document_classification_jobs_taxonomy",
        ),
        nullable=False,
    )
    enabled_label_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claim_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    claimed_by: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rerun_of_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_classification_jobs.id",
            ondelete="SET NULL",
            name="fk_document_classification_jobs_rerun",
        ),
        nullable=True,
    )
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_document_classification_jobs_requested_by",
        ),
        nullable=True,
    )
    result_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_classification_runs.id",
            ondelete="RESTRICT",
            name="fk_document_classification_jobs_result_run",
        ),
        nullable=True,
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
