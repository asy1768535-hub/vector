from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FileResource(Base):
    """The durable uploaded file, independent from processed knowledge."""

    __tablename__ = "file_resources"
    __table_args__ = (
        CheckConstraint(
            "storage_provider IN ('local','minio','oss')",
            name="ck_file_resources_storage_provider",
        ),
        CheckConstraint(
            "((storage_provider = 'local' AND bucket IS NULL) OR "
            "(storage_provider IN ('minio','oss') AND bucket IS NOT NULL))",
            name="ck_file_resources_provider_shape",
        ),
        CheckConstraint(
            "((immutability_mode = 'content_hash' AND object_version IS NULL) OR "
            "(immutability_mode = 'version_id' AND object_version IS NOT NULL)) AND "
            "(storage_provider <> 'local' OR immutability_mode = 'content_hash')",
            name="ck_file_resources_immutability",
        ),
        CheckConstraint(
            "length(endpoint_ref) BETWEEN 1 AND 128 AND "
            "length(object_key) BETWEEN 1 AND 2048 AND "
            "(bucket IS NULL OR length(bucket) BETWEEN 1 AND 255) AND "
            "(object_version IS NULL OR length(object_version) BETWEEN 1 AND 512) AND "
            "(etag IS NULL OR length(etag) BETWEEN 1 AND 512)",
            name="ck_file_resources_locator_bounds",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_file_resources_file_identity",
        ),
        CheckConstraint(
            "storage_status IN "
            "('storing','available','storage_failed','deleting','deleted')",
            name="ck_file_resources_storage_status",
        ),
        CheckConstraint(
            "storage_status <> 'available' OR storage_verified_at IS NOT NULL",
            name="ck_file_resources_available_verified",
        ),
        Index(
            "ix_file_resources_library_relative_path",
            "library_id",
            "relative_path",
        ),
        Index(
            "ix_file_resources_library_created",
            "library_id",
            "created_at",
        ),
        Index("ix_file_resources_sha256", "sha256"),
        Index(
            "ix_file_resources_library_storage_status",
            "library_id",
            "storage_status",
        ),
        Index(
            "ix_file_resources_storage_object",
            "storage_provider",
            "endpoint_ref",
            "bucket",
            "object_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local", server_default="local"
    )
    endpoint_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, default="primary", server_default="primary"
    )
    bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    object_version: Mapped[str | None] = mapped_column(String(512), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    immutability_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="content_hash", server_default="content_hash"
    )
    storage_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="storing", server_default="storing"
    )
    storage_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
