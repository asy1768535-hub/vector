"""Library graph extraction build mode.

Revision ID: 0046
Revises: 0045
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "graph_extraction_build_mode",
            sa.String(16),
            nullable=False,
            server_default="standard",
        ),
    )
    op.create_check_constraint(
        "ck_lib_graph_extraction_build_mode",
        "sys_libraries",
        "graph_extraction_build_mode IN ('fast','standard','deep')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_lib_graph_extraction_build_mode",
        "sys_libraries",
        type_="check",
    )
    op.drop_column("sys_libraries", "graph_extraction_build_mode")
