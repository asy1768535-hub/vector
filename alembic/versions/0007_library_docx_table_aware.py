"""add docx_table_aware to sys_libraries (per-library docx table-aware chunking toggle)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-18

库级 docx 表格感知切块开关；null = 继承全局 DOCX_TABLE_AWARE（默认 false），true/false = 覆盖。
非破坏性：仅新增可空列。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("docx_table_aware", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "docx_table_aware")
