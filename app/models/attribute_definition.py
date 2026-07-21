from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ATTRIBUTE_OWNER_ENTITY_TYPE = "entity_type"
ATTRIBUTE_OWNER_RELATION_TYPE = "relation_type"


class AttributeDefinition(Base):
    __tablename__ = "attribute_definitions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_attribute_definitions_status",
        ),
        CheckConstraint(
            "owner_kind IN ('entity_type','relation_type')",
            name="ck_attribute_definitions_owner_kind",
        ),
        CheckConstraint(
            "value_type IN ('string','text','integer','number','boolean','date','datetime','enum','json')",
            name="ck_attribute_definitions_value_type",
        ),
        UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "owner_kind",
            "owner_type_id",
            "key",
            name="uq_attribute_definitions_scope_key",
        ),
        Index(
            "ix_attribute_definitions_library_ontology_owner",
            "library_id",
            "ontology_version_id",
            "owner_kind",
        ),
        Index("ix_attribute_definitions_library_owner", "library_id", "owner_type_id"),
        Index("ix_attribute_definitions_library_status", "library_id", "status"),
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
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_type_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    enum_values: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    validation_schema: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
