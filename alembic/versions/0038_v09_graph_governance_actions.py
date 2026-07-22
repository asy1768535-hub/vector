"""v0.9 graph governance actions

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_governance_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_alias_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("survivor_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("loser_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expected_state_hash", sa.String(length=64), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("planned_publication_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_publication_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancelled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action_kind IN ('entity_create','relation_create','entity_correct',"
            "'relation_correct','entity_disable','entity_restore','relation_disable',"
            "'relation_restore','relation_review','alias_add','alias_disable','entity_merge')",
            name="ck_graph_governance_actions_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','approved','rejected','cancelled','applied')",
            name="ck_graph_governance_actions_status",
        ),
        sa.CheckConstraint(
            "expected_state_hash ~ '^[0-9a-f]{64}$' AND "
            "command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_governance_actions_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536",
            name="ck_graph_governance_actions_payload",
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND "
            "(reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_graph_governance_actions_values",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "((status = 'pending_review' AND decided_at IS NULL "
            "AND cancelled_at IS NULL AND applied_at IS NULL) OR "
            "(status IN ('approved','rejected') AND decided_at IS NOT NULL "
            "AND cancelled_at IS NULL AND applied_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND applied_at IS NULL) OR "
            "(status = 'applied' AND decided_at IS NOT NULL "
            "AND cancelled_at IS NULL AND applied_at IS NOT NULL))",
            name="ck_graph_governance_actions_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_graph_governance_actions_library",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"],
            ["ontology_versions.id"],
            name="fk_graph_governance_actions_ontology",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"],
            ["entities.id"],
            name="fk_graph_governance_actions_target_entity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_relation_id"],
            ["knowledge_relations.id"],
            name="fk_graph_governance_actions_target_relation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_alias_id"],
            ["entity_aliases.id"],
            name="fk_graph_governance_actions_target_alias",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["survivor_entity_id"],
            ["entities.id"],
            name="fk_graph_governance_actions_survivor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["loser_entity_id"],
            ["entities.id"],
            name="fk_graph_governance_actions_loser",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planned_publication_id"],
            ["graph_publications.id"],
            name="fk_graph_governance_actions_planned",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["applied_publication_id"],
            ["graph_publications.id"],
            name="fk_graph_governance_actions_applied",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["sys_users.id"],
            name="fk_graph_governance_actions_requested_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["sys_users.id"],
            name="fk_graph_governance_actions_decided_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_user_id"],
            ["sys_users.id"],
            name="fk_graph_governance_actions_cancelled_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_graph_governance_actions_library_key",
        ),
    )
    op.create_index(
        "ix_graph_governance_actions_scope_status",
        "graph_governance_actions",
        ["library_id", "ontology_version_id", "status", "created_at"],
    )
    for name, column in (
        ("ix_graph_governance_actions_entity", "target_entity_id"),
        ("ix_graph_governance_actions_relation", "target_relation_id"),
        ("ix_graph_governance_actions_alias", "target_alias_id"),
        ("ix_graph_governance_actions_planned", "planned_publication_id"),
        ("ix_graph_governance_actions_applied", "applied_publication_id"),
    ):
        op.create_index(name, "graph_governance_actions", [column])

    op.create_table(
        "graph_governance_action_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.String(length=16), nullable=False),
        sa.Column("effect_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("alias_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "effect_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="planned",
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_kind IN ('entity','relation','alias')",
            name="ck_graph_governance_action_items_kind",
        ),
        sa.CheckConstraint(
            "effect_kind IN ('activate','update','disable','restore','reassign','retain')",
            name="ck_graph_governance_action_items_effect",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 999",
            name="ck_graph_governance_action_items_ordinal",
        ),
        sa.CheckConstraint(
            "((item_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL "
            "AND alias_id IS NULL) OR "
            "(item_kind = 'relation' AND entity_id IS NULL AND relation_id IS NOT NULL "
            "AND alias_id IS NULL) OR "
            "(item_kind = 'alias' AND entity_id IS NULL AND relation_id IS NULL "
            "AND alias_id IS NOT NULL))",
            name="ck_graph_governance_action_items_target",
        ),
        sa.CheckConstraint(
            "(before_hash IS NULL OR before_hash ~ '^[0-9a-f]{64}$') AND "
            "after_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_governance_action_items_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(effect_payload) = 'object' AND "
            "octet_length(effect_payload::text) <= 65536",
            name="ck_graph_governance_action_items_payload",
        ),
        sa.CheckConstraint(
            "((status = 'planned' AND applied_at IS NULL) OR "
            "(status = 'applied' AND applied_at IS NOT NULL))",
            name="ck_graph_governance_action_items_status",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["graph_governance_actions.id"],
            name="fk_graph_governance_action_items_action",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_graph_governance_action_items_library",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name="fk_graph_governance_action_items_entity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["relation_id"],
            ["knowledge_relations.id"],
            name="fk_graph_governance_action_items_relation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["alias_id"],
            ["entity_aliases.id"],
            name="fk_graph_governance_action_items_alias",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_id",
            "ordinal",
            name="uq_graph_governance_action_items_action_order",
        ),
    )
    op.create_index(
        "ix_graph_governance_action_items_action_status",
        "graph_governance_action_items",
        ["action_id", "status", "ordinal"],
    )
    op.create_index(
        "ix_graph_governance_action_items_scope_kind",
        "graph_governance_action_items",
        ["library_id", "item_kind"],
    )
    for name, column in (
        ("ix_graph_governance_action_items_entity", "entity_id"),
        ("ix_graph_governance_action_items_relation", "relation_id"),
        ("ix_graph_governance_action_items_alias", "alias_id"),
    ):
        op.create_index(name, "graph_governance_action_items", [column])


def downgrade() -> None:
    for name in (
        "ix_graph_governance_action_items_alias",
        "ix_graph_governance_action_items_relation",
        "ix_graph_governance_action_items_entity",
        "ix_graph_governance_action_items_scope_kind",
        "ix_graph_governance_action_items_action_status",
    ):
        op.drop_index(name, table_name="graph_governance_action_items")
    op.drop_table("graph_governance_action_items")

    for name in (
        "ix_graph_governance_actions_applied",
        "ix_graph_governance_actions_planned",
        "ix_graph_governance_actions_alias",
        "ix_graph_governance_actions_relation",
        "ix_graph_governance_actions_entity",
        "ix_graph_governance_actions_scope_status",
    ):
        op.drop_index(name, table_name="graph_governance_actions")
    op.drop_table("graph_governance_actions")
