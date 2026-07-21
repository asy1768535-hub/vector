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


RETENTION_POLICY_VERSION = "retention-policy-v1"
RETENTION_REASON_REPLACEMENT_READY = "replacement_ready"
RETENTION_STATUSES = (
    "scheduled",
    "eligible",
    "blocked",
    "held",
    "queued",
    "processing",
    "cleaned",
    "failed",
    "cancelled",
)


class RevisionRetentionRecord(Base):
    __tablename__ = "revision_retention_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','eligible','blocked','held','queued',"
            "'processing','cleaned','failed','cancelled')",
            name="ck_revision_retention_status",
        ),
        CheckConstraint(
            "reason = 'replacement_ready'",
            name="ck_revision_retention_reason",
        ),
        CheckConstraint(
            "policy_version = 'retention-policy-v1'",
            name="ck_revision_retention_policy_version",
        ),
        CheckConstraint(
            "retention_days BETWEEN 30 AND 60 AND "
            "notice_days BETWEEN 1 AND 14 AND notice_days < retention_days",
            name="ck_revision_retention_policy_bounds",
        ),
        CheckConstraint(
            "cleanup_eligible_at >= replacement_ready_at AND "
            "cleanup_not_before >= cleanup_eligible_at AND "
            "notice_at < cleanup_not_before",
            name="ck_revision_retention_time_order",
        ),
        CheckConstraint(
            "notice_recorded_at IS NULL OR notice_recorded_at >= notice_at",
            name="ck_revision_retention_notice_time",
        ),
        CheckConstraint(
            "jsonb_typeof(impact_snapshot) = 'object' AND "
            "octet_length(impact_snapshot::text) <= 8192 AND "
            "impact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_revision_retention_impact",
        ),
        CheckConstraint(
            "((status IN ('blocked','cancelled') AND block_code IS NOT NULL) OR "
            "(status NOT IN ('blocked','cancelled') AND block_code IS NULL))",
            name="ck_revision_retention_block_code",
        ),
        CheckConstraint(
            "((status = 'held' AND hold_reason_code IS NOT NULL AND "
            "held_by_user_id IS NOT NULL AND held_at IS NOT NULL) OR "
            "(status <> 'held' AND hold_reason_code IS NULL AND "
            "held_by_user_id IS NULL AND held_at IS NULL))",
            name="ck_revision_retention_hold_shape",
        ),
        UniqueConstraint(
            "revision_file_id",
            name="uq_revision_retention_revision_file",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_revision_retention_idempotency",
        ),
        Index(
            "ix_revision_retention_due",
            "status",
            "cleanup_not_before",
        ),
        Index(
            "ix_revision_retention_notice_due",
            "notice_recorded_at",
            "notice_at",
        ),
        Index(
            "ix_revision_retention_library_status",
            "library_id",
            "status",
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
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    replacement_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revision_files.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RETENTION_REASON_REPLACEMENT_READY,
        server_default=RETENTION_REASON_REPLACEMENT_READY,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="scheduled", server_default="scheduled"
    )
    policy_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RETENTION_POLICY_VERSION,
        server_default=RETENTION_POLICY_VERSION,
    )
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    notice_days: Mapped[int] = mapped_column(Integer, nullable=False)
    replacement_ready_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cleanup_eligible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cleanup_not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notice_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notice_recorded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    block_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    impact_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    deadline_changed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    deadline_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hold_reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    held_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    held_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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
