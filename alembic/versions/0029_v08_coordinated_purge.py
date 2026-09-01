"""v0.8 coordinated graph purge operation

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_graph_publications_source_mode",
        "graph_publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_graph_publications_source_mode",
        "graph_publications",
        "source_mode IN ('initial_seed','manual_plan','rollback','coordinated_purge')",
    )
    op.create_table(
        "revision_purge_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("retention_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "replacement_publication_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "impact_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("impact_hash", sa.String(length=64), nullable=False),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("replacement_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.func.now(),
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "status IN ('planned','processing','cleanup_pending','completed','failed','cancelled')",
            name="ck_revision_purge_operations_status",
        ),
        sa.CheckConstraint(
            "source_publication_id <> replacement_publication_id",
            name="ck_revision_purge_operations_publications_differ",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_revision_purge_operations_attempt_count",
        ),
        sa.CheckConstraint(
            "confirmation_hash ~ '^[0-9a-f]{64}$' AND "
            "impact_hash ~ '^[0-9a-f]{64}$' AND "
            "source_manifest_hash ~ '^[0-9a-f]{64}$' AND "
            "replacement_manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_revision_purge_operations_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(impact_snapshot) = 'object' AND "
            "octet_length(impact_snapshot::text) <= 16384",
            name="ck_revision_purge_operations_impact",
        ),
        sa.CheckConstraint(
            "((status = 'processing' AND worker_id IS NOT NULL AND "
            "claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL AND lease_expires_at > claimed_at) OR "
            "(status <> 'processing' AND worker_id IS NULL AND claim_token IS NULL AND "
            "claimed_at IS NULL AND lease_expires_at IS NULL))",
            name="ck_revision_purge_operations_claim_shape",
        ),
        sa.CheckConstraint(
            "((status IN ('planned','failed') AND available_at IS NOT NULL) OR "
            "(status NOT IN ('planned','failed') AND available_at IS NULL))",
            name="ck_revision_purge_operations_available_shape",
        ),
        sa.CheckConstraint(
            "((status IN ('completed','cancelled') AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed','cancelled') AND finished_at IS NULL))",
            name="ck_revision_purge_operations_finished_shape",
        ),
        sa.CheckConstraint(
            "((status = 'failed' AND last_error_code IS NOT NULL) OR "
            "(status <> 'failed' AND last_error_code IS NULL))",
            name="ck_revision_purge_operations_error_shape",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["sys_libraries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retention_record_id"],
            ["revision_retention_records.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_file_id"],
            ["document_revision_files.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_publication_id"],
            ["graph_publications.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replacement_publication_id"],
            ["graph_publications.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["sys_users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "retention_record_id",
            name="uq_revision_purge_operations_retention",
        ),
        sa.UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_revision_purge_operations_idempotency",
        ),
    )
    op.create_index(
        "ix_revision_purge_operations_due",
        "revision_purge_operations",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_revision_purge_operations_lease",
        "revision_purge_operations",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_revision_purge_operations_cleanup_pending",
        "revision_purge_operations",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_revision_purge_operations_library_status",
        "revision_purge_operations",
        ["library_id", "status"],
    )


def downgrade() -> None:
    for name in (
        "ix_revision_purge_operations_library_status",
        "ix_revision_purge_operations_cleanup_pending",
        "ix_revision_purge_operations_lease",
        "ix_revision_purge_operations_due",
    ):
        op.drop_index(name, table_name="revision_purge_operations")
    op.drop_table("revision_purge_operations")
    op.drop_constraint(
        "ck_graph_publications_source_mode",
        "graph_publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_graph_publications_source_mode",
        "graph_publications",
        "source_mode IN ('initial_seed','manual_plan','rollback')",
    )
