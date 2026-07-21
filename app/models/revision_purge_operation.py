from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


REVISION_PURGE_OPERATION_STATUSES = (
    "planned",
    "processing",
    "cleanup_pending",
    "completed",
    "failed",
    "cancelled",
)


class RevisionPurgeOperation(Base):
    __tablename__ = "revision_purge_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned','processing','cleanup_pending','completed','failed','cancelled')",
            name="ck_revision_purge_operations_status",
        ),
        CheckConstraint(
            "source_publication_id <> replacement_publication_id",
            name="ck_revision_purge_operations_publications_differ",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_revision_purge_operations_attempt_count",
        ),
        CheckConstraint(
            "confirmation_hash ~ '^[0-9a-f]{64}$' AND "
            "impact_hash ~ '^[0-9a-f]{64}$' AND "
            "source_manifest_hash ~ '^[0-9a-f]{64}$' AND "
            "replacement_manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_revision_purge_operations_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(impact_snapshot) = 'object' AND "
            "octet_length(impact_snapshot::text) <= 16384",
            name="ck_revision_purge_operations_impact",
        ),
        CheckConstraint(
            "((status = 'processing' AND worker_id IS NOT NULL AND "
            "claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL AND lease_expires_at > claimed_at) OR "
            "(status <> 'processing' AND worker_id IS NULL AND claim_token IS NULL AND "
            "claimed_at IS NULL AND lease_expires_at IS NULL))",
            name="ck_revision_purge_operations_claim_shape",
        ),
        CheckConstraint(
            "((status IN ('planned','failed') AND available_at IS NOT NULL) OR "
            "(status NOT IN ('planned','failed') AND available_at IS NULL))",
            name="ck_revision_purge_operations_available_shape",
        ),
        CheckConstraint(
            "((status IN ('completed','cancelled') AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed','cancelled') AND finished_at IS NULL))",
            name="ck_revision_purge_operations_finished_shape",
        ),
        CheckConstraint(
            "((status = 'failed' AND last_error_code IS NOT NULL) OR "
            "(status <> 'failed' AND last_error_code IS NULL))",
            name="ck_revision_purge_operations_error_shape",
        ),
        UniqueConstraint(
            "retention_record_id",
            name="uq_revision_purge_operations_retention",
        ),
        UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_revision_purge_operations_idempotency",
        ),
        Index(
            "ix_revision_purge_operations_due",
            "status",
            "available_at",
        ),
        Index(
            "ix_revision_purge_operations_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_revision_purge_operations_cleanup_pending",
            "status",
            "updated_at",
        ),
        Index(
            "ix_revision_purge_operations_library_status",
            "library_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retention_record_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("revision_retention_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revision_files.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_publication_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_publications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    replacement_publication_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_publications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planned", server_default="planned"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    replacement_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
