"""Add per-library Schema organization mode.

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("schema_mode", sa.String(length=16), nullable=False, server_default="disabled"),
    )
    op.execute(
        "UPDATE sys_libraries SET schema_mode = 'governed' "
        "WHERE graph_extraction_enabled = true"
    )
    op.create_check_constraint(
        "ck_lib_schema_mode",
        "sys_libraries",
        "schema_mode IN ('disabled','explore','governed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_lib_schema_mode", "sys_libraries", type_="check")
    op.drop_column("sys_libraries", "schema_mode")
