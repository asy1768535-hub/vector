"""Large-file import jobs and path-based folder identity.

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_path", sa.Text(), nullable=True))
    op.drop_index("uq_documents_library_hash_active", table_name="documents")
    op.create_index(
        "uq_documents_library_hash_active",
        "documents",
        ["library_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text(
            "sync_source_id IS NULL AND external_id IS NULL "
            "AND source_path IS NULL AND deleted_at IS NULL"
        ),
    )
    op.create_index(
        "uq_documents_library_source_path_active",
        "documents",
        ["library_id", "source_path"],
        unique=True,
        postgresql_where=sa.text(
            "source_path IS NOT NULL AND deleted_at IS NULL"
        ),
    )

    op.create_table(
        "document_import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("upload_offset", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_modified_millis", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("staging_key", sa.String(512), nullable=False, unique=True),
        sa.Column("external_id", sa.String(512), nullable=True),
        sa.Column(
            "replace_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("security_level", sa.String(64), nullable=True),
        sa.Column(
            "graph_extraction_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploading"),
        sa.Column(
            "current_stage",
            sa.String(32),
            nullable=False,
            server_default="uploading",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(160), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upload_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result_operation", sa.String(32), nullable=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "document_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "embedding_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("embedding_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN "
            "('uploading','queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_document_import_jobs_status",
        ),
        sa.CheckConstraint(
            "current_stage IN "
            "('uploading','queued','validating','parsing','chunking','embedding','graph','completed')",
            name="ck_document_import_jobs_stage",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND upload_offset >= 0 AND upload_offset <= size_bytes",
            name="ck_document_import_jobs_offsets",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_document_import_jobs_attempts",
        ),
    )
    op.create_index(
        "ix_document_import_jobs_claimable",
        "document_import_jobs",
        ["status", "claimed_at", "created_at"],
    )
    op.create_index(
        "ix_document_import_jobs_library_created",
        "document_import_jobs",
        ["library_id", "created_at"],
    )
    op.create_index(
        "ix_document_import_jobs_batch",
        "document_import_jobs",
        ["library_id", "batch_id"],
    )
    op.create_index(
        "ix_document_import_jobs_resume",
        "document_import_jobs",
        ["library_id", "batch_id", "relative_path", "file_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_import_jobs_resume", table_name="document_import_jobs")
    op.drop_index("ix_document_import_jobs_batch", table_name="document_import_jobs")
    op.drop_index(
        "ix_document_import_jobs_library_created",
        table_name="document_import_jobs",
    )
    op.drop_index(
        "ix_document_import_jobs_claimable",
        table_name="document_import_jobs",
    )
    op.drop_table("document_import_jobs")

    op.drop_index(
        "uq_documents_library_source_path_active",
        table_name="documents",
    )
    op.drop_index("uq_documents_library_hash_active", table_name="documents")
    op.create_index(
        "uq_documents_library_hash_active",
        "documents",
        ["library_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text(
            "sync_source_id IS NULL AND external_id IS NULL AND deleted_at IS NULL"
        ),
    )
    op.drop_column("documents", "source_path")
