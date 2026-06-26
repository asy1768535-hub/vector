"""轻量 Hybrid Search 第一版：retrieval_mode + 关键词索引（pg_trgm，权限不足回退）

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-26

不引入 ES、不新增外部服务。为 hybrid 检索准备：
  - sys_libraries.retrieval_mode（dense/hybrid，默认 dense）；
  - chunks.text / documents.title / documents.external_id 的关键词索引（pg_trgm GIN，
    无权限启用 pg_trgm 时回退 lower btree + 运行期 ILIKE，并打印明确指引）。

幂等说明：旧 0012（已回退）的残留 `retrieval_mode` 列/约束可能已存在 → 本迁移按存在性跳过，
安全「收编」残留，不会因重复添加而失败。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) retrieval_mode 列 + CHECK（0012 残留库可能已有 → 幂等）
    has_col = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='sys_libraries' AND column_name='retrieval_mode'"
    )).scalar()
    if not has_col:
        op.add_column(
            "sys_libraries",
            sa.Column("retrieval_mode", sa.String(length=16), nullable=False, server_default="dense"),
        )
    has_ck = conn.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname='ck_lib_retrieval_mode'"
    )).scalar()
    if not has_ck:
        op.create_check_constraint(
            "ck_lib_retrieval_mode", "sys_libraries", "retrieval_mode IN ('dense','hybrid')",
        )

    # 2) pg_trgm（可选）：用 SAVEPOINT 尝试启用，权限不足则回退、不让整条迁移失败
    has_trgm = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'")).scalar() is not None
    if not has_trgm:
        try:
            with conn.begin_nested():          # 失败只回滚到 savepoint，外层事务保持可用
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            has_trgm = True
        except Exception as exc:               # noqa: BLE001 - 权限不足等都视为「不可用」并回退
            print(
                f"[0015] WARNING: 无法启用 pg_trgm（{exc}）。"
                " 关键词检索将回退到 lower-btree + 运行期 ILIKE（仍可用，仅更慢）。"
                " 如需 trigram 加速，请用具备权限的角色执行：CREATE EXTENSION pg_trgm;"
            )
            has_trgm = False

    # 3) 关键词索引
    if has_trgm:
        op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_documents_title_trgm ON documents USING gin (title gin_trgm_ops)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_documents_external_id_trgm "
            "ON documents USING gin (external_id gin_trgm_ops)"
        )
    else:
        # 回退：title/external_id 用 lower btree 辅助 ILIKE 前缀；chunks.text 不建（ILIKE 顺扫，小库可接受）
        op.execute("CREATE INDEX IF NOT EXISTS ix_documents_title_lower ON documents (lower(title))")
        op.execute("CREATE INDEX IF NOT EXISTS ix_documents_external_id_lower ON documents (lower(external_id))")


def downgrade() -> None:
    for idx in (
        "ix_chunks_text_trgm", "ix_documents_title_trgm", "ix_documents_external_id_trgm",
        "ix_documents_title_lower", "ix_documents_external_id_lower",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    # 注意：不 DROP EXTENSION pg_trgm（可能被其它对象/库使用）
    op.execute("ALTER TABLE sys_libraries DROP CONSTRAINT IF EXISTS ck_lib_retrieval_mode")
    op.execute("ALTER TABLE sys_libraries DROP COLUMN IF EXISTS retrieval_mode")
