"""v0.8 personal Library scopes

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_library_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=True),
        sa.Column("normalized_name", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "scope_kind IN ('last_used','named')",
            name="ck_user_library_scopes_kind",
        ),
        sa.CheckConstraint(
            "((scope_kind = 'last_used' AND name IS NULL AND normalized_name IS NULL) OR "
            "(scope_kind = 'named' AND name IS NOT NULL AND normalized_name IS NOT NULL))",
            name="ck_user_library_scopes_name_shape",
        ),
        sa.CheckConstraint(
            "name IS NULL OR char_length(name) BETWEEN 1 AND 80",
            name="ck_user_library_scopes_name_length",
        ),
        sa.CheckConstraint(
            "normalized_name IS NULL OR char_length(normalized_name) BETWEEN 1 AND 80",
            name="ck_user_library_scopes_normalized_name_length",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            name="fk_user_library_scopes_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sys_users.id"],
            name="fk_user_library_scopes_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_user_library_scopes_last_used_owner",
        "user_library_scopes",
        ["organization_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'last_used'"),
    )
    op.create_index(
        "uq_user_library_scopes_named_owner_name",
        "user_library_scopes",
        ["organization_id", "user_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'named'"),
    )
    op.create_index(
        "ix_user_library_scopes_owner_kind_updated",
        "user_library_scopes",
        ["organization_id", "user_id", "scope_kind", "updated_at"],
    )
    op.create_table(
        "user_library_scope_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND ordinal < 20",
            name="ck_user_library_scope_items_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["user_library_scopes.id"],
            name="fk_user_library_scope_items_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_user_library_scope_items_library",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_id",
            "library_id",
            name="uq_user_library_scope_items_scope_library",
        ),
        sa.UniqueConstraint(
            "scope_id",
            "ordinal",
            name="uq_user_library_scope_items_scope_ordinal",
        ),
    )
    op.create_index(
        "ix_user_library_scope_items_library",
        "user_library_scope_items",
        ["library_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_library_scope_items_library", table_name="user_library_scope_items")
    op.drop_table("user_library_scope_items")
    op.drop_index("ix_user_library_scopes_owner_kind_updated", table_name="user_library_scopes")
    op.drop_index("uq_user_library_scopes_named_owner_name", table_name="user_library_scopes")
    op.drop_index("uq_user_library_scopes_last_used_owner", table_name="user_library_scopes")
    op.drop_table("user_library_scopes")
