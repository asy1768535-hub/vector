"""v0.5 active graph publication

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _fk_column(
    column_name: str,
    target: str,
    constraint_name: str,
    ondelete: str,
    *,
    nullable: bool = False,
) -> sa.Column:
    return sa.Column(
        column_name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, name=constraint_name, ondelete=ondelete),
        nullable=nullable,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def upgrade() -> None:
    _create_graph_publications()
    _create_graph_publication_items()


def _create_graph_publications() -> None:
    table = "graph_publications"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_publications_library",
            "CASCADE",
        ),
        _fk_column(
            "ontology_version_id",
            "ontology_versions.id",
            "fk_graph_publications_ontology",
            "RESTRICT",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="planned"),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("manifest_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("policy_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("include_drafts", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("plan_options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _fk_column(
            "parent_publication_id",
            "graph_publications.id",
            "fk_graph_publications_parent",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "rollback_target_publication_id",
            "graph_publications.id",
            "fk_graph_publications_rollback_target",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "planned_by_user_id",
            "sys_users.id",
            "fk_graph_publications_planned_by",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "activated_by_user_id",
            "sys_users.id",
            "fk_graph_publications_activated_by",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "cancelled_by_user_id",
            "sys_users.id",
            "fk_graph_publications_cancelled_by",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "superseded_by_publication_id",
            "graph_publications.id",
            "fk_graph_publications_superseded_by",
            "SET NULL",
            nullable=True,
        ),
        sa.Column("entity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_counts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("blocked_diagnostics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("item_hashes_summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('planned','activating','active','degraded','superseded','cancelled','failed')",
            name="ck_graph_publications_status",
        ),
        sa.CheckConstraint(
            "source_mode IN ('initial_seed','manual_plan','rollback')",
            name="ck_graph_publications_source_mode",
        ),
        sa.CheckConstraint(
            "entity_count >= 0 AND relation_count >= 0",
            name="ck_graph_publications_counts",
        ),
    )
    op.create_index(
        "uq_graph_publications_current_scope",
        table,
        ["library_id", "ontology_version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active','degraded')"),
    )
    op.create_index(
        "uq_graph_publications_reusable_manifest",
        table,
        ["library_id", "ontology_version_id", "manifest_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('planned','activating','active','degraded')"),
    )
    op.create_index(
        "uq_graph_publications_nonterminal_command",
        table,
        ["library_id", "ontology_version_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('planned','activating')"),
    )
    op.create_index(
        "ix_graph_publications_library_ontology_status",
        table,
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index(
        "ix_graph_publications_library_status_updated",
        table,
        ["library_id", "status", "updated_at"],
    )
    op.create_index("ix_graph_publications_parent", table, ["parent_publication_id"])
    op.create_index(
        "ix_graph_publications_rollback_target",
        table,
        ["rollback_target_publication_id"],
    )


def _create_graph_publication_items() -> None:
    table = "graph_publication_items"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "publication_id",
            "graph_publications.id",
            "fk_graph_publication_items_publication",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_publication_items_library",
            "CASCADE",
        ),
        _fk_column(
            "ontology_version_id",
            "ontology_versions.id",
            "fk_graph_publication_items_ontology",
            "RESTRICT",
        ),
        sa.Column("item_kind", sa.String(length=16), nullable=False),
        _fk_column(
            "entity_id",
            "entities.id",
            "fk_graph_publication_items_entity",
            "RESTRICT",
            nullable=True,
        ),
        _fk_column(
            "relation_id",
            "knowledge_relations.id",
            "fk_graph_publication_items_relation",
            "RESTRICT",
            nullable=True,
        ),
        sa.Column("item_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="planned"),
        sa.Column("support_evidence_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("support_counts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fact_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "item_kind IN ('entity','relation')",
            name="ck_graph_publication_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('planned','active','stale','degraded','superseded')",
            name="ck_graph_publication_items_status",
        ),
        sa.CheckConstraint(
            "((item_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL) OR "
            "(item_kind = 'relation' AND relation_id IS NOT NULL AND entity_id IS NULL))",
            name="ck_graph_publication_items_exact_target",
        ),
    )
    op.create_index(
        "uq_graph_publication_items_publication_entity",
        table,
        ["publication_id", "item_kind", "entity_id"],
        unique=True,
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )
    op.create_index(
        "uq_graph_publication_items_publication_relation",
        table,
        ["publication_id", "item_kind", "relation_id"],
        unique=True,
        postgresql_where=sa.text("relation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_graph_publication_items_publication_hash",
        table,
        ["publication_id", "item_hash"],
        unique=True,
    )
    op.create_index(
        "ix_graph_publication_items_scope_kind_status",
        table,
        ["library_id", "ontology_version_id", "item_kind", "status"],
    )
    op.create_index("ix_graph_publication_items_entity", table, ["entity_id"])
    op.create_index("ix_graph_publication_items_relation", table, ["relation_id"])


def downgrade() -> None:
    op.drop_table("graph_publication_items")
    op.drop_table("graph_publications")
