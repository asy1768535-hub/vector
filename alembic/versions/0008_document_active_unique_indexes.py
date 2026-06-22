"""documents 活动行部分唯一索引（#14/#15 文档身份）

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-22

把原先的两个非唯一索引换成「只约束未删行」的部分唯一索引：
  - (library_id, external_id) WHERE external_id IS NOT NULL AND deleted_at IS NULL
  - (library_id, content_hash) WHERE external_id IS NULL AND deleted_at IS NULL

前提：若库内已存在违反唯一性的历史活动行（同 external_id 或同 content_hash 的多条未删
文档），CREATE UNIQUE INDEX 会失败，需先人工清理重复行再升级。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 删旧的非唯一索引（被下面的部分唯一索引取代，后者同样能服务活动行查询）
    op.drop_index("ix_documents_library_hash", table_name="documents")
    op.drop_index("ix_documents_library_external", table_name="documents")

    op.create_index(
        "uq_documents_library_external_active",
        "documents",
        ["library_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_documents_library_hash_active",
        "documents",
        ["library_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("external_id IS NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_documents_library_hash_active", table_name="documents")
    op.drop_index("uq_documents_library_external_active", table_name="documents")
    op.create_index("ix_documents_library_external", "documents", ["library_id", "external_id"])
    op.create_index("ix_documents_library_hash", "documents", ["library_id", "content_hash"])


