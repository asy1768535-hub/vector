"""v0.3 m2 graph fact tables

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("entities",
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
            sa.ForeignKey("ontology_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "entity_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_name", sa.String(length=512), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("authority_level", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','pending_review','active','rejected','stale','disabled','deleted')",
            name="ck_entities_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_entities_source_type",
        ),
        sa.UniqueConstraint(
            "library_id",
            "ontology_version_id",
            "entity_type_id",
            "normalized_name",
            name="uq_entities_library_ontology_type_normalized",
        ),
    )
    op.create_index("ix_entities_library_id", "entities", ["library_id"])
    op.create_index("ix_entities_ontology_version_id", "entities", ["ontology_version_id"])
    op.create_index("ix_entities_entity_type_id", "entities", ["entity_type_id"])
    op.create_index(
        "ix_entities_library_ontology_status",
        "entities",
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index(
        "ix_entities_library_type_status",
        "entities",
        ["library_id", "entity_type_id", "status"],
    )

    op.create_table("entity_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(length=512), nullable=False),
        sa.Column("normalized_alias", sa.String(length=512), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review','active','rejected','disabled','deleted')",
            name="ck_entity_aliases_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_entity_aliases_source_type",
        ),
        sa.UniqueConstraint(
            "library_id",
            "entity_id",
            "normalized_alias",
            name="uq_entity_aliases_library_entity_normalized",
        ),
    )
    op.create_index("ix_entity_aliases_library_id", "entity_aliases", ["library_id"])
    op.create_index("ix_entity_aliases_entity_id", "entity_aliases", ["entity_id"])
    op.create_index("ix_entity_aliases_library_status", "entity_aliases", ["library_id", "status"])
    op.create_index("ix_entity_aliases_library_alias", "entity_aliases", ["library_id", "normalized_alias"])

    op.create_table("entity_mentions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mention_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.String(length=512), nullable=True),
        sa.Column("quote_text", sa.Text(), nullable=True),
        sa.Column("evidence_text_snapshot", sa.Text(), nullable=True),
        sa.Column("source_span", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','stale','deleted')",
            name="ck_entity_mentions_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_entity_mentions_source_type",
        ),
    )
    op.create_index("ix_entity_mentions_library_id", "entity_mentions", ["library_id"])
    op.create_index("ix_entity_mentions_entity_id", "entity_mentions", ["entity_id"])
    op.create_index("ix_entity_mentions_evidence_id", "entity_mentions", ["evidence_id"])
    op.create_index("ix_entity_mentions_document_id", "entity_mentions", ["document_id"])
    op.create_index("ix_entity_mentions_document_revision_id", "entity_mentions", ["document_revision_id"])
    op.create_index(
        "ix_entity_mentions_library_entity_status",
        "entity_mentions",
        ["library_id", "entity_id", "status"],
    )
    op.create_index(
        "ix_entity_mentions_library_evidence",
        "entity_mentions",
        ["library_id", "evidence_id"],
    )
    op.create_index(
        "ix_entity_mentions_library_revision_status",
        "entity_mentions",
        ["library_id", "document_revision_id", "status"],
    )

    op.create_table("knowledge_relations",
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
            sa.ForeignKey("ontology_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "relation_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("relation_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("authority_level", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','pending_review','active','rejected','stale','disabled','deleted')",
            name="ck_knowledge_relations_status",
        ),
        sa.CheckConstraint(
            "review_status IS NULL OR review_status IN ('pending_review','approved','rejected','not_required')",
            name="ck_knowledge_relations_review_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual','imported','extracted')",
            name="ck_knowledge_relations_source_type",
        ),
    )
    op.create_index("ix_knowledge_relations_library_id", "knowledge_relations", ["library_id"])
    op.create_index("ix_knowledge_relations_ontology_version_id", "knowledge_relations", ["ontology_version_id"])
    op.create_index("ix_knowledge_relations_relation_type_id", "knowledge_relations", ["relation_type_id"])
    op.create_index("ix_knowledge_relations_source_entity_id", "knowledge_relations", ["source_entity_id"])
    op.create_index("ix_knowledge_relations_target_entity_id", "knowledge_relations", ["target_entity_id"])
    op.create_index(
        "ix_knowledge_relations_library_ontology_status",
        "knowledge_relations",
        ["library_id", "ontology_version_id", "status"],
    )
    op.create_index(
        "ix_knowledge_relations_library_source_relation",
        "knowledge_relations",
        ["library_id", "source_entity_id", "relation_type_id"],
    )
    op.create_index(
        "ix_knowledge_relations_library_target_relation",
        "knowledge_relations",
        ["library_id", "target_entity_id", "relation_type_id"],
    )

    op.create_table("relation_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sys_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_relations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("support_type", sa.String(length=32), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=True),
        sa.Column("evidence_text_snapshot", sa.Text(), nullable=True),
        sa.Column("source_span", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "support_type IN ('supports','contradicts','mentions','source')",
            name="ck_relation_evidence_support_type",
        ),
        sa.CheckConstraint(
            "status IN ('active','stale','deleted')",
            name="ck_relation_evidence_status",
        ),
        sa.UniqueConstraint(
            "library_id",
            "relation_id",
            "evidence_id",
            name="uq_relation_evidence_library_relation_evidence",
        ),
    )
    op.create_index("ix_relation_evidence_library_id", "relation_evidence", ["library_id"])
    op.create_index("ix_relation_evidence_relation_id", "relation_evidence", ["relation_id"])
    op.create_index("ix_relation_evidence_evidence_id", "relation_evidence", ["evidence_id"])
    op.create_index("ix_relation_evidence_document_id", "relation_evidence", ["document_id"])
    op.create_index("ix_relation_evidence_document_revision_id", "relation_evidence", ["document_revision_id"])
    op.create_index(
        "ix_relation_evidence_library_relation_status",
        "relation_evidence",
        ["library_id", "relation_id", "status"],
    )
    op.create_index(
        "ix_relation_evidence_library_evidence",
        "relation_evidence",
        ["library_id", "evidence_id"],
    )
    op.create_index(
        "ix_relation_evidence_library_revision_status",
        "relation_evidence",
        ["library_id", "document_revision_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_relation_evidence_library_revision_status", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_library_evidence", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_library_relation_status", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_document_revision_id", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_document_id", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_evidence_id", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_relation_id", table_name="relation_evidence")
    op.drop_index("ix_relation_evidence_library_id", table_name="relation_evidence")
    op.drop_table("relation_evidence")

    op.drop_index("ix_knowledge_relations_library_target_relation", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_library_source_relation", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_library_ontology_status", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_target_entity_id", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_source_entity_id", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_relation_type_id", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_ontology_version_id", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_library_id", table_name="knowledge_relations")
    op.drop_table("knowledge_relations")

    op.drop_index("ix_entity_mentions_library_revision_status", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_library_evidence", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_library_entity_status", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_document_revision_id", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_document_id", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_evidence_id", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_entity_id", table_name="entity_mentions")
    op.drop_index("ix_entity_mentions_library_id", table_name="entity_mentions")
    op.drop_table("entity_mentions")

    op.drop_index("ix_entity_aliases_library_alias", table_name="entity_aliases")
    op.drop_index("ix_entity_aliases_library_status", table_name="entity_aliases")
    op.drop_index("ix_entity_aliases_entity_id", table_name="entity_aliases")
    op.drop_index("ix_entity_aliases_library_id", table_name="entity_aliases")
    op.drop_table("entity_aliases")

    op.drop_index("ix_entities_library_type_status", table_name="entities")
    op.drop_index("ix_entities_library_ontology_status", table_name="entities")
    op.drop_index("ix_entities_entity_type_id", table_name="entities")
    op.drop_index("ix_entities_ontology_version_id", table_name="entities")
    op.drop_index("ix_entities_library_id", table_name="entities")
    op.drop_table("entities")
