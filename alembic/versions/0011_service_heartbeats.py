"""service_heartbeats：进程级心跳（运行状态监控，docs/26 / 批次 C2）

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-24

纯旁路表：无外键指向业务表，也无业务表指向它；回滚即整表删除，零风险。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_type", sa.String(length=32), nullable=False),
        sa.Column("instance_id", sa.String(length=160), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="online"),
        # DB 列名 metadata；ORM 属性名 heartbeat_metadata（避开 Declarative 保留名）
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "service_type IN ('api','embedding_worker','cleanup_worker')",
            name="ck_heartbeat_service_type",
        ),
        sa.CheckConstraint("status IN ('online','stopping')", name="ck_heartbeat_status"),
        sa.UniqueConstraint("service_type", "instance_id", name="uq_heartbeat_service_instance"),
    )
    op.create_index(
        "ix_heartbeat_service_lastseen", "service_heartbeats", ["service_type", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_heartbeat_service_lastseen", table_name="service_heartbeats")
    op.drop_table("service_heartbeats")
