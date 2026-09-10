"""Add durable original-file resources and bind import jobs to them."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_resources",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column(
            "storage_provider",
            sa.String(length=32),
            server_default="local",
            nullable=False,
        ),
        sa.Column(
            "endpoint_ref",
            sa.String(length=128),
            server_default="primary",
            nullable=False,
        ),
        sa.Column("bucket", sa.String(length=255), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("object_version", sa.String(length=512), nullable=True),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column(
            "immutability_mode",
            sa.String(length=32),
            server_default="content_hash",
            nullable=False,
        ),
        sa.Column(
            "storage_status",
            sa.String(length=32),
            server_default="available",
            nullable=False,
        ),
        sa.Column("storage_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "storage_provider IN ('local','minio','oss')",
            name="ck_file_resources_storage_provider",
        ),
        sa.CheckConstraint(
            "((storage_provider = 'local' AND bucket IS NULL) OR "
            "(storage_provider IN ('minio','oss') AND bucket IS NOT NULL))",
            name="ck_file_resources_provider_shape",
        ),
        sa.CheckConstraint(
            "((immutability_mode = 'content_hash' AND object_version IS NULL) OR "
            "(immutability_mode = 'version_id' AND object_version IS NOT NULL)) AND "
            "(storage_provider <> 'local' OR immutability_mode = 'content_hash')",
            name="ck_file_resources_immutability",
        ),
        sa.CheckConstraint(
            "length(endpoint_ref) BETWEEN 1 AND 128 AND "
            "length(object_key) BETWEEN 1 AND 2048 AND "
            "(bucket IS NULL OR length(bucket) BETWEEN 1 AND 255) AND "
            "(object_version IS NULL OR length(object_version) BETWEEN 1 AND 512) AND "
            "(etag IS NULL OR length(etag) BETWEEN 1 AND 512)",
            name="ck_file_resources_locator_bounds",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_file_resources_file_identity",
        ),
        sa.CheckConstraint(
            "storage_status IN "
            "('storing','available','storage_failed','deleting','deleted')",
            name="ck_file_resources_storage_status",
        ),
        sa.CheckConstraint(
            "storage_status <> 'available' OR storage_verified_at IS NOT NULL",
            name="ck_file_resources_available_verified",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_file_resources_library_relative_path",
        "file_resources",
        ["library_id", "relative_path"],
    )
    op.create_index(
        "ix_file_resources_library_created",
        "file_resources",
        ["library_id", "created_at"],
    )
    op.create_index("ix_file_resources_sha256", "file_resources", ["sha256"])
    op.create_index(
        "ix_file_resources_library_storage_status",
        "file_resources",
        ["library_id", "storage_status"],
    )
    op.create_index(
        "ix_file_resources_storage_object",
        "file_resources",
        ["storage_provider", "endpoint_ref", "bucket", "object_key"],
    )

    op.add_column(
        "document_import_jobs",
        sa.Column("file_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_import_jobs_file_resource_id",
        "document_import_jobs",
        "file_resources",
        ["file_resource_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_document_import_jobs_file_resource",
        "document_import_jobs",
        ["file_resource_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_document_import_jobs_file_resource",
        "document_import_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_document_import_jobs_file_resource_id",
        "document_import_jobs",
        type_="foreignkey",
    )
    op.drop_column("document_import_jobs", "file_resource_id")
    op.drop_index("ix_file_resources_storage_object", table_name="file_resources")
    op.drop_index(
        "ix_file_resources_library_storage_status", table_name="file_resources"
    )
    op.drop_index("ix_file_resources_sha256", table_name="file_resources")
    op.drop_index("ix_file_resources_library_created", table_name="file_resources")
    op.drop_index(
        "ix_file_resources_library_relative_path", table_name="file_resources"
    )
    op.drop_table("file_resources")
