"""v0.8 Organization identity and Library ownership

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ORGANIZATION_SLUG = "default"


def upgrade() -> None:
    op.create_table(
        "sys_organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "deployment_profile",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
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
            "slug ~ '^[a-z][a-z0-9_-]{1,62}[a-z0-9]$'",
            name="ck_sys_organizations_slug",
        ),
        sa.CheckConstraint(
            "btrim(name) <> ''", name="ck_sys_organizations_name"
        ),
        sa.CheckConstraint(
            "deployment_profile IN ('private','hosted')",
            name="ck_sys_organizations_deployment_profile",
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended')",
            name="ck_sys_organizations_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["sys_users.id"],
            ondelete="SET NULL",
            name="fk_sys_organizations_created_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_sys_organizations_slug"),
    )
    op.create_index(
        "ix_sys_organizations_status_created",
        "sys_organizations",
        ["status", "created_at"],
    )
    op.execute(
        f"""
            INSERT INTO sys_organizations (
                id, slug, name, deployment_profile, status
            ) VALUES (
                '{DEFAULT_ORGANIZATION_ID}'::uuid, '{DEFAULT_ORGANIZATION_SLUG}',
                'Default Organization',
                'private', 'active'
            )
            ON CONFLICT (id) DO NOTHING
            """
    )

    op.create_table(
        "sys_organization_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
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
            "role IN ('organization_admin','member')",
            name="ck_sys_organization_memberships_role",
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled')",
            name="ck_sys_organization_memberships_status",
        ),
        sa.CheckConstraint(
            "((status = 'active' AND disabled_at IS NULL) OR "
            "(status = 'disabled' AND disabled_at IS NOT NULL))",
            name="ck_sys_organization_memberships_disabled_shape",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sys_organizations.id"],
            ondelete="RESTRICT",
            name="fk_sys_organization_memberships_organization",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sys_users.id"],
            ondelete="RESTRICT",
            name="fk_sys_organization_memberships_user",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["sys_users.id"],
            ondelete="SET NULL",
            name="fk_sys_organization_memberships_created_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_sys_organization_memberships_org_user",
        ),
    )
    op.create_index(
        "ix_sys_organization_memberships_user_status",
        "sys_organization_memberships",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_sys_organization_memberships_org_status_role",
        "sys_organization_memberships",
        ["organization_id", "status", "role"],
    )

    op.add_column(
        "sys_libraries",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            server_default=DEFAULT_ORGANIZATION_ID,
        ),
    )
    op.create_foreign_key(
        "fk_sys_libraries_organization",
        "sys_libraries",
        "sys_organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        f"""
            UPDATE sys_libraries
            SET organization_id = '{DEFAULT_ORGANIZATION_ID}'::uuid
            WHERE organization_id IS NULL
            """
    )
    op.execute(
        f"""
            INSERT INTO sys_organization_memberships (
                id, organization_id, user_id, role, status, disabled_at
            )
            SELECT
                md5('default-organization-membership:' || u.id::text)::uuid,
                '{DEFAULT_ORGANIZATION_ID}'::uuid,
                u.id,
                CASE WHEN u.is_superuser THEN 'organization_admin' ELSE 'member' END,
                CASE
                    WHEN u.is_active AND u.deleted_at IS NULL THEN 'active'
                    ELSE 'disabled'
                END,
                CASE
                    WHEN u.is_active AND u.deleted_at IS NULL THEN NULL
                    ELSE COALESCE(u.deleted_at, now())
                END
            FROM sys_users AS u
            ON CONFLICT (organization_id, user_id) DO NOTHING
            """
    )
    op.alter_column(
        "sys_libraries",
        "organization_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        existing_server_default=DEFAULT_ORGANIZATION_ID,
    )
    op.create_index(
        "ix_sys_libraries_organization_id",
        "sys_libraries",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sys_libraries_organization_id", table_name="sys_libraries")
    op.drop_constraint(
        "fk_sys_libraries_organization", "sys_libraries", type_="foreignkey"
    )
    op.drop_column("sys_libraries", "organization_id")
    op.drop_table("sys_organization_memberships")
    op.drop_table("sys_organizations")
