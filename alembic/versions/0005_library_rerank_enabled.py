"""add rerank_enabled to sys_libraries (per-library rerank toggle)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-17

库级 rerank 开关；null = 继承全局 RERANK_ENABLED，true/false = 覆盖。
非破坏性：仅新增可空列。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("rerank_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "rerank_enabled")
