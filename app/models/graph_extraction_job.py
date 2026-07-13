from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphExtractionJob(Base):
    __tablename__ = "graph_extraction_jobs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual','revision_published','full_rerun','repair','eval')",
            name="ck_graph_extraction_jobs_trigger_type",
        ),
        CheckConstraint(
            "execution_mode IN ('production','eval','repair')",
            name="ck_graph_extraction_jobs_execution_mode",
        ),
        CheckConstraint(
            "status IN ('queued','processing','partially_succeeded','succeeded','failed','cancelled','superseded')",
            name="ck_graph_extraction_jobs_status",
        ),
        CheckConstraint(
            "current_stage IS NULL OR current_stage IN "
            "('preparing','building_context','extracting','parsing','binding_evidence',"
            "'aggregating','validating','scoring','materializing','finalizing')",
            name="ck_graph_extraction_jobs_current_stage",
        ),
        CheckConstraint(
            "retry_generation >= 0",
            name="ck_graph_extraction_jobs_retry_generation",
        ),
        CheckConstraint(
            "(trigger_type = 'full_rerun' AND rerun_of_job_id IS NOT NULL) OR "
            "trigger_type = 'repair' OR "
            "(trigger_type IN ('manual','revision_published','eval') "
            "AND rerun_of_job_id IS NULL)",
            name="ck_graph_extraction_jobs_rerun_scope",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_graph_extraction_jobs_idempotency_key",
        ),
        Index("ix_graph_extraction_jobs_status_created", "status", "created_at"),
        Index(
            "ix_graph_extraction_jobs_library_status_created",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_graph_extraction_jobs_revision_created",
            "document_revision_id",
            "created_at",
        ),
        Index("ix_graph_extraction_jobs_rerun_of", "rerun_of_job_id"),
        Index(
            "ix_graph_extraction_jobs_sensitive_purge",
            "sensitive_payload_purged_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_graph_extraction_jobs_library",
        ),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_jobs_document",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_jobs_revision",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_extraction_jobs_ontology",
        ),
        nullable=False,
    )
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    current_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    rerun_of_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_jobs.id",
            ondelete="SET NULL",
            name="fk_graph_extraction_jobs_rerun",
        ),
        nullable=True,
    )
    retry_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    output_parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    document_parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)

    model_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ontology_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_graph_extraction_jobs_requested_by",
        ),
        nullable=True,
    )
    sensitive_payload_purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    statistics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
