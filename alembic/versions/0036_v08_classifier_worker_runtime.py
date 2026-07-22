"""v0.8 classifier worker runtime

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HEARTBEAT_TYPES_V08_CLASSIFIER = (
    "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor',"
    "'knowledge_artifact_worker','classification_worker')"
)
_HEARTBEAT_TYPES_PRE_CLASSIFIER = (
    "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor',"
    "'knowledge_artifact_worker')"
)


def upgrade() -> None:
    op.create_table(
        "document_classification_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_content_hash", sa.String(length=64), nullable=False),
        sa.Column("taxonomy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled_label_set_hash", sa.String(length=64), nullable=False),
        sa.Column("classifier_version", sa.String(length=64), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_config_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("retry_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rerun_of_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND "
            "enabled_label_set_hash ~ '^[0-9a-f]{64}$' AND "
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_document_classification_jobs_hashes",
        ),
        sa.CheckConstraint(
            "btrim(classifier_version) <> '' AND btrim(model_provider) <> '' AND "
            "btrim(model_name) <> '' AND btrim(prompt_version) <> ''",
            name="ck_document_classification_jobs_identities",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_document_classification_jobs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_document_classification_jobs_status",
        ),
        sa.CheckConstraint(
            "retry_generation >= 0 AND attempt_count >= 0",
            name="ck_document_classification_jobs_attempts",
        ),
        sa.CheckConstraint(
            "((status = 'processing' AND claim_token IS NOT NULL "
            "AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND last_heartbeat_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL "
            "AND claimed_by IS NULL AND lease_expires_at IS NULL "
            "AND last_heartbeat_at IS NULL))",
            name="ck_document_classification_jobs_claim_state",
        ),
        sa.CheckConstraint(
            "((status = 'succeeded' AND result_run_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('cancelled','superseded') AND result_run_id IS NULL "
            "AND error_code IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('queued','processing') AND result_run_id IS NULL "
            "AND finished_at IS NULL))",
            name="ck_document_classification_jobs_result_state",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_document_classification_jobs_library",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_classification_jobs_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            name="fk_document_classification_jobs_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_version_id"],
            ["classification_taxonomies.id"],
            name="fk_document_classification_jobs_taxonomy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rerun_of_job_id"],
            ["document_classification_jobs.id"],
            name="fk_document_classification_jobs_rerun",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["sys_users.id"],
            name="fk_document_classification_jobs_requested_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_run_id"],
            ["document_classification_runs.id"],
            name="fk_document_classification_jobs_result_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_document_classification_jobs_idempotency_key",
        ),
        sa.UniqueConstraint(
            "result_run_id",
            name="uq_document_classification_jobs_result_run",
        ),
    )
    op.create_index(
        "ix_document_classification_jobs_status_created",
        "document_classification_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_doc_class_jobs_library_status_created",
        "document_classification_jobs",
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_doc_class_jobs_revision_created",
        "document_classification_jobs",
        ["document_revision_id", "created_at"],
    )
    op.create_index(
        "ix_document_classification_jobs_claimable",
        "document_classification_jobs",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_document_classification_jobs_rerun",
        "document_classification_jobs",
        ["rerun_of_job_id"],
    )

    for name in (
        "classification_auto_enabled",
        "classification_external_model_enabled",
    ):
        op.add_column(
            "sys_libraries",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "classification_allowed_security_levels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_lib_classification_security_levels_array",
        "sys_libraries",
        "jsonb_typeof(classification_allowed_security_levels) = 'array'",
    )

    op.drop_constraint(
        "ck_heartbeat_service_type", "service_heartbeats", type_="check"
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type",
        "service_heartbeats",
        _HEARTBEAT_TYPES_V08_CLASSIFIER,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM service_heartbeats WHERE service_type = 'classification_worker'"
    )
    op.drop_constraint(
        "ck_heartbeat_service_type", "service_heartbeats", type_="check"
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type",
        "service_heartbeats",
        _HEARTBEAT_TYPES_PRE_CLASSIFIER,
    )

    op.drop_constraint(
        "ck_lib_classification_security_levels_array",
        "sys_libraries",
        type_="check",
    )
    op.drop_column("sys_libraries", "classification_allowed_security_levels")
    op.drop_column("sys_libraries", "classification_external_model_enabled")
    op.drop_column("sys_libraries", "classification_auto_enabled")

    op.drop_index(
        "ix_document_classification_jobs_rerun",
        table_name="document_classification_jobs",
    )
    op.drop_index(
        "ix_document_classification_jobs_claimable",
        table_name="document_classification_jobs",
    )
    op.drop_index(
        "ix_doc_class_jobs_revision_created",
        table_name="document_classification_jobs",
    )
    op.drop_index(
        "ix_doc_class_jobs_library_status_created",
        table_name="document_classification_jobs",
    )
    op.drop_index(
        "ix_document_classification_jobs_status_created",
        table_name="document_classification_jobs",
    )
    op.drop_table("document_classification_jobs")
