"""v0.9 Schema lifecycle actions.

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schema_lifecycle_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_kind", sa.String(length=32), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_state_hash", sa.String(length=64), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "result_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_kind IN ('clone_version','create_item','update_item',"
            "'disable_item','activate_version')",
            name="ck_schema_lifecycle_actions_kind",
        ),
        sa.CheckConstraint(
            "target_kind IN ('ontology_version','entity_type','relation_type',"
            "'attribute','constraint')",
            name="ck_schema_lifecycle_actions_target_kind",
        ),
        sa.CheckConstraint(
            "expected_state_hash ~ '^[0-9a-f]{64}$' AND "
            "command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_schema_lifecycle_actions_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_payload) = 'object' AND "
            "octet_length(result_payload::text) <= 65536",
            name="ck_schema_lifecycle_actions_payload",
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> ''",
            name="ck_schema_lifecycle_actions_idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["sys_users.id"],
            name="fk_schema_lifecycle_actions_actor",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            name="fk_schema_lifecycle_actions_library",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"],
            ["ontology_versions.id"],
            name="fk_schema_lifecycle_actions_ontology",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schema_lifecycle_actions"),
        sa.UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_schema_lifecycle_actions_library_key",
        ),
    )
    op.create_index(
        "ix_schema_lifecycle_actions_scope_created",
        "schema_lifecycle_actions",
        ["library_id", "ontology_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_schema_lifecycle_actions_target",
        "schema_lifecycle_actions",
        ["library_id", "target_kind", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_schema_lifecycle_actions_target",
        table_name="schema_lifecycle_actions",
    )
    op.drop_index(
        "ix_schema_lifecycle_actions_scope_created",
        table_name="schema_lifecycle_actions",
    )
    op.drop_table("schema_lifecycle_actions")
