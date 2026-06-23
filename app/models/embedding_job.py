"""embedding_jobs：异步任务队列。worker 用 FOR UPDATE SKIP LOCKED 抢锁。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"
    __table_args__ = (
        Index(
            "ix_embedding_jobs_status_lib",
            "status",
            "library_id",
            postgresql_where="status IN ('pending','processing')",
        ),
        # 活动任务唯一：防并发更新对同一 (document, revision) 造出多条活动 job（#6 §4.2）
        Index(
            "uq_jobs_doc_rev_active", "document_id", "document_revision",
            unique=True, postgresql_where=text("status IN ('pending','processing')"),
        ),
        # 同 operation 内每篇文档至多一条 job：finalize 靠 done 计数，重复 job 会让计数提前满足（#6 §4.2）
        Index(
            "uq_jobs_op_doc", "rebuild_operation_id", "document_id",
            unique=True, postgresql_where=text("rebuild_operation_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # 建 job 时快照 documents.current_revision（#6）。迁移已去 DB 默认（与 0009 一致）→ 应用层必须显式赋值。
    document_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 非空=本 job 由某次 rebuild operation 创建；ON DELETE RESTRICT 禁止删 operation 把旧 job 变普通 job。
    rebuild_operation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rebuild_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # pending | processing | done | failed | superseded
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    worker_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
