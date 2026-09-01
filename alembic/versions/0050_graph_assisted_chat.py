"""Add graph-assisted chat rollout and message evidence.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("graph_assisted_chat_mode", sa.String(length=16), nullable=False, server_default="off"),
    )
    op.create_check_constraint(
        "ck_lib_graph_assisted_chat_mode",
        "sys_libraries",
        "graph_assisted_chat_mode IN ('off','shadow','enabled')",
    )
    op.add_column(
        "chat_messages",
        sa.Column("graph_augmented", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "graph_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "graph_evidence")
    op.drop_column("chat_messages", "graph_augmented")
    op.drop_constraint("ck_lib_graph_assisted_chat_mode", "sys_libraries", type_="check")
    op.drop_column("sys_libraries", "graph_assisted_chat_mode")
