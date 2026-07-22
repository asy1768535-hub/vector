"""v0.8 provider-neutral revision file storage

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_revision_files",
        sa.Column("storage_provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("endpoint_ref", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("bucket", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("object_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("object_version", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("etag", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("immutability_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column(
            "managed_snapshot",
            sa.Boolean(),
            nullable=True,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("source_locator", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "document_revision_files",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        "UPDATE document_revision_files SET "
        "storage_provider = 'local', endpoint_ref = 'primary', "
        "object_key = storage_path, immutability_mode = 'content_hash', "
        "managed_snapshot = true "
        "WHERE storage_provider IS NULL"
    )
    op.execute(
        """
        INSERT INTO document_revision_files (
            id, document_revision_id, document_id, library_id,
            file_name, content_type, storage_path, size_bytes, sha256, created_at,
            storage_provider, endpoint_ref, bucket, object_key, object_version,
            etag, immutability_mode, managed_snapshot, source_locator, verified_at
        )
        SELECT
            md5(df.document_id::text || ':' || d.current_revision_id::text)::uuid,
            d.current_revision_id, df.document_id, d.library_id,
            df.file_name, df.content_type, df.storage_path, df.size_bytes, df.sha256,
            df.created_at, 'local', 'primary', NULL, df.storage_path, NULL,
            NULL, 'content_hash', true, NULL, NULL
        FROM document_files AS df
        JOIN documents AS d ON d.id = df.document_id
        JOIN document_revisions AS dr ON dr.id = d.current_revision_id
        WHERE d.current_revision_id IS NOT NULL
          AND dr.document_id = d.id
          AND dr.library_id = d.library_id
          AND NOT EXISTS (
              SELECT 1 FROM document_revision_files AS existing
              WHERE existing.document_revision_id = d.current_revision_id
          )
        """
    )

    for name in (
        "storage_provider",
        "endpoint_ref",
        "object_key",
        "immutability_mode",
        "managed_snapshot",
    ):
        op.alter_column("document_revision_files", name, nullable=False)
    op.alter_column(
        "document_revision_files", "storage_provider", server_default="local"
    )
    op.alter_column(
        "document_revision_files", "endpoint_ref", server_default="primary"
    )
    op.alter_column(
        "document_revision_files", "immutability_mode", server_default="content_hash"
    )

    op.create_check_constraint(
        "ck_document_revision_files_storage_provider",
        "document_revision_files",
        "storage_provider IN ('local','minio','oss')",
    )
    op.create_check_constraint(
        "ck_document_revision_files_provider_shape",
        "document_revision_files",
        "((storage_provider = 'local' AND bucket IS NULL) OR "
        "(storage_provider IN ('minio','oss') AND bucket IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_document_revision_files_immutability",
        "document_revision_files",
        "((immutability_mode = 'content_hash' AND object_version IS NULL) OR "
        "(immutability_mode = 'version_id' AND object_version IS NOT NULL)) AND "
        "(storage_provider <> 'local' OR immutability_mode = 'content_hash')",
    )
    op.create_check_constraint(
        "ck_document_revision_files_locator_bounds",
        "document_revision_files",
        "length(endpoint_ref) BETWEEN 1 AND 128 AND "
        "length(object_key) BETWEEN 1 AND 2048 AND "
        "(bucket IS NULL OR length(bucket) BETWEEN 1 AND 255) AND "
        "(object_version IS NULL OR length(object_version) BETWEEN 1 AND 512) AND "
        "(etag IS NULL OR length(etag) BETWEEN 1 AND 512)",
    )
    op.create_check_constraint(
        "ck_document_revision_files_file_identity",
        "document_revision_files",
        "size_bytes >= 0 AND sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_document_revision_files_source_locator_object",
        "document_revision_files",
        "source_locator IS NULL OR "
        "(jsonb_typeof(source_locator) = 'object' AND "
        "octet_length(source_locator::text) <= 8192)",
    )
    op.create_check_constraint(
        "ck_document_revision_files_external_ownership",
        "document_revision_files",
        "managed_snapshot OR "
        "(immutability_mode = 'version_id' AND source_locator IS NOT NULL AND "
        "source_locator->>'kind' = 'external_object')",
    )
    op.create_unique_constraint(
        "uq_document_revision_files_revision",
        "document_revision_files",
        ["document_revision_id"],
    )
    op.create_index(
        "ix_document_revision_files_storage_object",
        "document_revision_files",
        ["storage_provider", "endpoint_ref", "bucket", "object_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_revision_files_storage_object",
        table_name="document_revision_files",
    )
    op.drop_constraint(
        "uq_document_revision_files_revision",
        "document_revision_files",
        type_="unique",
    )
    for name in (
        "ck_document_revision_files_external_ownership",
        "ck_document_revision_files_source_locator_object",
        "ck_document_revision_files_file_identity",
        "ck_document_revision_files_locator_bounds",
        "ck_document_revision_files_immutability",
        "ck_document_revision_files_provider_shape",
        "ck_document_revision_files_storage_provider",
    ):
        op.drop_constraint(name, "document_revision_files", type_="check")
    for name in (
        "verified_at",
        "source_locator",
        "managed_snapshot",
        "immutability_mode",
        "etag",
        "object_version",
        "object_key",
        "bucket",
        "endpoint_ref",
        "storage_provider",
    ):
        op.drop_column("document_revision_files", name)
