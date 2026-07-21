from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


SCHEMA_STATUS_DRAFT = "draft"
SCHEMA_STATUS_ACTIVE = "active"
SCHEMA_STATUS_DISABLED = "disabled"
SCHEMA_STATUS_DELETED = "deleted"


class EntityType(Base):
    __tablename__ = "entity_types"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_entity_types_status",
        ),
        UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "key",
            name="uq_entity_types_library_ontology_key",
        ),
        Index("ix_entity_types_library_ontology_status", "library_id", "ontology_version_id", "status"),
        Index("ix_entity_types_library_key", "library_id", "key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    properties_schema: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=SCHEMA_STATUS_DRAFT)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
