"""add source_config (JSONB) to sys_libraries (cross-DB enrichment)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-09

当 Qdrant payload 只存外键、正文在另一个业务库时，检索后按外键回查源库
把正文拼回。该列存补全配置；null = 不补全。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("source_config", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "source_config")
