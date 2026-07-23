"""v0.9 supported deployment rollout controls

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_capability_rollouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(48), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "capability IN ('public_api_v1','mcp_adapter','external_graph_sync')",
            name="ck_org_capability_rollouts_capability",
        ),
        sa.CheckConstraint("version >= 1", name="ck_org_capability_rollouts_version"),
        sa.ForeignKeyConstraint(["organization_id"], ["sys_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["changed_by_user_id"], ["sys_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "capability",
            name="uq_org_capability_rollouts_org_capability",
        ),
    )
    op.create_index(
        "ix_org_capability_rollouts_enabled",
        "organization_capability_rollouts",
        ["capability", "enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_org_capability_rollouts_enabled",
        table_name="organization_capability_rollouts",
    )
    op.drop_table("organization_capability_rollouts")

