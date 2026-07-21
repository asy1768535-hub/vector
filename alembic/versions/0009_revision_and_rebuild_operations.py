"""revision 索引版本 + rebuild operations + 库生命周期（#6 批次 A）

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-22

按循环外键顺序：先建 rebuild_operations（不含回指 FK）→ 加 embedding_jobs/documents/sys_libraries 列与约束
→ 最后 ALTER 补 sys_libraries.active_rebuild_operation_id 的 FK。
回填后去掉 embedding_jobs.document_revision 的 DB 默认值（决策 2：强制应用层显式赋值）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. documents.current_revision
    op.add_column(
        "documents",
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint("ck_documents_current_revision", "documents", "current_revision >= 1")

    # 2. embedding_jobs.document_revision（先带默认加列+回填，再去默认）
    op.add_column(
        "embedding_jobs",
        sa.Column("document_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    # 回填存量为 1（与「payload 缺 revision 视作 1」对齐）
    op.execute("UPDATE embedding_jobs SET document_revision = 1 WHERE document_revision IS NULL")
    # 去掉 DB 默认（决策 2）：之后由应用层显式赋值
    op.alter_column("embedding_jobs", "document_revision", server_default=None)

    # 3. 先建 rebuild_operations（不含回指 FK；library_id FK 指向已存在的 sys_libraries）
    op.create_table(
        "rebuild_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="preparing"),
        sa.Column("expected_job_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('preparing','running','done','failed')", name="ck_rebuild_op_status"
        ),
    )
    op.create_index(
        "uq_rebuild_op_active_per_lib", "rebuild_operations", ["library_id"],
        unique=True, postgresql_where=sa.text("status IN ('preparing','running')"),
    )
    op.create_index("ix_rebuild_op_lib_status", "rebuild_operations", ["library_id", "status"])

    # 4. embedding_jobs.rebuild_operation_id + FK(ON DELETE RESTRICT)
    op.add_column(
        "embedding_jobs",
        sa.Column("rebuild_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_jobs_rebuild_op", "embedding_jobs", "rebuild_operations",
        ["rebuild_operation_id"], ["id"], ondelete="RESTRICT",
    )
    # 5. 活动任务唯一索引（防并发更新造重复活动 job）
    op.create_index(
        "uq_jobs_doc_rev_active", "embedding_jobs", ["document_id", "document_revision"],
        unique=True, postgresql_where=sa.text("status IN ('pending','processing')"),
    )
    # 5b. 同 operation 文档快照唯一索引
    op.create_index(
        "uq_jobs_op_doc", "embedding_jobs", ["rebuild_operation_id", "document_id"],
        unique=True, postgresql_where=sa.text("rebuild_operation_id IS NOT NULL"),
    )

    # 6/7. sys_libraries.lifecycle_mode / index_state（CHECK）
    op.add_column(
        "sys_libraries",
        sa.Column("lifecycle_mode", sa.String(length=16), nullable=False, server_default="managed"),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("index_state", sa.String(length=16), nullable=False, server_default="ready"),
    )
    op.create_check_constraint(
        "ck_lib_lifecycle_mode", "sys_libraries", "lifecycle_mode IN ('managed','external')"
    )
    op.create_check_constraint(
        "ck_lib_index_state", "sys_libraries", "index_state IN ('ready','rebuilding','failed')"
    )

    # 8. sys_libraries.active_rebuild_operation_id + 最后补回指 FK（解开循环）
    op.add_column(
        "sys_libraries",
        sa.Column("active_rebuild_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_lib_active_rebuild_op", "sys_libraries", "rebuild_operations",
        ["active_rebuild_operation_id"], ["id"],
    )


def downgrade() -> None:
    # 逆序：先 drop sys_libraries 回指 FK → 列/索引 → rebuild_operations 表
    op.drop_constraint("fk_lib_active_rebuild_op", "sys_libraries", type_="foreignkey")
    op.drop_column("sys_libraries", "active_rebuild_operation_id")
    op.drop_constraint("ck_lib_index_state", "sys_libraries", type_="check")
    op.drop_constraint("ck_lib_lifecycle_mode", "sys_libraries", type_="check")
    op.drop_column("sys_libraries", "index_state")
    op.drop_column("sys_libraries", "lifecycle_mode")

    op.drop_index("uq_jobs_op_doc", table_name="embedding_jobs")
    op.drop_index("uq_jobs_doc_rev_active", table_name="embedding_jobs")
    op.drop_constraint("fk_jobs_rebuild_op", "embedding_jobs", type_="foreignkey")
    op.drop_column("embedding_jobs", "rebuild_operation_id")
    op.drop_column("embedding_jobs", "document_revision")

    op.drop_index("ix_rebuild_op_lib_status", table_name="rebuild_operations")
    op.drop_index("uq_rebuild_op_active_per_lib", table_name="rebuild_operations")
    op.drop_table("rebuild_operations")

    op.drop_constraint("ck_documents_current_revision", "documents", type_="check")
    op.drop_column("documents", "current_revision")
