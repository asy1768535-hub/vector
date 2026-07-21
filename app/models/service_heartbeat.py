"""service_heartbeats：进程级心跳（运行状态监控，docs/26）。

API / Embedding Worker / Cleanup Worker 各自每 ~15s upsert 一行；状态接口据 last_seen_at
龄判定 online/offline。本表为纯旁路：无外键指向业务表，也无业务表指向它。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"
    __table_args__ = (
        CheckConstraint(
            "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor')",
            name="ck_heartbeat_service_type",
        ),
        CheckConstraint("status IN ('online','stopping')", name="ck_heartbeat_status"),
        # 同类进程不同 instance_id 各占一行；同一实例 upsert 命中同一行。
        UniqueConstraint("service_type", "instance_id", name="uq_heartbeat_service_instance"),
        # 按类型取最新 / 过期扫描。
        Index("ix_heartbeat_service_lastseen", "service_type", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 约定 = f"{hostname}-{pid}-{uuid4().hex[:8]}"（随机后缀防 PID 复用覆盖旧实例）
    instance_id: Mapped[str] = mapped_column(String(160), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本进程真实启动时间（仅首次 INSERT 写，后续心跳不改）
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 自报生命周期：'online'（正常心跳）/ 'stopping'（优雅关闭写一次）。在线/离线以 last_seen_at 龄为准。
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="online")
    # 注意：DB 列名是 metadata，但 ORM 属性必须叫 heartbeat_metadata——
    # 'metadata' 是 SQLAlchemy Declarative 保留名（Base.metadata），直接用会映射报错。
    heartbeat_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
