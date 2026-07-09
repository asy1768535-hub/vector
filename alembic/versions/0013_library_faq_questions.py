"""library_faq_questions：知识库「常用问题」（第一版）

Revision ID: 0013
Revises: 0011
Create Date: 2026-06-26

每个库可维护一组高频问题，普通用户在检索测试页一键发起检索。纯旁路功能表：
只被 sys_libraries 引用（FK ondelete cascade，删库随删），不参与任何检索链路。
回滚即整表删除，零风险。

注：0012（hybrid_retrieval）方案已回退、未落地，故 0013 直接续在 0011 之后。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "library_faq_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # question 去掉首尾空白后不能为空（schema 层也校验，这里兜底防脏写）
        sa.CheckConstraint("char_length(btrim(question)) > 0", name="ck_faq_question_not_blank"),
    )
    op.create_index(
        "ix_library_faq_questions_library_id", "library_faq_questions", ["library_id"],
    )
    op.create_index(
        "ix_library_faq_questions_library_active_sort", "library_faq_questions",
        ["library_id", "is_active", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_library_faq_questions_library_active_sort", table_name="library_faq_questions")
    op.drop_index("ix_library_faq_questions_library_id", table_name="library_faq_questions")
    op.drop_table("library_faq_questions")
