"""qdrant_cleanup_outbox：删除/重建后 Qdrant 物理清理的事务性 outbox（#7 设计 §4.4 / §8）。

删除/更新与本表行在同一事务写入 → PG 提交即逻辑生效；Qdrant 物理清理由独立 Cleanup Worker
幂等执行、失败指数退避重试。事件类型见下。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint, DateTime, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# 事件类型
EVENT_DELETE_DOCUMENT_ALL = "delete_document_all"                 # 删文档：删该 document_id 全部 points
EVENT_DELETE_DOCUMENT_BEFORE_REVISION = "delete_document_before_revision"  # 更新：删 revision 缺失或 < target
EVENT_DELETE_COLLECTION = "delete_collection"                     # 删库：删整个 collection


class CleanupOutbox(Base):
    __tablename__ = "qdrant_cleanup_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','done','failed')", name="ck_cleanup_status"
        ),
        Index(
            "ix_cleanup_claim", "status", "available_at",
            postgresql_where="status IN ('pending','processing')",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # 幂等键：同一逻辑清理只入队一次（重复删除/更新不产生重复任务）
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
