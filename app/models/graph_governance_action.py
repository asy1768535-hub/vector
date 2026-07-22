from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


GRAPH_GOVERNANCE_ACTION_KINDS = (
    "entity_create",
    "relation_create",
    "entity_correct",
    "relation_correct",
    "entity_disable",
    "entity_restore",
    "relation_disable",
    "relation_restore",
    "relation_review",
    "alias_add",
    "alias_disable",
    "entity_merge",
)
GRAPH_GOVERNANCE_ACTION_STATUSES = (
    "pending_review",
    "approved",
    "rejected",
    "cancelled",
    "applied",
)
GRAPH_GOVERNANCE_ITEM_KINDS = ("entity", "relation", "alias")
GRAPH_GOVERNANCE_EFFECT_KINDS = (
    "activate",
    "update",
    "disable",
    "restore",
    "reassign",
    "retain",
)


class GraphGovernanceAction(Base):
    __tablename__ = "graph_governance_actions"
    __table_args__ = (
        CheckConstraint(
            "action_kind IN ('entity_create','relation_create','entity_correct',"
            "'relation_correct','entity_disable','entity_restore','relation_disable',"
            "'relation_restore','relation_review','alias_add','alias_disable','entity_merge')",
            name="ck_graph_governance_actions_kind",
        ),
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','cancelled','applied')",
            name="ck_graph_governance_actions_status",
        ),
        CheckConstraint(
            "expected_state_hash ~ '^[0-9a-f]{64}$' AND "
            "command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_governance_actions_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536",
            name="ck_graph_governance_actions_payload",
        ),
        CheckConstraint(
            "btrim(idempotency_key) <> '' AND "
            "(reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_graph_governance_actions_values",
        ),
        CheckConstraint(
            "((action_kind IN ('entity_create','entity_correct','entity_disable','entity_restore') "
            "AND target_entity_id IS NOT NULL AND target_relation_id IS NULL "
            "AND target_alias_id IS NULL AND survivor_entity_id IS NULL AND loser_entity_id IS NULL) OR "
            "(action_kind IN ('relation_create','relation_correct','relation_disable',"
            "'relation_restore','relation_review') AND target_entity_id IS NULL "
            "AND target_relation_id IS NOT NULL AND target_alias_id IS NULL "
            "AND survivor_entity_id IS NULL AND loser_entity_id IS NULL) OR "
            "(action_kind IN ('alias_add','alias_disable') AND target_entity_id IS NULL "
            "AND target_relation_id IS NULL AND target_alias_id IS NOT NULL "
            "AND survivor_entity_id IS NULL AND loser_entity_id IS NULL) OR "
            "(action_kind = 'entity_merge' AND target_entity_id IS NULL "
            "AND target_relation_id IS NULL AND target_alias_id IS NULL "
            "AND survivor_entity_id IS NOT NULL AND loser_entity_id IS NOT NULL "
            "AND survivor_entity_id <> loser_entity_id))",
            name="ck_graph_governance_actions_target",
        ),
        CheckConstraint(
            "((status = 'pending_review' AND decided_at IS NULL "
            "AND cancelled_at IS NULL AND applied_at IS NULL) OR "
            "(status IN ('approved','rejected') AND decided_at IS NOT NULL "
            "AND cancelled_at IS NULL AND applied_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND applied_at IS NULL) OR "
            "(status = 'applied' AND decided_at IS NOT NULL "
            "AND cancelled_at IS NULL AND applied_at IS NOT NULL))",
            name="ck_graph_governance_actions_lifecycle",
        ),
        UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_graph_governance_actions_library_key",
        ),
        Index(
            "ix_graph_governance_actions_scope_status",
            "library_id",
            "ontology_version_id",
            "status",
            "created_at",
        ),
        Index("ix_graph_governance_actions_entity", "target_entity_id"),
        Index("ix_graph_governance_actions_relation", "target_relation_id"),
        Index("ix_graph_governance_actions_alias", "target_alias_id"),
        Index("ix_graph_governance_actions_planned", "planned_publication_id"),
        Index("ix_graph_governance_actions_applied", "applied_publication_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_library",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_ontology",
        ),
        nullable=False,
    )
    action_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_target_entity",
        ),
        nullable=True,
    )
    target_relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_relations.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_target_relation",
        ),
        nullable=True,
    )
    target_alias_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entity_aliases.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_target_alias",
        ),
        nullable=True,
    )
    survivor_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_survivor",
        ),
        nullable=True,
    )
    loser_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_actions_loser",
        ),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    expected_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    planned_publication_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="SET NULL",
            name="fk_graph_governance_actions_planned",
        ),
        nullable=True,
    )
    applied_publication_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="SET NULL",
            name="fk_graph_governance_actions_applied",
        ),
        nullable=True,
    )
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_graph_governance_actions_requested_by",
        ),
        nullable=True,
    )
    decided_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_graph_governance_actions_decided_by",
        ),
        nullable=True,
    )
    cancelled_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_graph_governance_actions_cancelled_by",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GraphGovernanceActionItem(Base):
    __tablename__ = "graph_governance_action_items"
    __table_args__ = (
        CheckConstraint(
            "item_kind IN ('entity','relation','alias')",
            name="ck_graph_governance_action_items_kind",
        ),
        CheckConstraint(
            "effect_kind IN ('activate','update','disable','restore','reassign','retain')",
            name="ck_graph_governance_action_items_effect",
        ),
        CheckConstraint(
            "ordinal BETWEEN 0 AND 999",
            name="ck_graph_governance_action_items_ordinal",
        ),
        CheckConstraint(
            "((item_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL "
            "AND alias_id IS NULL) OR "
            "(item_kind = 'relation' AND entity_id IS NULL AND relation_id IS NOT NULL "
            "AND alias_id IS NULL) OR "
            "(item_kind = 'alias' AND entity_id IS NULL AND relation_id IS NULL "
            "AND alias_id IS NOT NULL))",
            name="ck_graph_governance_action_items_target",
        ),
        CheckConstraint(
            "(before_hash IS NULL OR before_hash ~ '^[0-9a-f]{64}$') AND "
            "after_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_governance_action_items_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(effect_payload) = 'object' AND "
            "octet_length(effect_payload::text) <= 65536",
            name="ck_graph_governance_action_items_payload",
        ),
        CheckConstraint(
            "((status = 'planned' AND applied_at IS NULL) OR "
            "(status = 'applied' AND applied_at IS NOT NULL))",
            name="ck_graph_governance_action_items_status",
        ),
        UniqueConstraint(
            "action_id",
            "ordinal",
            name="uq_graph_governance_action_items_action_order",
        ),
        Index(
            "ix_graph_governance_action_items_action_status",
            "action_id",
            "status",
            "ordinal",
        ),
        Index(
            "ix_graph_governance_action_items_scope_kind",
            "library_id",
            "item_kind",
        ),
        Index("ix_graph_governance_action_items_entity", "entity_id"),
        Index("ix_graph_governance_action_items_relation", "relation_id"),
        Index("ix_graph_governance_action_items_alias", "alias_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_governance_actions.id",
            ondelete="CASCADE",
            name="fk_graph_governance_action_items_action",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_action_items_library",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    item_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entities.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_action_items_entity",
        ),
        nullable=True,
    )
    relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_relations.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_action_items_relation",
        ),
        nullable=True,
    )
    alias_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "entity_aliases.id",
            ondelete="RESTRICT",
            name="fk_graph_governance_action_items_alias",
        ),
        nullable=True,
    )
    before_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="planned", server_default="planned"
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
