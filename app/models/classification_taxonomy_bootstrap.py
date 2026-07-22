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


class TaxonomyBootstrapRun(Base):
    __tablename__ = "classification_taxonomy_bootstrap_runs"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('builtin_template','admin_import','llm_proposal')",
            name="ck_taxonomy_bootstrap_runs_source",
        ),
        CheckConstraint(
            "status IN ('processing','succeeded','failed')",
            name="ck_taxonomy_bootstrap_runs_status",
        ),
        CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "idempotency_key ~ '^[0-9a-f]{64}$' AND "
            "(model_config_hash IS NULL OR model_config_hash ~ '^[0-9a-f]{64}$')",
            name="ck_taxonomy_bootstrap_runs_hashes",
        ),
        CheckConstraint(
            "btrim(source_key) <> '' AND btrim(source_version) <> '' AND "
            "jsonb_typeof(warning_items) = 'array'",
            name="ck_taxonomy_bootstrap_runs_values",
        ),
        CheckConstraint(
            "((source_type = 'llm_proposal' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL "
            "AND prompt_version IS NOT NULL) OR "
            "(source_type <> 'llm_proposal' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL "
            "AND prompt_version IS NULL))",
            name="ck_taxonomy_bootstrap_runs_model",
        ),
        CheckConstraint(
            "((status = 'processing' AND source_type = 'llm_proposal' "
            "AND attempt_token IS NOT NULL AND expires_at IS NOT NULL "
            "AND output_taxonomy_id IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND attempt_token IS NULL AND expires_at IS NULL "
            "AND output_taxonomy_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND attempt_token IS NULL AND expires_at IS NULL "
            "AND output_taxonomy_id IS NULL AND error_code IS NOT NULL "
            "AND finished_at IS NOT NULL))",
            name="ck_taxonomy_bootstrap_runs_state",
        ),
        CheckConstraint(
            "source_type = 'llm_proposal' OR status = 'succeeded'",
            name="ck_taxonomy_bootstrap_runs_direct_success",
        ),
        UniqueConstraint(
            "organization_id",
            "request_id",
            name="uq_taxonomy_bootstrap_runs_org_request",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_taxonomy_bootstrap_runs_idempotency",
        ),
        UniqueConstraint(
            "output_taxonomy_id",
            name="uq_taxonomy_bootstrap_runs_output",
        ),
        Index(
            "ix_taxonomy_bootstrap_runs_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
        Index(
            "uq_taxonomy_bootstrap_one_processing_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'processing'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_organizations.id",
            ondelete="RESTRICT",
            name="fk_taxonomy_bootstrap_runs_organization",
        ),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    warning_items: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    output_taxonomy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomies.id",
            ondelete="RESTRICT",
            name="fk_taxonomy_bootstrap_runs_output",
        ),
        nullable=True,
    )
    model_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model_config_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_taxonomy_bootstrap_runs_created_by",
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


class TaxonomyBootstrapSource(Base):
    __tablename__ = "classification_taxonomy_bootstrap_sources"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 19",
            name="ck_taxonomy_bootstrap_sources_ordinal",
        ),
        CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND btrim(security_level) <> ''",
            name="ck_taxonomy_bootstrap_sources_values",
        ),
        UniqueConstraint(
            "bootstrap_run_id",
            "ordinal",
            name="uq_taxonomy_bootstrap_sources_run_ordinal",
        ),
        UniqueConstraint(
            "bootstrap_run_id",
            "document_revision_id",
            name="uq_taxonomy_bootstrap_sources_run_revision",
        ),
        Index(
            "ix_taxonomy_bootstrap_sources_run_order",
            "bootstrap_run_id",
            "ordinal",
        ),
        Index(
            "ix_taxonomy_bootstrap_sources_revision",
            "document_revision_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bootstrap_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomy_bootstrap_runs.id",
            ondelete="CASCADE",
            name="fk_taxonomy_bootstrap_sources_run",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="RESTRICT",
            name="fk_taxonomy_bootstrap_sources_library",
        ),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="RESTRICT",
            name="fk_taxonomy_bootstrap_sources_document",
        ),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_taxonomy_bootstrap_sources_revision",
        ),
        nullable=False,
    )
    revision_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    security_level: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
