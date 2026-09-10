from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentImportJob(Base):
    __tablename__ = "document_import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('uploading','queued','processing','succeeded','failed','cancelled','superseded')",
            name="ck_document_import_jobs_status",
        ),
        CheckConstraint(
            "current_stage IN "
            "('uploading','queued','converting','conversion_ready','validating','parsing',"
            "'chunking','embedding','graph','completed')",
            name="ck_document_import_jobs_stage",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND upload_offset >= 0 AND upload_offset <= size_bytes",
            name="ck_document_import_jobs_offsets",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_document_import_jobs_attempts",
        ),
        CheckConstraint(
            "conversion_attempt_count >= 0",
            name="ck_document_import_jobs_conversion_attempts",
        ),
        Index(
            "ix_document_import_jobs_claimable",
            "status",
            "claimed_at",
            "created_at",
        ),
        Index(
            "ix_document_import_jobs_library_created",
            "library_id",
            "created_at",
        ),
        Index(
            "ix_document_import_jobs_batch",
            "library_id",
            "batch_id",
        ),
        Index(
            "ix_document_import_jobs_resume",
            "library_id",
            "batch_id",
            "relative_path",
            "file_name",
        ),
        UniqueConstraint(
            "file_resource_id",
            name="uq_document_import_jobs_file_resource",
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
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    relative_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    upload_offset: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_modified_millis: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    conversion_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    converter_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    conversion_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    staging_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    replace_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=True,
    )
    security_level: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    graph_extraction_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploading", server_default="uploading"
    )
    current_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploading", server_default="uploading"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    upload_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_operation: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    document_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    file_resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("file_resources.id", ondelete="RESTRICT"),
        nullable=True,
    )
    embedding_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("embedding_jobs.id", ondelete="SET NULL"),
        nullable=True,
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
