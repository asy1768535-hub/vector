"""Chat 会话历史 + 问答审计模型：会话 / 消息 / 消息来源。

只服务用户端问答留痕，不参与检索链路。删用户→删会话→删消息→删来源（FK cascade）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active','archived')", name="ck_chat_conv_status"),
        Index("ix_chat_conv_user_status_updated", "user_id", "status", "updated_at"),
        Index("ix_chat_conv_library", "library_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="CASCADE"), nullable=False,
    )
    library_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_chat_msg_role"),
        CheckConstraint("status IS NULL OR status IN ('success','failed')", name="ck_chat_msg_status"),
        Index("ix_chat_msg_conv_created", "conversation_id", "created_at"),
        Index("ix_chat_msg_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)             # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # assistant: success|failed
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    graph_augmented: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    graph_evidence: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # assistant 指向它回答的 user 消息（审计页配对问/答）
    parent_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChatMessageSource(Base):
    __tablename__ = "chat_message_sources"
    __table_args__ = (
        Index("ix_chat_msg_src_message", "message_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chunk_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="rrf", server_default="rrf"
    )
    display_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
