"""v0.8 taxonomy bootstrap provenance

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "classification_taxonomy_bootstrap_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("source_version", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("warning_items", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("output_taxonomy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("model_config_hash", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('builtin_template','admin_import','llm_proposal')",
            name="ck_taxonomy_bootstrap_runs_source",
        ),
        sa.CheckConstraint(
            "status IN ('processing','succeeded','failed')",
            name="ck_taxonomy_bootstrap_runs_status",
        ),
        sa.CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "idempotency_key ~ '^[0-9a-f]{64}$' AND "
            "(model_config_hash IS NULL OR model_config_hash ~ '^[0-9a-f]{64}$')",
            name="ck_taxonomy_bootstrap_runs_hashes",
        ),
        sa.CheckConstraint(
            "btrim(source_key) <> '' AND btrim(source_version) <> '' AND "
            "jsonb_typeof(warning_items) = 'array'",
            name="ck_taxonomy_bootstrap_runs_values",
        ),
        sa.CheckConstraint(
            "((source_type = 'llm_proposal' AND model_provider IS NOT NULL "
            "AND model_name IS NOT NULL AND model_config_hash IS NOT NULL "
            "AND prompt_version IS NOT NULL) OR "
            "(source_type <> 'llm_proposal' AND model_provider IS NULL "
            "AND model_name IS NULL AND model_config_hash IS NULL "
            "AND prompt_version IS NULL))",
            name="ck_taxonomy_bootstrap_runs_model",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "source_type = 'llm_proposal' OR status = 'succeeded'",
            name="ck_taxonomy_bootstrap_runs_direct_success",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            name="fk_taxonomy_bootstrap_runs_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["output_taxonomy_id"],
            ["classification_taxonomies.id"],
            name="fk_taxonomy_bootstrap_runs_output",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["sys_users.id"],
            name="fk_taxonomy_bootstrap_runs_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "request_id",
            name="uq_taxonomy_bootstrap_runs_org_request",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_taxonomy_bootstrap_runs_idempotency",
        ),
        sa.UniqueConstraint(
            "output_taxonomy_id",
            name="uq_taxonomy_bootstrap_runs_output",
        ),
    )
    op.create_index(
        "ix_taxonomy_bootstrap_runs_org_created",
        "classification_taxonomy_bootstrap_runs",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "uq_taxonomy_bootstrap_one_processing_org",
        "classification_taxonomy_bootstrap_runs",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "classification_taxonomy_bootstrap_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bootstrap_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_content_hash", sa.String(length=64), nullable=False),
        sa.Column("security_level", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 19",
            name="ck_taxonomy_bootstrap_sources_ordinal",
        ),
        sa.CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND btrim(security_level) <> ''",
            name="ck_taxonomy_bootstrap_sources_values",
        ),
        sa.ForeignKeyConstraint(
            ["bootstrap_run_id"],
            ["classification_taxonomy_bootstrap_runs.id"],
            name="fk_taxonomy_bootstrap_sources_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_taxonomy_bootstrap_sources_library",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_taxonomy_bootstrap_sources_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            name="fk_taxonomy_bootstrap_sources_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bootstrap_run_id",
            "ordinal",
            name="uq_taxonomy_bootstrap_sources_run_ordinal",
        ),
        sa.UniqueConstraint(
            "bootstrap_run_id",
            "document_revision_id",
            name="uq_taxonomy_bootstrap_sources_run_revision",
        ),
    )
    op.create_index(
        "ix_taxonomy_bootstrap_sources_run_order",
        "classification_taxonomy_bootstrap_sources",
        ["bootstrap_run_id", "ordinal"],
    )
    op.create_index(
        "ix_taxonomy_bootstrap_sources_revision",
        "classification_taxonomy_bootstrap_sources",
        ["document_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_taxonomy_bootstrap_sources_revision",
        table_name="classification_taxonomy_bootstrap_sources",
    )
    op.drop_index(
        "ix_taxonomy_bootstrap_sources_run_order",
        table_name="classification_taxonomy_bootstrap_sources",
    )
    op.drop_table("classification_taxonomy_bootstrap_sources")
    op.drop_index(
        "uq_taxonomy_bootstrap_one_processing_org",
        table_name="classification_taxonomy_bootstrap_runs",
    )
    op.drop_index(
        "ix_taxonomy_bootstrap_runs_org_created",
        table_name="classification_taxonomy_bootstrap_runs",
    )
    op.drop_table("classification_taxonomy_bootstrap_runs")
