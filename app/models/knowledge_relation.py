from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_review','active','rejected','stale','disabled','deleted')",
            name="ck_knowledge_relations_status",
        ),
        CheckConstraint(
            "review_status IS NULL OR review_status IN ('pending_review','approved','rejected','not_required')",
            name="ck_knowledge_relations_review_status",
        ),
        CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_knowledge_relations_source_type",
        ),
        Index(
            "ix_knowledge_relations_library_ontology_status",
            "library_id",
            "ontology_version_id",
            "status",
        ),
        Index(
            "ix_knowledge_relations_library_source_relation",
            "library_id",
            "source_entity_id",
            "relation_type_id",
        ),
        Index(
            "ix_knowledge_relations_library_target_relation",
            "library_id",
            "target_entity_id",
            "relation_type_id",
        ),
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
    relation_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relation_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    properties: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    review_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    authority_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
