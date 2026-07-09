"""v0.3 m1 ontology schema tables

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("ontology_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_key", sa.String(length=128), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ontology_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_ontology_versions_status",
        ),
        sa.UniqueConstraint(
            "library_id",
            "version_key",
            "version_no",
            name="uq_ontology_versions_library_key_version_no",
        ),
    )
    op.create_index("ix_ontology_versions_library_id", "ontology_versions", ["library_id"])
    op.create_index("ix_ontology_versions_library_status", "ontology_versions", ["library_id", "status"])
    op.create_index(
        "ix_ontology_versions_library_key_status",
        "ontology_versions",
        ["library_id", "version_key", "status"],
    )
    op.create_index(
        "uq_ontology_versions_library_version_key_active",
        "ontology_versions",
        ["library_id", "version_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table("entity_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ontology_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ontology_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("properties_schema", postgresql.JSONB(), nullable=True),
        sa.Column("is_seeded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_entity_types_status",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "key",
            name="uq_entity_types_library_ontology_key",
        ),
    )
    op.create_index("ix_entity_types_library_id", "entity_types", ["library_id"])
    op.create_index("ix_entity_types_ontology_version_id", "entity_types", ["ontology_version_id"])
    op.create_index(
        "ix_entity_types_library_ontology_status",
        "entity_types",
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index("ix_entity_types_library_key", "entity_types", ["library_id", "key"])

    op.create_table("relation_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ontology_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ontology_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("requires_evidence", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("default_review_policy", sa.String(length=32), nullable=False),
        sa.Column("properties_schema", postgresql.JSONB(), nullable=True),
        sa.Column("is_seeded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_relation_types_status",
        ),
        sa.CheckConstraint(
            "direction IN ('directed','undirected')",
            name="ck_relation_types_direction",
        ),
        sa.CheckConstraint(
            "default_review_policy IN ('auto_active','pending_review','manual_only')",
            name="ck_relation_types_review_policy",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "key",
            name="uq_relation_types_library_ontology_key",
        ),
    )
    op.create_index("ix_relation_types_library_id", "relation_types", ["library_id"])
    op.create_index("ix_relation_types_ontology_version_id", "relation_types", ["ontology_version_id"])
    op.create_index(
        "ix_relation_types_library_ontology_status",
        "relation_types",
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index("ix_relation_types_library_key", "relation_types", ["library_id", "key"])

    op.create_table("relation_type_constraints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ontology_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ontology_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relation_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("relation_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_entity_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cardinality", sa.String(length=32), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_relation_type_constraints_status",
        ),
        sa.CheckConstraint(
            "cardinality IS NULL OR cardinality IN ('one_to_one','one_to_many','many_to_one','many_to_many')",
            name="ck_relation_type_constraints_cardinality",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "relation_type_id",
            "source_entity_type_id",
            "target_entity_type_id",
            name="uq_relation_type_constraints_scope",
        ),
    )
    op.create_index(
        "ix_relation_type_constraints_library_id",
        "relation_type_constraints",
        ["library_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_ontology_version_id",
        "relation_type_constraints",
        ["ontology_version_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_relation_type_id",
        "relation_type_constraints",
        ["relation_type_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_source_entity_type_id",
        "relation_type_constraints",
        ["source_entity_type_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_target_entity_type_id",
        "relation_type_constraints",
        ["target_entity_type_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_library_ontology_status",
        "relation_type_constraints",
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index(
        "ix_relation_type_constraints_library_relation",
        "relation_type_constraints",
        ["library_id", "relation_type_id"],
    )
    op.create_index(
        "ix_relation_type_constraints_library_source_target",
        "relation_type_constraints",
        ["library_id", "source_entity_type_id", "target_entity_type_id"],
    )

    op.create_table("attribute_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ontology_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ontology_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("value_type", sa.String(length=32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enum_values", postgresql.JSONB(), nullable=True),
        sa.Column("validation_schema", postgresql.JSONB(), nullable=True),
        sa.Column("indexed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','deleted')",
            name="ck_attribute_definitions_status",
        ),
        sa.CheckConstraint(
            "owner_kind IN ('entity_type','relation_type')",
            name="ck_attribute_definitions_owner_kind",
        ),
        sa.CheckConstraint(
            "value_type IN ('string','text','integer','number','boolean','date','datetime','enum','json')",
            name="ck_attribute_definitions_value_type",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "owner_kind",
            "owner_type_id",
            "key",
            name="uq_attribute_definitions_scope_key",
        ),
    )
    op.create_index("ix_attribute_definitions_library_id", "attribute_definitions", ["library_id"])
    op.create_index(
        "ix_attribute_definitions_ontology_version_id",
        "attribute_definitions",
        ["ontology_version_id"],
    )
    op.create_index(
        "ix_attribute_definitions_library_ontology_owner",
        "attribute_definitions",
        ["library_id", "ontology_version_id", "owner_kind"],
    )
    op.create_index(
        "ix_attribute_definitions_library_owner",
        "attribute_definitions",
        ["library_id", "owner_type_id"],
    )
    op.create_index(
        "ix_attribute_definitions_library_status",
        "attribute_definitions",
        ["library_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_attribute_definitions_library_status", table_name="attribute_definitions")
    op.drop_index("ix_attribute_definitions_library_owner", table_name="attribute_definitions")
    op.drop_index("ix_attribute_definitions_library_ontology_owner", table_name="attribute_definitions")
    op.drop_index("ix_attribute_definitions_ontology_version_id", table_name="attribute_definitions")
    op.drop_index("ix_attribute_definitions_library_id", table_name="attribute_definitions")
    op.drop_table("attribute_definitions")

    op.drop_index(
        "ix_relation_type_constraints_library_source_target",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_library_relation",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_library_ontology_status",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_target_entity_type_id",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_source_entity_type_id",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_relation_type_id",
        table_name="relation_type_constraints",
    )
    op.drop_index(
        "ix_relation_type_constraints_ontology_version_id",
        table_name="relation_type_constraints",
    )
    op.drop_index("ix_relation_type_constraints_library_id", table_name="relation_type_constraints")
    op.drop_table("relation_type_constraints")

    op.drop_index("ix_relation_types_library_key", table_name="relation_types")
    op.drop_index("ix_relation_types_library_ontology_status", table_name="relation_types")
    op.drop_index("ix_relation_types_ontology_version_id", table_name="relation_types")
    op.drop_index("ix_relation_types_library_id", table_name="relation_types")
    op.drop_table("relation_types")

    op.drop_index("ix_entity_types_library_key", table_name="entity_types")
    op.drop_index("ix_entity_types_library_ontology_status", table_name="entity_types")
    op.drop_index("ix_entity_types_ontology_version_id", table_name="entity_types")
    op.drop_index("ix_entity_types_library_id", table_name="entity_types")
    op.drop_table("entity_types")

    op.drop_index("uq_ontology_versions_library_version_key_active", table_name="ontology_versions")
    op.drop_index("ix_ontology_versions_library_key_status", table_name="ontology_versions")
    op.drop_index("ix_ontology_versions_library_status", table_name="ontology_versions")
    op.drop_index("ix_ontology_versions_library_id", table_name="ontology_versions")
    op.drop_table("ontology_versions")
