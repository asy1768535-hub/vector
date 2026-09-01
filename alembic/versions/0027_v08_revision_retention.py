"""v0.8 revision file retention governance

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "revision_retention_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "revision_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
    )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "revision_retention_notice_days",
            sa.Integer(),
            nullable=False,
            server_default="7",
        ),
    )
    op.create_check_constraint(
        "ck_lib_revision_retention_policy",
        "sys_libraries",
        "revision_retention_days BETWEEN 30 AND 60 AND "
        "revision_retention_notice_days BETWEEN 1 AND 14 AND "
        "revision_retention_notice_days < revision_retention_days",
    )

    op.create_table(
        "revision_retention_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "replacement_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("revision_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reason",
            sa.String(length=32),
            nullable=False,
            server_default="replacement_ready",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column(
            "policy_version",
            sa.String(length=32),
            nullable=False,
            server_default="retention-policy-v1",
        ),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("notice_days", sa.Integer(), nullable=False),
        sa.Column("replacement_ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notice_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notice_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_code", sa.String(length=64), nullable=True),
        sa.Column(
            "impact_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("impact_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "deadline_changed_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("deadline_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_reason_code", sa.String(length=64), nullable=True),
        sa.Column("held_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled','eligible','blocked','held','queued',"
            "'processing','cleaned','failed','cancelled')",
            name="ck_revision_retention_status",
        ),
        sa.CheckConstraint(
            "reason = 'replacement_ready'",
            name="ck_revision_retention_reason",
        ),
        sa.CheckConstraint(
            "policy_version = 'retention-policy-v1'",
            name="ck_revision_retention_policy_version",
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 30 AND 60 AND "
            "notice_days BETWEEN 1 AND 14 AND notice_days < retention_days",
            name="ck_revision_retention_policy_bounds",
        ),
        sa.CheckConstraint(
            "cleanup_eligible_at >= replacement_ready_at AND "
            "cleanup_not_before >= cleanup_eligible_at AND "
            "notice_at < cleanup_not_before",
            name="ck_revision_retention_time_order",
        ),
        sa.CheckConstraint(
            "notice_recorded_at IS NULL OR notice_recorded_at >= notice_at",
            name="ck_revision_retention_notice_time",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(impact_snapshot) = 'object' AND "
            "octet_length(impact_snapshot::text) <= 8192 AND "
            "impact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_revision_retention_impact",
        ),
        sa.CheckConstraint(
            "((status IN ('blocked','cancelled') AND block_code IS NOT NULL) OR "
            "(status NOT IN ('blocked','cancelled') AND block_code IS NULL))",
            name="ck_revision_retention_block_code",
        ),
        sa.CheckConstraint(
            "((status = 'held' AND hold_reason_code IS NOT NULL AND "
            "held_by_user_id IS NOT NULL AND held_at IS NOT NULL) OR "
            "(status <> 'held' AND hold_reason_code IS NULL AND "
            "held_by_user_id IS NULL AND held_at IS NULL))",
            name="ck_revision_retention_hold_shape",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["sys_libraries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replacement_revision_id"],
            ["document_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_file_id"],
            ["document_revision_files.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deadline_changed_by_user_id"],
            ["sys_users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["held_by_user_id"], ["sys_users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_file_id", name="uq_revision_retention_revision_file"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_revision_retention_idempotency"
        ),
    )
    op.create_index(
        "ix_revision_retention_due",
        "revision_retention_records",
        ["status", "cleanup_not_before"],
    )
    op.create_index(
        "ix_revision_retention_notice_due",
        "revision_retention_records",
        ["notice_recorded_at", "notice_at"],
    )
    op.create_index(
        "ix_revision_retention_library_status",
        "revision_retention_records",
        ["library_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_revision_retention_library_status",
        table_name="revision_retention_records",
    )
    op.drop_index(
        "ix_revision_retention_notice_due",
        table_name="revision_retention_records",
    )
    op.drop_index(
        "ix_revision_retention_due",
        table_name="revision_retention_records",
    )
    op.drop_table("revision_retention_records")
    op.drop_constraint(
        "ck_lib_revision_retention_policy", "sys_libraries", type_="check"
    )
    op.drop_column("sys_libraries", "revision_retention_notice_days")
    op.drop_column("sys_libraries", "revision_retention_days")
    op.drop_column("sys_libraries", "revision_retention_enabled")
