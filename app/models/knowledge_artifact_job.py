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


class KnowledgeArtifactJob(Base):
    __tablename__ = "knowledge_artifact_jobs"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('summary','outline')",
            name="ck_knowledge_artifact_jobs_type",
        ),
        CheckConstraint(
            "((artifact_type = 'summary' AND contract_version = 'summary-v1') OR "
            "(artifact_type = 'outline' AND contract_version = 'outline-v1'))",
            name="ck_knowledge_artifact_jobs_contract",
        ),
        CheckConstraint(
            "generation_mode IN ('deterministic','model')",
            name="ck_knowledge_artifact_jobs_generation_mode",
        ),
        CheckConstraint(
            "((generation_mode = 'deterministic' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL) OR "
            "(generation_mode = 'model' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL))",
            name="ck_knowledge_artifact_jobs_model_identity",
        ),
        CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_knowledge_artifact_jobs_trigger",
        ),
        CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_knowledge_artifact_jobs_status",
        ),
        CheckConstraint(
            "retry_generation >= 0",
            name="ck_knowledge_artifact_jobs_retry_generation",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_knowledge_artifact_jobs_idempotency_key",
        ),
        Index(
            "ix_knowledge_artifact_jobs_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_knowledge_artifact_jobs_library_status_created",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_knowledge_artifact_jobs_revision_type_created",
            "document_revision_id",
            "artifact_type",
            "created_at",
        ),
        Index("ix_knowledge_artifact_jobs_rerun", "rerun_of_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_knowledge_artifact_jobs_library",
        ),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_knowledge_artifact_jobs_document",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_knowledge_artifact_jobs_revision",
        ),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    model_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model_config_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    rerun_of_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_artifact_jobs.id",
            ondelete="SET NULL",
            name="fk_knowledge_artifact_jobs_rerun",
        ),
        nullable=True,
    )
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_knowledge_artifact_jobs_requested_by",
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
