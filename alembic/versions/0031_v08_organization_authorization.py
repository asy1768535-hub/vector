"""v0.8 Organization-scoped API keys

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.add_column(
        "sys_api_keys",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            server_default=DEFAULT_ORGANIZATION_ID,
        ),
    )
    op.create_foreign_key(
        "fk_sys_api_keys_organization",
        "sys_api_keys",
        "sys_organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        f"""
        UPDATE sys_api_keys
        SET organization_id = '{DEFAULT_ORGANIZATION_ID}'::uuid
        WHERE organization_id IS NULL
        """
    )
    op.alter_column(
        "sys_api_keys",
        "organization_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        existing_server_default=DEFAULT_ORGANIZATION_ID,
    )
    op.create_index(
        "ix_sys_api_keys_organization_user_revoked",
        "sys_api_keys",
        ["organization_id", "user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sys_api_keys_organization_user_revoked",
        table_name="sys_api_keys",
    )
    op.drop_constraint(
        "fk_sys_api_keys_organization",
        "sys_api_keys",
        type_="foreignkey",
    )
    op.drop_column("sys_api_keys", "organization_id")
