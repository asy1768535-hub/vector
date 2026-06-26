"""chat 会话历史 + 问答审计：chat_conversations / chat_messages / chat_message_sources

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-26

用户端轻量问答的会话与逐条问答留痕。不参与任何检索链路；删用户/删会话级联清理。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("library_slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_chat_conv_status"),
    )
    op.create_index("ix_chat_conv_user_status_updated", "chat_conversations",
                    ["user_id", "status", "updated_at"])
    op.create_index("ix_chat_conv_library", "chat_conversations", ["library_slug"])

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("rewritten_query", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),          # assistant: success/failed
        sa.Column("error_message", sa.Text(), nullable=True),
        # assistant 消息指向它回答的那条 user 消息（便于审计页配对问/答）
        sa.Column(
            "parent_message_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_chat_msg_role"),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('success','failed')", name="ck_chat_msg_status",
        ),
    )
    op.create_index("ix_chat_msg_conv_created", "chat_messages", ["conversation_id", "created_at"])
    op.create_index("ix_chat_msg_status_created", "chat_messages", ["status", "created_at"])

    op.create_table(
        "chat_message_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("document_id", sa.String(length=128), nullable=True),
        sa.Column("chunk_id", sa.String(length=128), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_msg_src_message", "chat_message_sources", ["message_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_chat_msg_src_message", table_name="chat_message_sources")
    op.drop_table("chat_message_sources")
    op.drop_index("ix_chat_msg_status_created", table_name="chat_messages")
    op.drop_index("ix_chat_msg_conv_created", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conv_library", table_name="chat_conversations")
    op.drop_index("ix_chat_conv_user_status_updated", table_name="chat_conversations")
    op.drop_table("chat_conversations")
