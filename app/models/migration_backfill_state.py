from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MigrationBackfillState(Base):
    __tablename__ = "migration_backfill_state"
    __table_args__ = (
        Index("uq_migration_backfill_state_task_library", "task_name", "library_id", unique=True),
        Index("ix_migration_backfill_state_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)
    library_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    last_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    processed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
