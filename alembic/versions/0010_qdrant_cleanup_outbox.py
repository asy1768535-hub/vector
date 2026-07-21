"""qdrant_cleanup_outbox：删除/重建后 Qdrant 物理清理的事务性 outbox（#7 批次 B）

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qdrant_cleanup_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("collection_name", sa.String(length=128), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_cleanup_idempotency_key"),
        sa.CheckConstraint("status IN ('pending','processing','done','failed')", name="ck_cleanup_status"),
    )
    op.create_index(
        "ix_cleanup_claim", "qdrant_cleanup_outbox", ["status", "available_at"],
        postgresql_where=sa.text("status IN ('pending','processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_cleanup_claim", table_name="qdrant_cleanup_outbox")
    op.drop_table("qdrant_cleanup_outbox")
