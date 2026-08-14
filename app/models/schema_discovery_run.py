from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SchemaDiscoveryRun(Base):
    """One corpus-level Schema discovery coordination record.

    ``source_set_key`` is the caller-owned import batch identity.  The
    revision list and source hash make the actual corpus immutable and
    auditable even when a batch is retried or documents are replaced later.
    """

    __tablename__ = "schema_discovery_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','discovering','waiting_confirmation','succeeded','failed','cancelled')",
            name="ck_schema_discovery_runs_status",
        ),
        UniqueConstraint(
            "library_id",
            "source_set_key",
            name="uq_schema_discovery_runs_library_source_set",
        ),
        Index(
            "ix_schema_discovery_runs_library_status_created",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_schema_discovery_runs_source_hash",
            "library_id",
            "source_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_set_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_revision_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", server_default="queued")
    confirmation_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="required", server_default="required"
    )
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ontology_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    ontology_snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    concept_inventory: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    discovery_trace: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def schema_state(self) -> str:
        return {
            "queued": "waiting_schema",
            "discovering": "discovering_schema",
            "succeeded": "confirmed_schema",
            "waiting_confirmation": "ai_draft_pending_confirmation",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(self.status, "failed")
