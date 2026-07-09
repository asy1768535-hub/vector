"""v0.2 evidence foundation m1

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_folders_library_id", "folders", ["library_id"])
    op.create_index("ix_folders_library_path", "folders", ["library_id", "path"])
    op.create_index(
        "uq_folders_library_parent_name_active",
        "folders",
        ["library_id", "parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table("sync_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sync_sources_library_id", "sync_sources", ["library_id"])
    op.create_index("ix_sync_sources_library_status", "sync_sources", ["library_id", "status"])
    op.create_index(
        "uq_sync_sources_library_source_key_active",
        "sync_sources",
        ["library_id", "source_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table("document_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("document_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("normalized_text_storage_path", sa.Text(), nullable=True),
        sa.Column("parser_name", sa.String(length=128), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("parser_config", postgresql.JSONB(), nullable=True),
        sa.Column("chunking_strategy", sa.String(length=128), nullable=False),
        sa.Column("chunking_strategy_version", sa.String(length=64), nullable=False),
        sa.Column("chunking_config", postgresql.JSONB(), nullable=True),
        sa.Column("visibility_scope", sa.String(length=64), nullable=True),
        sa.Column("security_level", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_document_revisions_document_id", "document_revisions", ["document_id"])
    op.create_index("ix_document_revisions_library_id", "document_revisions", ["library_id"])
    op.create_index(
        "ix_document_revisions_document_revision_no",
        "document_revisions",
        ["document_id", "revision_no"],
    )
    op.create_index("ix_document_revisions_library_status", "document_revisions", ["library_id", "status"])
    op.create_index("ix_document_revisions_document_status", "document_revisions", ["document_id", "status"])

    op.create_table("document_revision_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_document_revision_files_library_id", "document_revision_files", ["library_id"])
    op.create_index("ix_document_revision_files_revision", "document_revision_files", ["document_revision_id"])
    op.create_index("ix_document_revision_files_document", "document_revision_files", ["document_id"])

    op.create_table("document_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_block_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("block_kind", sa.String(length=32), nullable=False),
        sa.Column("title_path", postgresql.JSONB(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.Column("position", postgresql.JSONB(), nullable=True),
        sa.Column("parser_name", sa.String(length=128), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_document_blocks_library_id", "document_blocks", ["library_id"])
    op.create_index("ix_document_blocks_document_id", "document_blocks", ["document_id"])
    op.create_index("ix_document_blocks_document_revision_id", "document_blocks", ["document_revision_id"])
    op.create_index("ix_document_blocks_revision_seq", "document_blocks", ["document_revision_id", "seq"])
    op.create_index(
        "ix_document_blocks_library_revision_kind",
        "document_blocks",
        ["library_id", "document_revision_id", "block_kind"],
    )
    op.create_index("ix_document_blocks_parent", "document_blocks", ["parent_block_id"])

    op.create_table("evidence_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_block_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("title_path", postgresql.JSONB(), nullable=True),
        sa.Column("position", postgresql.JSONB(), nullable=True),
        sa.Column("text_quote", sa.Text(), nullable=True),
        sa.Column("text_quote_hash", sa.String(length=64), nullable=True),
        sa.Column("evidence_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("visibility_scope", sa.String(length=64), nullable=True),
        sa.Column("security_level", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evidence_units_library_id", "evidence_units", ["library_id"])
    op.create_index("ix_evidence_units_document_id", "evidence_units", ["document_id"])
    op.create_index("ix_evidence_units_document_revision_id", "evidence_units", ["document_revision_id"])
    op.create_index("ix_evidence_units_revision", "evidence_units", ["document_revision_id"])
    op.create_index("ix_evidence_units_block", "evidence_units", ["document_block_id"])
    op.create_index("ix_evidence_units_library_status", "evidence_units", ["library_id", "status"])

    op.create_table("chunk_blocks",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_block_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
    )
    op.create_index("ix_chunk_blocks_revision", "chunk_blocks", ["document_revision_id"])
    op.create_index("ix_chunk_blocks_block", "chunk_blocks", ["document_block_id"])

    op.create_table("chunk_evidence",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
    )
    op.create_index("ix_chunk_evidence_revision", "chunk_evidence", ["document_revision_id"])
    op.create_index("ix_chunk_evidence_evidence", "chunk_evidence", ["evidence_id"])

    op.create_table("migration_backfill_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("processed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_migration_backfill_state_task_library",
        "migration_backfill_state",
        ["task_name", "library_id"],
        unique=True,
    )
    op.create_index("ix_migration_backfill_state_status", "migration_backfill_state", ["status"])

    op.add_column("documents", sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("sync_source_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("display_name", sa.String(length=512), nullable=True))
    op.add_column("documents", sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("latest_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("visibility_scope", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("security_level", sa.String(length=64), nullable=True))
    op.alter_column("documents", "external_id", type_=sa.String(length=512), existing_nullable=True)
    op.drop_index("uq_documents_library_external_active", table_name="documents")
    op.drop_index("uq_documents_library_hash_active", table_name="documents")
    op.create_index(
        "uq_documents_library_external_no_source_active",
        "documents",
        ["library_id", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "sync_source_id IS NULL AND external_id IS NOT NULL AND deleted_at IS NULL"
        ),
    )
    op.create_index(
        "uq_documents_library_sync_external_active",
        "documents",
        ["library_id", "sync_source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "sync_source_id IS NOT NULL AND external_id IS NOT NULL AND deleted_at IS NULL"
        ),
    )
    op.create_index(
        "uq_documents_library_hash_active",
        "documents",
        ["library_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text(
            "sync_source_id IS NULL AND external_id IS NULL AND deleted_at IS NULL"
        ),
    )
    op.create_index("ix_documents_folder", "documents", ["folder_id"])
    op.create_index("ix_documents_current_revision_id", "documents", ["current_revision_id"])
    op.create_index("ix_documents_latest_revision_id", "documents", ["latest_revision_id"])

    op.add_column("chunks", sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("chunks", sa.Column("block_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("chunks", sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("chunks", sa.Column("chunk_kind", sa.String(length=32), nullable=True))
    op.add_column("chunks", sa.Column("page_start", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("page_end", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("title_path", postgresql.JSONB(), nullable=True))
    op.add_column("chunks", sa.Column("source_start", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("source_end", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("position", postgresql.JSONB(), nullable=True))
    op.add_column(
        "chunks",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("ix_chunks_revision_seq", "chunks", ["document_revision_id", "seq"])
    op.create_index("ix_chunks_evidence_id", "chunks", ["evidence_id"])
    op.create_index("ix_chunks_block_id", "chunks", ["block_id"])

    op.add_column("embedding_jobs", sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("embedding_jobs", sa.Column("document_revision_no", sa.Integer(), nullable=True))
    op.create_index("ix_embedding_jobs_revision_id", "embedding_jobs", ["document_revision_id"])


def downgrade() -> None:
    op.drop_index("ix_embedding_jobs_revision_id", table_name="embedding_jobs")
    op.drop_column("embedding_jobs", "document_revision_no")
    op.drop_column("embedding_jobs", "document_revision_id")

    op.drop_index("ix_chunks_block_id", table_name="chunks")
    op.drop_index("ix_chunks_evidence_id", table_name="chunks")
    op.drop_index("ix_chunks_revision_seq", table_name="chunks")
    op.drop_column("chunks", "created_at")
    op.drop_column("chunks", "position")
    op.drop_column("chunks", "source_end")
    op.drop_column("chunks", "source_start")
    op.drop_column("chunks", "title_path")
    op.drop_column("chunks", "page_end")
    op.drop_column("chunks", "page_start")
    op.drop_column("chunks", "chunk_kind")
    op.drop_column("chunks", "evidence_id")
    op.drop_column("chunks", "block_id")
    op.drop_column("chunks", "document_revision_id")

    op.drop_index("ix_documents_latest_revision_id", table_name="documents")
    op.drop_index("ix_documents_current_revision_id", table_name="documents")
    op.drop_index("ix_documents_folder", table_name="documents")
    op.drop_index("uq_documents_library_hash_active", table_name="documents")
    op.drop_index("uq_documents_library_sync_external_active", table_name="documents")
    op.drop_index("uq_documents_library_external_no_source_active", table_name="documents")
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
    op.alter_column("documents", "external_id", type_=sa.String(length=255), existing_nullable=True)
    op.drop_column("documents", "security_level")
    op.drop_column("documents", "visibility_scope")
    op.drop_column("documents", "latest_revision_id")
    op.drop_column("documents", "current_revision_id")
    op.drop_column("documents", "display_name")
    op.drop_column("documents", "sync_source_id")
    op.drop_column("documents", "folder_id")

    op.drop_index("ix_migration_backfill_state_status", table_name="migration_backfill_state")
    op.drop_index("uq_migration_backfill_state_task_library", table_name="migration_backfill_state")
    op.drop_table("migration_backfill_state")

    op.drop_index("ix_chunk_evidence_evidence", table_name="chunk_evidence")
    op.drop_index("ix_chunk_evidence_revision", table_name="chunk_evidence")
    op.drop_table("chunk_evidence")

    op.drop_index("ix_chunk_blocks_block", table_name="chunk_blocks")
    op.drop_index("ix_chunk_blocks_revision", table_name="chunk_blocks")
    op.drop_table("chunk_blocks")

    op.drop_index("ix_evidence_units_library_status", table_name="evidence_units")
    op.drop_index("ix_evidence_units_block", table_name="evidence_units")
    op.drop_index("ix_evidence_units_revision", table_name="evidence_units")
    op.drop_index("ix_evidence_units_document_revision_id", table_name="evidence_units")
    op.drop_index("ix_evidence_units_document_id", table_name="evidence_units")
    op.drop_index("ix_evidence_units_library_id", table_name="evidence_units")
    op.drop_table("evidence_units")

    op.drop_index("ix_document_blocks_parent", table_name="document_blocks")
    op.drop_index("ix_document_blocks_library_revision_kind", table_name="document_blocks")
    op.drop_index("ix_document_blocks_revision_seq", table_name="document_blocks")
    op.drop_index("ix_document_blocks_document_revision_id", table_name="document_blocks")
    op.drop_index("ix_document_blocks_document_id", table_name="document_blocks")
    op.drop_index("ix_document_blocks_library_id", table_name="document_blocks")
    op.drop_table("document_blocks")

    op.drop_index("ix_document_revision_files_document", table_name="document_revision_files")
    op.drop_index("ix_document_revision_files_revision", table_name="document_revision_files")
    op.drop_index("ix_document_revision_files_library_id", table_name="document_revision_files")
    op.drop_table("document_revision_files")

    op.drop_index("ix_document_revisions_document_status", table_name="document_revisions")
    op.drop_index("ix_document_revisions_library_status", table_name="document_revisions")
    op.drop_index("ix_document_revisions_document_revision_no", table_name="document_revisions")
    op.drop_index("ix_document_revisions_library_id", table_name="document_revisions")
    op.drop_index("ix_document_revisions_document_id", table_name="document_revisions")
    op.drop_table("document_revisions")

    op.drop_index("uq_sync_sources_library_source_key_active", table_name="sync_sources")
    op.drop_index("ix_sync_sources_library_status", table_name="sync_sources")
    op.drop_index("ix_sync_sources_library_id", table_name="sync_sources")
    op.drop_table("sync_sources")

    op.drop_index("uq_folders_library_parent_name_active", table_name="folders")
    op.drop_index("ix_folders_library_path", table_name="folders")
    op.drop_index("ix_folders_library_id", table_name="folders")
    op.drop_table("folders")
