"""Graph extraction batch audit membership.

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_raw_output_attempts",
        sa.Column("batch_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "extraction_raw_output_attempts",
        sa.Column("batch_key", sa.String(32), nullable=True),
    )
    op.add_column(
        "extraction_raw_output_attempts",
        sa.Column("batch_ordinal", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_extraction_raw_attempts_batch_membership",
        "extraction_raw_output_attempts",
        "(batch_request_id IS NULL AND batch_key IS NULL AND batch_ordinal IS NULL) OR "
        "(batch_request_id IS NOT NULL AND batch_key IS NOT NULL AND batch_ordinal IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_extraction_raw_attempts_batch_ordinal",
        "extraction_raw_output_attempts",
        "batch_ordinal IS NULL OR batch_ordinal BETWEEN 0 AND 7",
    )
    op.create_unique_constraint(
        "uq_extraction_raw_attempts_batch_key",
        "extraction_raw_output_attempts",
        ["batch_request_id", "batch_key"],
    )
    op.create_index(
        "ix_extraction_raw_attempts_batch_request",
        "extraction_raw_output_attempts",
        ["batch_request_id", "batch_ordinal"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_raw_attempts_batch_request",
        table_name="extraction_raw_output_attempts",
    )
    op.drop_constraint(
        "uq_extraction_raw_attempts_batch_key",
        "extraction_raw_output_attempts",
        type_="unique",
    )
    op.drop_constraint(
        "ck_extraction_raw_attempts_batch_ordinal",
        "extraction_raw_output_attempts",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_raw_attempts_batch_membership",
        "extraction_raw_output_attempts",
        type_="check",
    )
    op.drop_column("extraction_raw_output_attempts", "batch_ordinal")
    op.drop_column("extraction_raw_output_attempts", "batch_key")
    op.drop_column("extraction_raw_output_attempts", "batch_request_id")
