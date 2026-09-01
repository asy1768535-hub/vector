from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


CANONICAL_ENTITY_STATUS_ACTIVE = "active"
CANONICAL_ENTITY_STATUS_PENDING_REVIEW = "pending_review"
CANONICAL_ENTITY_STATUS_DISABLED = "disabled"


class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','pending_review','disabled')",
            name="ck_canonical_entities_status",
        ),
        UniqueConstraint("id", "library_id", name="uq_canonical_entities_id_library"),
        Index(
            "ix_canonical_entities_library_normalized_status",
            "library_id",
            "normalized_name",
            "status",
        ),
        Index("ix_canonical_entities_library_status", "library_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CANONICAL_ENTITY_STATUS_ACTIVE,
        server_default=CANONICAL_ENTITY_STATUS_ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
