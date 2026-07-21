"""add embed_batch_size to sys_libraries (per-library override)

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17

库级 embedding 单批大小覆盖；null = 用全局 EMBED_BATCH_SIZE。
本地 bge-m3 可放大（如 32），阿里云 compatible-mode 受限（10）时按库下调。
非破坏性：仅新增可空列，不动既有数据。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("embed_batch_size", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "embed_batch_size")
