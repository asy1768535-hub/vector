"""v0.8 knowledge artifact foundation

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _fk_column(
    column_name: str,
    target: str,
    constraint_name: str,
    ondelete: str,
    *,
    nullable: bool = False,
) -> sa.Column:
    return sa.Column(
        column_name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, name=constraint_name, ondelete=ondelete),
        nullable=nullable,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def upgrade() -> None:
    _create_knowledge_artifact_jobs()
    _create_knowledge_artifacts()


def _create_knowledge_artifact_jobs() -> None:
    table = "knowledge_artifact_jobs"
    op.create_table(
        "knowledge_artifact_jobs",
        _id_column(),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_knowledge_artifact_jobs_library",
            "CASCADE",
        ),
        _fk_column(
            "document_id",
            "documents.id",
            "fk_knowledge_artifact_jobs_document",
            "RESTRICT",
        ),
        _fk_column(
            "document_revision_id",
            "document_revisions.id",
            "fk_knowledge_artifact_jobs_revision",
            "RESTRICT",
        ),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("generation_mode", sa.String(length=32), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("model_config_hash", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("retry_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        _fk_column(
            "rerun_of_job_id",
            "knowledge_artifact_jobs.id",
            "fk_knowledge_artifact_jobs_rerun",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "requested_by_user_id",
            "sys_users.id",
            "fk_knowledge_artifact_jobs_requested_by",
            "SET NULL",
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "artifact_type IN ('summary','outline')",
            name="ck_knowledge_artifact_jobs_type",
        ),
        sa.CheckConstraint(
            "((artifact_type = 'summary' AND contract_version = 'summary-v1') OR "
            "(artifact_type = 'outline' AND contract_version = 'outline-v1'))",
            name="ck_knowledge_artifact_jobs_contract",
        ),
        sa.CheckConstraint(
            "generation_mode IN ('deterministic','model')",
            name="ck_knowledge_artifact_jobs_generation_mode",
        ),
        sa.CheckConstraint(
            "((generation_mode = 'deterministic' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL) OR "
            "(generation_mode = 'model' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL))",
            name="ck_knowledge_artifact_jobs_model_identity",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_knowledge_artifact_jobs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_knowledge_artifact_jobs_status",
        ),
        sa.CheckConstraint(
            "retry_generation >= 0",
            name="ck_knowledge_artifact_jobs_retry_generation",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_knowledge_artifact_jobs_idempotency_key"
        ),
    )
    op.create_index(
        "ix_knowledge_artifact_jobs_status_created",
        table,
        ["status", "created_at"],
    )
    op.create_index(
        "ix_knowledge_artifact_jobs_library_status_created",
        table,
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_knowledge_artifact_jobs_revision_type_created",
        table,
        ["document_revision_id", "artifact_type", "created_at"],
    )
    op.create_index(
        "ix_knowledge_artifact_jobs_rerun", table, ["rerun_of_job_id"]
    )


def _create_knowledge_artifacts() -> None:
    table = "knowledge_artifacts"
    op.create_table(
        "knowledge_artifacts",
        _id_column(),
        _fk_column(
            "job_id",
            "knowledge_artifact_jobs.id",
            "fk_knowledge_artifacts_job",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_knowledge_artifacts_library",
            "CASCADE",
        ),
        _fk_column(
            "document_id",
            "documents.id",
            "fk_knowledge_artifacts_document",
            "RESTRICT",
        ),
        _fk_column(
            "document_revision_id",
            "document_revisions.id",
            "fk_knowledge_artifacts_revision",
            "RESTRICT",
        ),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("generation_mode", sa.String(length=32), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("model_config_hash", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="current",
        ),
        _created_at(),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "artifact_type IN ('summary','outline')",
            name="ck_knowledge_artifacts_type",
        ),
        sa.CheckConstraint(
            "((artifact_type = 'summary' AND contract_version = 'summary-v1') OR "
            "(artifact_type = 'outline' AND contract_version = 'outline-v1'))",
            name="ck_knowledge_artifacts_contract",
        ),
        sa.CheckConstraint(
            "generation_mode IN ('deterministic','model')",
            name="ck_knowledge_artifacts_generation_mode",
        ),
        sa.CheckConstraint(
            "((generation_mode = 'deterministic' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL) OR "
            "(generation_mode = 'model' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL))",
            name="ck_knowledge_artifacts_model_identity",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('current','stale','deleted')",
            name="ck_knowledge_artifacts_lifecycle",
        ),
        sa.CheckConstraint(
            "((lifecycle_state = 'current' AND stale_at IS NULL AND deleted_at IS NULL) OR "
            "(lifecycle_state = 'stale' AND stale_at IS NOT NULL AND deleted_at IS NULL) OR "
            "(lifecycle_state = 'deleted' AND deleted_at IS NOT NULL))",
            name="ck_knowledge_artifacts_lifecycle_timestamps",
        ),
        sa.UniqueConstraint("job_id", name="uq_knowledge_artifacts_job"),
    )
    op.create_index(
        "uq_knowledge_artifacts_current_revision_type",
        table,
        ["document_revision_id", "artifact_type"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'current'"),
    )
    op.create_index(
        "ix_knowledge_artifacts_library_type_lifecycle",
        table,
        ["library_id", "artifact_type", "lifecycle_state"],
    )
    op.create_index(
        "ix_knowledge_artifacts_document_type_created",
        table,
        ["document_id", "artifact_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_artifacts")
    op.drop_table("knowledge_artifact_jobs")
