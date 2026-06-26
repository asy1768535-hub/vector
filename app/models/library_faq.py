"""library_faq_questions：知识库「常用问题」（第一版）。

管理员维护一组高频问题；普通用户在检索测试页一键点击发起检索。只改善提问体验，
不参与任何检索链路。删库时随 FK ondelete cascade 一并清理。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LibraryFAQQuestion(Base):
    __tablename__ = "library_faq_questions"
    __table_args__ = (
        # 去掉首尾空白后不能为空（schema 层也校验，这里 DB 兜底）
        CheckConstraint("char_length(btrim(question)) > 0", name="ck_faq_question_not_blank"),
        Index("ix_library_faq_questions_library_id", "library_id"),
        # 列表查询主路径：按库取 active、按 sort_order 排
        Index("ix_library_faq_questions_library_active_sort", "library_id", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=func.true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
