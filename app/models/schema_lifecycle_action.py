from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


SCHEMA_LIFECYCLE_ACTION_KINDS = (
    "clone_version",
    "create_item",
    "update_item",
    "disable_item",
    "activate_version",
)
SCHEMA_LIFECYCLE_TARGET_KINDS = (
    "ontology_version",
    "entity_type",
    "relation_type",
    "attribute",
    "constraint",
)


class SchemaLifecycleAction(Base):
    __tablename__ = "schema_lifecycle_actions"
    __table_args__ = (
        CheckConstraint(
            "action_kind IN ('clone_version','create_item','update_item',"
            "'disable_item','activate_version')",
            name="ck_schema_lifecycle_actions_kind",
        ),
        CheckConstraint(
            "target_kind IN ('ontology_version','entity_type','relation_type',"
            "'attribute','constraint')",
            name="ck_schema_lifecycle_actions_target_kind",
        ),
        CheckConstraint(
            "expected_state_hash ~ '^[0-9a-f]{64}$' AND "
            "command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_schema_lifecycle_actions_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(result_payload) = 'object' AND "
            "octet_length(result_payload::text) <= 65536",
            name="ck_schema_lifecycle_actions_payload",
        ),
        CheckConstraint(
            "btrim(idempotency_key) <> ''",
            name="ck_schema_lifecycle_actions_idempotency_key",
        ),
        UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_schema_lifecycle_actions_library_key",
        ),
        Index(
            "ix_schema_lifecycle_actions_scope_created",
            "library_id",
            "ontology_version_id",
            "created_at",
        ),
        Index(
            "ix_schema_lifecycle_actions_target",
            "library_id",
            "target_kind",
            "target_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="RESTRICT",
            name="fk_schema_lifecycle_actions_library",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_schema_lifecycle_actions_ontology",
        ),
        nullable=False,
    )
    action_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    expected_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_schema_lifecycle_actions_actor",
        ),
        nullable=True,
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
