"""add embedding_base_url to sys_libraries (per-library override)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-20

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_base_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sys_libraries", "embedding_base_url")
