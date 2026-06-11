"""initial schema: users, api_keys, libraries, documents, chunks, embedding_jobs, audit_log, casbin_rule

Revision ID: 0001
Revises:
Create Date: 2026-05-19

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 启用 pgcrypto / uuid-ossp 不强制（id 由应用层 default uuid4 产生）
    # ── sys_users ─────────────────────────────────────────────────────
    op.create_table(
        "sys_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True, index=True),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("username", sa.String(length=64), unique=True, nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── sys_api_keys ──────────────────────────────────────────────────
    op.create_table(
        "sys_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False, index=True),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_sys_api_keys_active_user",
        "sys_api_keys",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ── sys_libraries ─────────────────────────────────────────────────
    op.create_table(
        "sys_libraries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=80), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=80), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("vector_distance", sa.String(length=16), nullable=False, server_default="cosine"),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("qdrant_collection", sa.String(length=128), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── documents ─────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_documents_library_hash", "documents", ["library_id", "content_hash"])
    op.create_index("ix_documents_library_external", "documents", ["library_id", "external_id"])

    # ── chunks ────────────────────────────────────────────────────────
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_chunks_lib_doc_seq", "chunks", ["library_id", "document_id", "seq"])

    # ── embedding_jobs ────────────────────────────────────────────────
    op.create_table(
        "embedding_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_embedding_jobs_status_lib",
        "embedding_jobs",
        ["status", "library_id"],
        postgresql_where=sa.text("status IN ('pending','processing')"),
    )

    # ── sys_audit_log ─────────────────────────────────────────────────
    op.create_table(
        "sys_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("action", sa.String(length=64), nullable=False, index=True),
        sa.Column("target", postgresql.JSONB(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── casbin_rule ────────────────────────────────────────────────────
    # casbin-sqlalchemy-adapter 自己也会在 Enforcer 初始化时建这张表。
    # 用 IF NOT EXISTS 兼容两种情况：alembic 先跑（我们建）/ uvicorn 先跑（adapter 已建）。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS casbin_rule (
            id SERIAL PRIMARY KEY,
            ptype VARCHAR(255),
            v0 VARCHAR(255),
            v1 VARCHAR(255),
            v2 VARCHAR(255),
            v3 VARCHAR(255),
            v4 VARCHAR(255),
            v5 VARCHAR(255)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_casbin_rule_ptype_v0 ON casbin_rule (ptype, v0)"
    )


def downgrade() -> None:
    op.drop_table("casbin_rule")
    op.drop_table("sys_audit_log")
    op.drop_table("embedding_jobs")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("sys_libraries")
    op.drop_table("sys_api_keys")
    op.drop_table("sys_users")
