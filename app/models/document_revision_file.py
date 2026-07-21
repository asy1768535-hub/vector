from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentRevisionFile(Base):
    __tablename__ = "document_revision_files"
    __table_args__ = (
        CheckConstraint(
            "storage_provider IN ('local','minio','oss')",
            name="ck_document_revision_files_storage_provider",
        ),
        CheckConstraint(
            "((storage_provider = 'local' AND bucket IS NULL) OR "
            "(storage_provider IN ('minio','oss') AND bucket IS NOT NULL))",
            name="ck_document_revision_files_provider_shape",
        ),
        CheckConstraint(
            "((immutability_mode = 'content_hash' AND object_version IS NULL) OR "
            "(immutability_mode = 'version_id' AND object_version IS NOT NULL)) AND "
            "(storage_provider <> 'local' OR immutability_mode = 'content_hash')",
            name="ck_document_revision_files_immutability",
        ),
        CheckConstraint(
            "length(endpoint_ref) BETWEEN 1 AND 128 AND "
            "length(object_key) BETWEEN 1 AND 2048 AND "
            "(bucket IS NULL OR length(bucket) BETWEEN 1 AND 255) AND "
            "(object_version IS NULL OR length(object_version) BETWEEN 1 AND 512) AND "
            "(etag IS NULL OR length(etag) BETWEEN 1 AND 512)",
            name="ck_document_revision_files_locator_bounds",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_revision_files_file_identity",
        ),
        CheckConstraint(
            "source_locator IS NULL OR "
            "(jsonb_typeof(source_locator) = 'object' AND "
            "octet_length(source_locator::text) <= 8192)",
            name="ck_document_revision_files_source_locator_object",
        ),
        CheckConstraint(
            "managed_snapshot OR "
            "(immutability_mode = 'version_id' AND source_locator IS NOT NULL AND "
            "source_locator->>'kind' = 'external_object')",
            name="ck_document_revision_files_external_ownership",
        ),
        UniqueConstraint(
            "document_revision_id",
            name="uq_document_revision_files_revision",
        ),
        Index("ix_document_revision_files_revision", "document_revision_id"),
        Index("ix_document_revision_files_document", "document_id"),
        Index(
            "ix_document_revision_files_storage_object",
            "storage_provider",
            "endpoint_ref",
            "bucket",
            "object_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local", server_default="local"
    )
    endpoint_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, default="primary", server_default="primary"
    )
    bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    object_version: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    etag: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    immutability_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="content_hash", server_default="content_hash"
    )
    managed_snapshot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    source_locator: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
