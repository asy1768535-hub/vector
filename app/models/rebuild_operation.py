"""rebuild_operations：持久化、可重试、分三阶段的 collection 重建记录（#6 设计 §4.6）。

prepare / qdrant / activate 三阶段；finalize 靠 done job 数 == expected_job_count 判定。
不存 target_revisions JSONB（大库爆行）——以 jobs（带 rebuild_operation_id + document_revision）作快照。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RebuildOperation(Base):
    __tablename__ = "rebuild_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('preparing','running','done','failed')", name="ck_rebuild_op_status"
        ),
        # 每个库至多一个进行中 operation（preparing/running）
        Index(
            "uq_rebuild_op_active_per_lib", "library_id",
            unique=True, postgresql_where=text("status IN ('preparing','running')"),
        ),
        Index("ix_rebuild_op_lib_status", "library_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="preparing")
    expected_job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
