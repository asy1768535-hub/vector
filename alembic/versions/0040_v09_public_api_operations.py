"""v0.9 public API operational controls.

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "public_api_request_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("endpoint_key", sa.String(length=64), nullable=False),
        sa.Column("http_method", sa.String(length=8), nullable=False),
        sa.Column(
            "library_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("is_stream", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("graph_entity_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("graph_relation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("answer_model", sa.String(length=160), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_id ~ '^[0-9a-f]{32}$'",
            name="ck_public_api_request_records_request_id",
        ),
        sa.CheckConstraint(
            "endpoint_key ~ '^[a-z][a-z0-9_.-]{0,63}$' "
            "AND http_method IN ('GET','POST')",
            name="ck_public_api_request_records_endpoint",
        ),
        sa.CheckConstraint(
            "cardinality(library_ids) <= 20 "
            "AND array_position(library_ids, NULL) IS NULL",
            name="ck_public_api_request_records_libraries",
        ),
        sa.CheckConstraint(
            "finished_at >= started_at AND duration_ms >= 0 "
            "AND http_status BETWEEN 100 AND 599",
            name="ck_public_api_request_records_timing",
        ),
        sa.CheckConstraint(
            "outcome IN ('completed','failed','cancelled','disconnected') "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_public_api_request_records_outcome",
        ),
        sa.CheckConstraint(
            "source_count >= 0 AND chunk_count >= 0 "
            "AND graph_entity_count >= 0 AND graph_relation_count >= 0 "
            "AND (input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0)",
            name="ck_public_api_request_records_counts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            name="fk_public_api_request_records_organization",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sys_users.id"],
            name="fk_public_api_request_records_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["sys_api_keys.id"],
            name="fk_public_api_request_records_api_key",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_public_api_request_records"),
        sa.UniqueConstraint("request_id", name="uq_public_api_request_records_request_id"),
    )
    op.create_index(
        "ix_public_api_request_records_retention",
        "public_api_request_records",
        ["finished_at", "id"],
    )
    op.create_index(
        "ix_public_api_request_records_organization_finished",
        "public_api_request_records",
        ["organization_id", sa.text("finished_at DESC")],
    )
    op.create_index(
        "ix_public_api_request_records_api_key_finished",
        "public_api_request_records",
        ["api_key_id", sa.text("finished_at DESC")],
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
    )

    op.create_table(
        "public_api_rate_windows",
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_kind IN ('organization','api_key') AND request_count > 0",
            name="ck_public_api_rate_windows_values",
        ),
        sa.CheckConstraint(
            "date_trunc('minute', window_started_at) = window_started_at",
            name="ck_public_api_rate_windows_boundary",
        ),
        sa.PrimaryKeyConstraint(
            "scope_kind",
            "scope_id",
            "window_started_at",
            name="pk_public_api_rate_windows",
        ),
    )
    op.create_index(
        "ix_public_api_rate_windows_started",
        "public_api_rate_windows",
        ["window_started_at"],
    )

    op.create_table(
        "public_api_answer_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("endpoint_key", sa.String(length=64), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "request_id ~ '^[0-9a-f]{32}$' "
            "AND endpoint_key IN ('answers.create','answers.stream')",
            name="ck_public_api_answer_leases_identity",
        ),
        sa.CheckConstraint(
            "expires_at > acquired_at",
            name="ck_public_api_answer_leases_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            name="fk_public_api_answer_leases_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["sys_api_keys.id"],
            name="fk_public_api_answer_leases_api_key",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_public_api_answer_leases"),
        sa.UniqueConstraint("request_id", name="uq_public_api_answer_leases_request_id"),
    )
    op.create_index(
        "ix_public_api_answer_leases_organization_expiry",
        "public_api_answer_leases",
        ["organization_id", "expires_at"],
    )
    op.create_index(
        "ix_public_api_answer_leases_api_key_expiry",
        "public_api_answer_leases",
        ["api_key_id", "expires_at"],
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
    )
    op.create_index(
        "ix_public_api_answer_leases_expiry",
        "public_api_answer_leases",
        ["expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_api_answer_leases_expiry",
        table_name="public_api_answer_leases",
    )
    op.drop_index(
        "ix_public_api_answer_leases_api_key_expiry",
        table_name="public_api_answer_leases",
    )
    op.drop_index(
        "ix_public_api_answer_leases_organization_expiry",
        table_name="public_api_answer_leases",
    )
    op.drop_table("public_api_answer_leases")

    op.drop_index(
        "ix_public_api_rate_windows_started",
        table_name="public_api_rate_windows",
    )
    op.drop_table("public_api_rate_windows")

    op.drop_index(
        "ix_public_api_request_records_api_key_finished",
        table_name="public_api_request_records",
    )
    op.drop_index(
        "ix_public_api_request_records_organization_finished",
        table_name="public_api_request_records",
    )
    op.drop_index(
        "ix_public_api_request_records_retention",
        table_name="public_api_request_records",
    )
    op.drop_table("public_api_request_records")
