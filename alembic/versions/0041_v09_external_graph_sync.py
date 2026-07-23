"""v0.9 external graph fact synchronization

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "graph_sync_source_policies",
        sa.Column("id", UUID, nullable=False),
        sa.Column("library_id", UUID, nullable=False),
        sa.Column("sync_source_id", UUID, nullable=False),
        sa.Column("authority_rank", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stale_after_seconds", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("authority_rank BETWEEN 1 AND 999", name="ck_graph_sync_policies_rank"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_graph_sync_policies_status"),
        sa.CheckConstraint(
            "stale_after_seconds BETWEEN 60 AND 31536000", name="ck_graph_sync_policies_stale"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_source_id"], ["sync_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_source_id", name="uq_graph_sync_policies_source"),
    )
    op.create_index(
        "ix_graph_sync_policies_library_status",
        "graph_sync_source_policies",
        ["library_id", "status"],
    )

    op.create_table(
        "graph_external_sync_operations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("library_id", UUID, nullable=False),
        sa.Column("sync_source_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=True),
        sa.Column("snapshot_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deleted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stale_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "result_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("requested_by_user_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing','applied','conflicted','failed')",
            name="ck_graph_external_sync_operations_status",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND item_count BETWEEN 1 AND 100 "
            "AND created_count >= 0 AND updated_count >= 0 AND unchanged_count >= 0 "
            "AND deleted_count >= 0 AND conflict_count >= 0 AND stale_count >= 0",
            name="ck_graph_external_sync_operations_values",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_payload) = 'object' "
            "AND octet_length(result_payload::text) <= 131072",
            name="ck_graph_external_sync_operations_result",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_source_id"], ["sync_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["sys_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "sync_source_id",
            "idempotency_key",
            name="uq_graph_external_sync_operations_key",
        ),
    )
    op.create_index(
        "ix_graph_external_sync_operations_scope_created",
        "graph_external_sync_operations",
        ["library_id", "sync_source_id", "created_at"],
    )
    op.create_index(
        "uq_graph_external_sync_operations_source_event",
        "graph_external_sync_operations",
        ["library_id", "sync_source_id", "source_event_id"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )

    op.create_table(
        "graph_external_fact_mappings",
        sa.Column("id", UUID, nullable=False),
        sa.Column("library_id", UUID, nullable=False),
        sa.Column("sync_source_id", UUID, nullable=False),
        sa.Column("fact_kind", sa.String(16), nullable=False),
        sa.Column("external_type", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("entity_id", UUID, nullable=True),
        sa.Column("relation_id", UUID, nullable=True),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=True),
        sa.Column("source_event_id", sa.String(128), nullable=True),
        sa.Column("snapshot_id", sa.String(128), nullable=True),
        sa.Column("evidence_id", UUID, nullable=True),
        sa.Column(
            "source_locator",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_operation_id", UUID, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("fact_kind IN ('entity','relation')", name="ck_graph_external_mappings_kind"),
        sa.CheckConstraint(
            "lifecycle IN ('active','stale','tombstoned')",
            name="ck_graph_external_mappings_lifecycle",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name="ck_graph_external_mappings_hash"
        ),
        sa.CheckConstraint(
            "((fact_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL) OR "
            "(fact_kind = 'relation' AND relation_id IS NOT NULL AND entity_id IS NULL))",
            name="ck_graph_external_mappings_target",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_locator) = 'object' AND octet_length(source_locator::text) <= 8192",
            name="ck_graph_external_mappings_locator",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_source_id"], ["sync_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["relation_id"], ["knowledge_relations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence_units.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["last_operation_id"], ["graph_external_sync_operations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "sync_source_id",
            "fact_kind",
            "external_type",
            "external_id",
            name="uq_graph_external_mappings_identity",
        ),
    )
    for name, columns in (
        ("ix_graph_external_mappings_entity", ["entity_id"]),
        ("ix_graph_external_mappings_relation", ["relation_id"]),
        (
            "ix_graph_external_mappings_source_lifecycle",
            ["library_id", "sync_source_id", "lifecycle"],
        ),
    ):
        op.create_index(name, "graph_external_fact_mappings", columns)

    op.create_table(
        "graph_external_sync_conflicts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("library_id", UUID, nullable=False),
        sa.Column("sync_source_id", UUID, nullable=False),
        sa.Column("mapping_id", UUID, nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("incoming_hash", sa.String(64), nullable=False),
        sa.Column("current_hash", sa.String(64), nullable=True),
        sa.Column("incoming_authority_rank", sa.Integer(), nullable=False),
        sa.Column("current_authority_rank", sa.Integer(), nullable=True),
        sa.Column(
            "proposal", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason_code IN ('manual_authority','stronger_source_authority',"
            "'equal_authority_divergence','published_fact_change','published_fact_delete',"
            "'relation_dependency','source_snapshot_stale')",
            name="ck_graph_external_sync_conflicts_reason",
        ),
        sa.CheckConstraint(
            "status IN ('open','resolved','dismissed')",
            name="ck_graph_external_sync_conflicts_status",
        ),
        sa.CheckConstraint(
            "incoming_hash ~ '^[0-9a-f]{64}$' "
            "AND (current_hash IS NULL OR current_hash ~ '^[0-9a-f]{64}$')",
            name="ck_graph_external_sync_conflicts_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proposal) = 'object' AND octet_length(proposal::text) <= 65536",
            name="ck_graph_external_sync_conflicts_proposal",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_source_id"], ["sync_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["mapping_id"], ["graph_external_fact_mappings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["graph_external_sync_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_graph_external_sync_conflicts_open",
        "graph_external_sync_conflicts",
        ["mapping_id", "reason_code", "incoming_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_graph_external_sync_conflicts_scope_status",
        "graph_external_sync_conflicts",
        ["library_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_external_sync_conflicts_scope_status",
        table_name="graph_external_sync_conflicts",
    )
    op.drop_index(
        "uq_graph_external_sync_conflicts_open",
        table_name="graph_external_sync_conflicts",
    )
    op.drop_table("graph_external_sync_conflicts")
    for name in (
        "ix_graph_external_mappings_source_lifecycle",
        "ix_graph_external_mappings_relation",
        "ix_graph_external_mappings_entity",
    ):
        op.drop_index(name, table_name="graph_external_fact_mappings")
    op.drop_table("graph_external_fact_mappings")
    op.drop_index(
        "uq_graph_external_sync_operations_source_event",
        table_name="graph_external_sync_operations",
    )
    op.drop_index(
        "ix_graph_external_sync_operations_scope_created",
        table_name="graph_external_sync_operations",
    )
    op.drop_table("graph_external_sync_operations")
    op.drop_index(
        "ix_graph_sync_policies_library_status",
        table_name="graph_sync_source_policies",
    )
    op.drop_table("graph_sync_source_policies")
