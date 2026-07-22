"""v0.8 classification decision governance

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_classification_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_content_hash", sa.String(length=64), nullable=False),
        sa.Column("taxonomy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled_label_set_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("min_confidence_micros", sa.Integer(), nullable=False),
        sa.Column("min_margin_micros", sa.Integer(), nullable=False),
        sa.Column("max_secondary_labels", sa.Integer(), nullable=False),
        sa.Column("classifier_version", sa.String(length=64), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_config_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("retry_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision_content_hash ~ '^[0-9a-f]{64}$' AND "
            "enabled_label_set_hash ~ '^[0-9a-f]{64}$' AND "
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_document_classification_runs_hashes",
        ),
        sa.CheckConstraint(
            "btrim(policy_version) <> '' AND btrim(classifier_version) <> '' AND "
            "btrim(model_provider) <> '' AND btrim(model_name) <> '' AND "
            "btrim(prompt_version) <> ''",
            name="ck_document_classification_runs_identities",
        ),
        sa.CheckConstraint(
            "min_confidence_micros BETWEEN 0 AND 1000000 AND "
            "min_margin_micros BETWEEN 0 AND 1000000 AND "
            "max_secondary_labels BETWEEN 0 AND 8",
            name="ck_document_classification_runs_policy",
        ),
        sa.CheckConstraint(
            "generation_no > 0 AND retry_generation >= 0",
            name="ck_document_classification_runs_generation",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('revision_ready','manual','retry','repair')",
            name="ck_document_classification_runs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','auto_applied','blocked_manual',"
            "'manual_applied','rejected','failed','stale')",
            name="ck_document_classification_runs_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) <= 16",
            name="ck_document_classification_runs_reason_codes",
        ),
        sa.CheckConstraint(
            "((status = 'failed' AND error_code IS NOT NULL) OR "
            "(status <> 'failed' AND error_code IS NULL))",
            name="ck_document_classification_runs_error_shape",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_document_classification_runs_library",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_classification_runs_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            name="fk_document_classification_runs_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_version_id"],
            ["classification_taxonomies.id"],
            name="fk_document_classification_runs_taxonomy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["sys_users.id"],
            name="fk_document_classification_runs_requested_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_document_classification_runs_idempotency_key",
        ),
        sa.UniqueConstraint(
            "document_revision_id",
            "generation_no",
            name="uq_document_classification_runs_revision_generation",
        ),
    )
    op.create_index(
        "ix_document_classification_runs_library_status_created",
        "document_classification_runs",
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_document_classification_runs_revision_created",
        "document_classification_runs",
        ["document_revision_id", "created_at"],
    )
    op.create_index(
        "ix_document_classification_runs_taxonomy_status",
        "document_classification_runs",
        ["taxonomy_version_id", "status"],
    )

    op.create_table(
        "document_classification_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("proposed_key", sa.String(length=64), nullable=True),
        sa.Column("proposed_label", sa.String(length=160), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("confidence_micros", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "((label_id IS NOT NULL AND proposed_key IS NULL AND proposed_label IS NULL) OR "
            "(label_id IS NULL AND proposed_key IS NOT NULL AND proposed_label IS NOT NULL))",
            name="ck_document_classification_proposals_target_shape",
        ),
        sa.CheckConstraint(
            "proposed_key IS NULL OR proposed_key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_document_classification_proposals_key",
        ),
        sa.CheckConstraint(
            "proposed_label IS NULL OR btrim(proposed_label) <> ''",
            name="ck_document_classification_proposals_label",
        ),
        sa.CheckConstraint(
            "role IN ('primary','secondary')",
            name="ck_document_classification_proposals_role",
        ),
        sa.CheckConstraint(
            "rank BETWEEN 0 AND 49 AND confidence_micros BETWEEN 0 AND 1000000",
            name="ck_document_classification_proposals_score",
        ),
        sa.CheckConstraint(
            "status IN ('auto_selected','not_selected','pending_review','blocked_manual',"
            "'accepted','rejected','invalid','stale')",
            name="ck_document_classification_proposals_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) <= 16",
            name="ck_document_classification_proposals_reason_codes",
        ),
        sa.CheckConstraint(
            "((status IN ('accepted','rejected') AND reviewed_at IS NOT NULL) OR "
            "(status NOT IN ('accepted','rejected') AND reviewed_at IS NULL "
            "AND reviewed_by_user_id IS NULL))",
            name="ck_document_classification_proposals_review_shape",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["document_classification_runs.id"],
            name="fk_document_classification_proposals_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["label_id"],
            ["classification_labels.id"],
            name="fk_document_classification_proposals_label",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["sys_users.id"],
            name="fk_document_classification_proposals_reviewed_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "role",
            "rank",
            name="uq_document_classification_proposals_run_role_rank",
        ),
    )
    op.create_index(
        "ix_document_classification_proposals_run_status",
        "document_classification_proposals",
        ["run_id", "status", "role", "rank"],
    )
    op.create_index(
        "ix_document_classification_proposals_label",
        "document_classification_proposals",
        ["label_id"],
    )

    op.create_table(
        "document_classification_decision_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("classifier_version", sa.String(length=64), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("model_config_hash", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("supersedes_decision_set_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('model','manual')",
            name="ck_document_classification_decision_sets_source",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('effective','superseded','removed')",
            name="ck_document_classification_decision_sets_lifecycle",
        ),
        sa.CheckConstraint(
            "generation_no > 0",
            name="ck_document_classification_decision_sets_generation",
        ),
        sa.CheckConstraint(
            "((lifecycle = 'effective' AND superseded_at IS NULL AND removed_at IS NULL) OR "
            "(lifecycle = 'superseded' AND superseded_at IS NOT NULL AND removed_at IS NULL) OR "
            "(lifecycle = 'removed' AND removed_at IS NOT NULL AND superseded_at IS NULL))",
            name="ck_document_classification_decision_sets_lifecycle_shape",
        ),
        sa.CheckConstraint(
            "supersedes_decision_set_id IS NULL OR supersedes_decision_set_id <> id",
            name="ck_document_classification_decision_sets_supersedes_not_self",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "lifecycle <> 'removed' OR source = 'manual'",
            name="ck_document_classification_decision_sets_removed_manual",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_document_classification_decision_sets_library",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_classification_decision_sets_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            name="fk_document_classification_decision_sets_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_version_id"],
            ["classification_taxonomies.id"],
            name="fk_document_classification_decision_sets_taxonomy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["document_classification_runs.id"],
            name="fk_document_classification_decision_sets_source_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_set_id"],
            ["document_classification_decision_sets.id"],
            name="fk_document_classification_decision_sets_supersedes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["sys_users.id"],
            name="fk_document_classification_decision_sets_reviewed_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_revision_id",
            "generation_no",
            name="uq_document_classification_decision_sets_revision_generation",
        ),
    )
    op.create_index(
        "uq_document_classification_decision_sets_effective_revision",
        "document_classification_decision_sets",
        ["document_revision_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle = 'effective'"),
    )
    op.create_index(
        "ix_doc_class_decision_sets_library_lifecycle",
        "document_classification_decision_sets",
        ["library_id", "lifecycle", "created_at"],
    )
    op.create_index(
        "ix_document_classification_decision_sets_document_revision",
        "document_classification_decision_sets",
        ["document_id", "document_revision_id", "generation_no"],
    )

    op.create_table(
        "document_classification_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("confidence_micros", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "((role = 'primary' AND ordinal = 0) OR "
            "(role = 'secondary' AND ordinal BETWEEN 1 AND 8))",
            name="ck_document_classification_decisions_role_ordinal",
        ),
        sa.CheckConstraint(
            "confidence_micros IS NULL OR confidence_micros BETWEEN 0 AND 1000000",
            name="ck_document_classification_decisions_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["decision_set_id"],
            ["document_classification_decision_sets.id"],
            name="fk_document_classification_decisions_set",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["label_id"],
            ["classification_labels.id"],
            name="fk_document_classification_decisions_label",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_set_id",
            "ordinal",
            name="uq_document_classification_decisions_set_ordinal",
        ),
        sa.UniqueConstraint(
            "decision_set_id",
            "label_id",
            name="uq_document_classification_decisions_set_label",
        ),
    )
    op.create_index(
        "ix_document_classification_decisions_label",
        "document_classification_decisions",
        ["label_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_classification_decisions_label",
        table_name="document_classification_decisions",
    )
    op.drop_table("document_classification_decisions")
    op.drop_index(
        "ix_document_classification_decision_sets_document_revision",
        table_name="document_classification_decision_sets",
    )
    op.drop_index(
        "ix_doc_class_decision_sets_library_lifecycle",
        table_name="document_classification_decision_sets",
    )
    op.drop_index(
        "uq_document_classification_decision_sets_effective_revision",
        table_name="document_classification_decision_sets",
    )
    op.drop_table("document_classification_decision_sets")
    op.drop_index(
        "ix_document_classification_proposals_label",
        table_name="document_classification_proposals",
    )
    op.drop_index(
        "ix_document_classification_proposals_run_status",
        table_name="document_classification_proposals",
    )
    op.drop_table("document_classification_proposals")
    op.drop_index(
        "ix_document_classification_runs_taxonomy_status",
        table_name="document_classification_runs",
    )
    op.drop_index(
        "ix_document_classification_runs_revision_created",
        table_name="document_classification_runs",
    )
    op.drop_index(
        "ix_document_classification_runs_library_status_created",
        table_name="document_classification_runs",
    )
    op.drop_table("document_classification_runs")
