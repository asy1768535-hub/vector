"""add ocr_enabled to sys_libraries (per-library image OCR toggle)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-18

库级图片 OCR 开关；null = 继承全局 OCR_ENABLED（默认 false），true/false = 覆盖。
非破坏性：仅新增可空列。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("ocr_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "ocr_enabled")
