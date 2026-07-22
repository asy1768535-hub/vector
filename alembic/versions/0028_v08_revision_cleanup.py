"""v0.8 revision file cleanup execution state

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_revision_files",
        sa.Column(
            "lifecycle_status",
            sa.String(length=32),
            nullable=False,
            server_default="available",
        ),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("delete_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_document_revision_files_lifecycle_status",
        "document_revision_files",
        "lifecycle_status IN ('available','deleting','deleted','released')",
    )
    op.create_check_constraint(
        "ck_document_revision_files_lifecycle_shape",
        "document_revision_files",
        "((lifecycle_status IN ('available','deleting') AND deleted_at IS NULL AND "
        "delete_verified_at IS NULL) OR "
        "(lifecycle_status = 'deleted' AND deleted_at IS NOT NULL AND "
        "delete_verified_at IS NOT NULL AND delete_verified_at >= deleted_at) OR "
        "(lifecycle_status = 'released' AND deleted_at IS NOT NULL AND "
        "delete_verified_at IS NULL))",
    )

    op.add_column(
        "revision_retention_records",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("worker_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "revision_retention_records",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_revision_retention_attempt_count",
        "revision_retention_records",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_revision_retention_claim_shape",
        "revision_retention_records",
        "((status = 'processing' AND worker_id IS NOT NULL AND "
        "claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
        "lease_expires_at IS NOT NULL AND lease_expires_at > claimed_at) OR "
        "(status <> 'processing' AND worker_id IS NULL AND claim_token IS NULL AND "
        "claimed_at IS NULL AND lease_expires_at IS NULL))",
    )
    op.create_check_constraint(
        "ck_revision_retention_available_shape",
        "revision_retention_records",
        "((status IN ('queued','failed') AND available_at IS NOT NULL) OR "
        "(status NOT IN ('queued','failed') AND available_at IS NULL))",
    )
    op.create_check_constraint(
        "ck_revision_retention_finished_shape",
        "revision_retention_records",
        "((status = 'cleaned' AND finished_at IS NOT NULL) OR "
        "(status <> 'cleaned' AND finished_at IS NULL))",
    )
    op.create_check_constraint(
        "ck_revision_retention_error_shape",
        "revision_retention_records",
        "((status = 'failed' AND last_error_code IS NOT NULL) OR "
        "(status <> 'failed' AND last_error_code IS NULL))",
    )
    op.create_index(
        "ix_revision_retention_cleanup_due",
        "revision_retention_records",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_revision_retention_cleanup_lease",
        "revision_retention_records",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_revision_retention_cleanup_lease",
        table_name="revision_retention_records",
    )
    op.drop_index(
        "ix_revision_retention_cleanup_due",
        table_name="revision_retention_records",
    )
    for name in (
        "ck_revision_retention_error_shape",
        "ck_revision_retention_finished_shape",
        "ck_revision_retention_available_shape",
        "ck_revision_retention_claim_shape",
        "ck_revision_retention_attempt_count",
    ):
        op.drop_constraint(name, "revision_retention_records", type_="check")
    for name in (
        "last_error_code",
        "finished_at",
        "lease_expires_at",
        "claimed_at",
        "claim_token",
        "worker_id",
        "available_at",
        "attempt_count",
    ):
        op.drop_column("revision_retention_records", name)

    op.drop_constraint(
        "ck_document_revision_files_lifecycle_shape",
        "document_revision_files",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_revision_files_lifecycle_status",
        "document_revision_files",
        type_="check",
    )
    op.drop_column("document_revision_files", "delete_verified_at")
    op.drop_column("document_revision_files", "deleted_at")
    op.drop_column("document_revision_files", "lifecycle_status")
