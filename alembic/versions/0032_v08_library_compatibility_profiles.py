"""v0.8 Library embedding verification snapshots

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SNAPSHOT_COLUMNS = (
    "embedding_probe_contract_version",
    "embedding_probe_model",
    "embedding_probe_dimension",
    "embedding_probe_endpoint_sha256",
    "embedding_probe_fingerprint",
    "embedding_probe_verified_at",
)


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_contract_version", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_model", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_dimension", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_endpoint_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("embedding_probe_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_sys_libraries_embedding_probe_snapshot",
        "sys_libraries",
        "(num_nonnulls("
        + ", ".join(_SNAPSHOT_COLUMNS)
        + ") = 0 OR (num_nonnulls("
        + ", ".join(_SNAPSHOT_COLUMNS)
        + ") = 6 "
        "AND embedding_probe_dimension > 0 "
        "AND embedding_probe_endpoint_sha256 ~ '^[0-9a-f]{64}$' "
        "AND embedding_probe_fingerprint ~ '^[0-9a-f]{64}$'))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sys_libraries_embedding_probe_snapshot",
        "sys_libraries",
        type_="check",
    )
    for column_name in reversed(_SNAPSHOT_COLUMNS):
        op.drop_column("sys_libraries", column_name)
