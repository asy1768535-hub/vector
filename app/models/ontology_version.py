from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ONTOLOGY_STATUS_DRAFT = "draft"
ONTOLOGY_STATUS_ACTIVE = "active"
ONTOLOGY_STATUS_DISABLED = "disabled"
ONTOLOGY_STATUS_DELETED = "deleted"


class OntologyVersion(Base):
    __tablename__ = "ontology_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_ontology_versions_status",
        ),
        UniqueConstraint(
            "library_id",
            "version_key",
            "version_no",
            name="uq_ontology_versions_library_key_version_no",
        ),
        Index("ix_ontology_versions_library_status", "library_id", "status"),
        Index("ix_ontology_versions_library_key_status", "library_id", "version_key", "status"),
        Index(
            "uq_ontology_versions_library_version_key_active",
            "library_id",
            "version_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ONTOLOGY_STATUS_DRAFT)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
