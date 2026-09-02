from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


RELATION_DIRECTION_DIRECTED = "directed"
RELATION_DIRECTION_UNDIRECTED = "undirected"
REVIEW_POLICY_AUTO_ACTIVE = "auto_active"
REVIEW_POLICY_PENDING_REVIEW = "pending_review"
REVIEW_POLICY_MANUAL_ONLY = "manual_only"


class RelationType(Base):
    __tablename__ = "relation_types"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_relation_types_status",
        ),
        CheckConstraint(
            "direction IN ('directed','undirected')",
            name="ck_relation_types_direction",
        ),
        CheckConstraint(
            "default_review_policy IN ('auto_active','pending_review','manual_only')",
            name="ck_relation_types_review_policy",
        ),
        UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "key",
            name="uq_relation_types_library_ontology_key",
        ),
        UniqueConstraint("id", "library_id", name="uq_relation_types_id_library"),
        Index("ix_relation_types_library_ontology_status", "library_id", "ontology_version_id", "status"),
        Index("ix_relation_types_library_key", "library_id", "key"),
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
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_evidence: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    default_review_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    properties_schema: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
