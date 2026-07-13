from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


GRAPH_FACT_STATUS_DRAFT = "draft"
GRAPH_FACT_STATUS_PENDING_REVIEW = "pending_review"
GRAPH_FACT_STATUS_ACTIVE = "active"
GRAPH_FACT_STATUS_REJECTED = "rejected"
GRAPH_FACT_STATUS_STALE = "stale"
GRAPH_FACT_STATUS_DISABLED = "disabled"
GRAPH_FACT_STATUS_DELETED = "deleted"

GRAPH_SOURCE_MANUAL = "manual"
GRAPH_SOURCE_IMPORTED = "imported"
GRAPH_SOURCE_EXTRACTED = "extracted"


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_review','active','rejected','stale','disabled','deleted')",
            name="ck_entities_status",
        ),
        CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_entities_source_type",
        ),
        UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "entity_type_id",
            "normalized_name",
            name="uq_entities_library_ontology_type_normalized",
        ),
        Index("ix_entities_library_ontology_status", "library_id", "ontology_version_id", "status"),
        Index("ix_entities_library_type_status", "library_id", "entity_type_id", "status"),
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
        ForeignKey("ontology_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entity_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entity_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    properties: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=GRAPH_FACT_STATUS_DRAFT)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default=GRAPH_SOURCE_MANUAL)
    authority_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
