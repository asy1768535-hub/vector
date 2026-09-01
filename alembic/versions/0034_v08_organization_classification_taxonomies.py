"""v0.8 Organization classification taxonomy foundation

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "classification_taxonomies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_key", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("activated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "version_key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_classification_taxonomies_version_key",
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name="ck_classification_taxonomies_version_no",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled')",
            name="ck_classification_taxonomies_status",
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 1000",
            name="ck_classification_taxonomies_description",
        ),
        sa.CheckConstraint(
            "parent_version_id IS NULL OR parent_version_id <> id",
            name="ck_classification_taxonomies_parent_not_self",
        ),
        sa.CheckConstraint(
            "((status = 'draft' AND activated_at IS NULL AND activated_by_user_id IS NULL) OR "
            "(status IN ('active','disabled') AND activated_at IS NOT NULL))",
            name="ck_classification_taxonomies_activation_shape",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            name="fk_classification_taxonomies_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["classification_taxonomies.id"],
            name="fk_classification_taxonomies_parent_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["sys_users.id"],
            name="fk_classification_taxonomies_created_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"],
            ["sys_users.id"],
            name="fk_classification_taxonomies_activated_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "version_key",
            "version_no",
            name="uq_classification_taxonomies_org_version",
        ),
    )
    op.create_index(
        "uq_classification_taxonomies_one_active_org",
        "classification_taxonomies",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_classification_taxonomies_org_status_created",
        "classification_taxonomies",
        ["organization_id", "status", "created_at"],
    )
    op.create_table(
        "classification_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("parent_label_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_classification_labels_key",
        ),
        sa.CheckConstraint(
            "btrim(label) <> ''",
            name="ck_classification_labels_label",
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 1000",
            name="ck_classification_labels_description",
        ),
        sa.CheckConstraint(
            "parent_label_id IS NULL OR parent_label_id <> id",
            name="ck_classification_labels_parent_not_self",
        ),
        sa.CheckConstraint(
            "sort_order BETWEEN 0 AND 1000000",
            name="ck_classification_labels_sort_order",
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled')",
            name="ck_classification_labels_status",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_version_id"],
            ["classification_taxonomies.id"],
            name="fk_classification_labels_taxonomy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_label_id"],
            ["classification_labels.id"],
            name="fk_classification_labels_parent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "taxonomy_version_id",
            "key",
            name="uq_classification_labels_taxonomy_key",
        ),
    )
    op.create_index(
        "ix_classification_labels_taxonomy_status_order",
        "classification_labels",
        ["taxonomy_version_id", "status", "sort_order", "key"],
    )
    op.create_index(
        "ix_classification_labels_parent",
        "classification_labels",
        ["parent_label_id"],
    )
    op.create_table(
        "library_classification_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 499",
            name="ck_library_classification_labels_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_library_classification_labels_library",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_version_id"],
            ["classification_taxonomies.id"],
            name="fk_library_classification_labels_taxonomy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_id"],
            ["classification_labels.id"],
            name="fk_library_classification_labels_label",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "label_id",
            name="uq_library_classification_labels_library_label",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ordinal",
            name="uq_library_classification_labels_library_ordinal",
        ),
    )
    op.create_index(
        "ix_library_classification_labels_library_taxonomy_order",
        "library_classification_labels",
        ["library_id", "taxonomy_version_id", "ordinal"],
    )
    op.create_index(
        "ix_library_classification_labels_label",
        "library_classification_labels",
        ["label_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_library_classification_labels_label",
        table_name="library_classification_labels",
    )
    op.drop_index(
        "ix_library_classification_labels_library_taxonomy_order",
        table_name="library_classification_labels",
    )
    op.drop_table("library_classification_labels")
    op.drop_index("ix_classification_labels_parent", table_name="classification_labels")
    op.drop_index(
        "ix_classification_labels_taxonomy_status_order",
        table_name="classification_labels",
    )
    op.drop_table("classification_labels")
    op.drop_index(
        "ix_classification_taxonomies_org_status_created",
        table_name="classification_taxonomies",
    )
    op.drop_index(
        "uq_classification_taxonomies_one_active_org",
        table_name="classification_taxonomies",
    )
    op.drop_table("classification_taxonomies")
