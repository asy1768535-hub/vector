from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RelationTypeConstraint(Base):
    __tablename__ = "relation_type_constraints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_relation_type_constraints_status",
        ),
        CheckConstraint(
            "cardinality IS NULL OR cardinality IN ('one_to_one','one_to_many','many_to_one','many_to_many')",
            name="ck_relation_type_constraints_cardinality",
        ),
        UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "relation_type_id",
            "source_entity_type_id",
            "target_entity_type_id",
            name="uq_relation_type_constraints_scope",
        ),
        Index(
            "ix_relation_type_constraints_library_ontology_status",
            "library_id",
            "ontology_version_id",
            "status",
        ),
        Index("ix_relation_type_constraints_library_relation", "library_id", "relation_type_id"),
        Index(
            "ix_relation_type_constraints_library_source_target",
            "library_id",
            "source_entity_type_id",
            "target_entity_type_id",
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
        ForeignKey("ontology_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relation_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_entity_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entity_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_entity_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entity_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cardinality: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    requires_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
