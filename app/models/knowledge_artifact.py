from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class KnowledgeArtifact(Base):
    __tablename__ = "knowledge_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('summary','outline')",
            name="ck_knowledge_artifacts_type",
        ),
        CheckConstraint(
            "((artifact_type = 'summary' AND contract_version = 'summary-v1') OR "
            "(artifact_type = 'outline' AND contract_version = 'outline-v1'))",
            name="ck_knowledge_artifacts_contract",
        ),
        CheckConstraint(
            "generation_mode IN ('deterministic','model')",
            name="ck_knowledge_artifacts_generation_mode",
        ),
        CheckConstraint(
            "((generation_mode = 'deterministic' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL) OR "
            "(generation_mode = 'model' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL))",
            name="ck_knowledge_artifacts_model_identity",
        ),
        CheckConstraint(
            "lifecycle_state IN ('current','stale','deleted')",
            name="ck_knowledge_artifacts_lifecycle",
        ),
        CheckConstraint(
            "((lifecycle_state = 'current' AND stale_at IS NULL AND deleted_at IS NULL) OR "
            "(lifecycle_state = 'stale' AND stale_at IS NOT NULL AND deleted_at IS NULL) OR "
            "(lifecycle_state = 'deleted' AND deleted_at IS NOT NULL))",
            name="ck_knowledge_artifacts_lifecycle_timestamps",
        ),
        UniqueConstraint("job_id", name="uq_knowledge_artifacts_job"),
        Index(
            "uq_knowledge_artifacts_current_revision_type",
            "document_revision_id",
            "artifact_type",
            unique=True,
            postgresql_where=text("lifecycle_state = 'current'"),
        ),
        Index(
            "ix_knowledge_artifacts_library_type_lifecycle",
            "library_id",
            "artifact_type",
            "lifecycle_state",
        ),
        Index(
            "ix_knowledge_artifacts_document_type_created",
            "document_id",
            "artifact_type",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_artifact_jobs.id",
            ondelete="CASCADE",
            name="fk_knowledge_artifacts_job",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_knowledge_artifacts_library",
        ),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_knowledge_artifacts_document",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_knowledge_artifacts_revision",
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
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="current", server_default="current"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stale_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
