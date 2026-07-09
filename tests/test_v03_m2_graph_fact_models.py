from __future__ import annotations

from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


MIGRATION = Path("alembic/versions/0020_v03_m2_graph_fact_tables.py")


def _unique_column_sets(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _index_column_sets(table) -> set[tuple[str, ...]]:
    return {tuple(column.name for column in index.columns) for index in table.indexes}


def _foreign_key_targets(column) -> set[str]:
    return {f"{fk.column.table.name}.{fk.column.name}" for fk in column.foreign_keys}


def test_v03_m2_models_are_exported():
    from app.models import Entity, EntityAlias, EntityMention, KnowledgeRelation, RelationEvidence

    assert Entity.__tablename__ == "entities"
    assert EntityAlias.__tablename__ == "entity_aliases"
    assert EntityMention.__tablename__ == "entity_mentions"
    assert KnowledgeRelation.__tablename__ == "knowledge_relations"
    assert RelationEvidence.__tablename__ == "relation_evidence"


def test_entities_reuse_m1_ontology_schema_models():
    from app.models.entity import Entity

    table = Entity.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "entity_type_id",
        "canonical_name",
        "normalized_name",
        "properties",
        "status",
        "source_type",
        "authority_level",
        "confidence",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.library_id.nullable is False
    assert _foreign_key_targets(cols.library_id) == {"sys_libraries.id"}
    assert _foreign_key_targets(cols.ontology_version_id) == {"ontology_versions.id"}
    assert _foreign_key_targets(cols.entity_type_id) == {"entity_types.id"}
    assert isinstance(cols.properties.type, JSONB)
    assert ("library_id", "ontology_version_id", "entity_type_id", "normalized_name") in _unique_column_sets(table)


def test_entity_aliases_are_scoped_to_entities():
    from app.models.entity_alias import EntityAlias

    table = EntityAlias.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "entity_id",
        "alias",
        "normalized_alias",
        "source_type",
        "confidence",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.library_id.nullable is False
    assert _foreign_key_targets(cols.library_id) == {"sys_libraries.id"}
    assert _foreign_key_targets(cols.entity_id) == {"entities.id"}
    assert ("library_id", "entity_id", "normalized_alias") in _unique_column_sets(table)


def test_entity_mentions_bind_to_evidence_units_with_denormalized_snapshot_fields():
    from app.models.entity_mention import EntityMention

    table = EntityMention.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "entity_id",
        "evidence_id",
        "document_id",
        "document_revision_id",
        "chunk_id",
        "mention_text",
        "normalized_text",
        "quote_text",
        "evidence_text_snapshot",
        "source_span",
        "confidence",
        "source_type",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert _foreign_key_targets(cols.entity_id) == {"entities.id"}
    assert _foreign_key_targets(cols.evidence_id) == {"evidence_units.id"}
    assert cols.evidence_id.nullable is False
    assert cols.document_id.nullable is False
    assert cols.document_revision_id.nullable is False
    assert cols.chunk_id.nullable is True
    assert not list(cols.chunk_id.foreign_keys)
    assert isinstance(cols.source_span.type, JSONB)
    indexes = _index_column_sets(table)
    assert ("library_id", "entity_id", "status") in indexes
    assert ("library_id", "evidence_id") in indexes
    assert ("library_id", "document_revision_id", "status") in indexes


def test_knowledge_relations_reuse_m1_relation_types_and_entity_rows():
    from app.models.knowledge_relation import KnowledgeRelation

    table = KnowledgeRelation.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "relation_type_id",
        "source_entity_id",
        "target_entity_id",
        "properties",
        "status",
        "review_status",
        "source_type",
        "authority_level",
        "confidence",
        "valid_from",
        "valid_to",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert _foreign_key_targets(cols.ontology_version_id) == {"ontology_versions.id"}
    assert _foreign_key_targets(cols.relation_type_id) == {"relation_types.id"}
    assert _foreign_key_targets(cols.source_entity_id) == {"entities.id"}
    assert _foreign_key_targets(cols.target_entity_id) == {"entities.id"}
    assert isinstance(cols.properties.type, JSONB)
    indexes = _index_column_sets(table)
    assert ("library_id", "ontology_version_id", "status") in indexes
    assert ("library_id", "source_entity_id", "relation_type_id") in indexes
    assert ("library_id", "target_entity_id", "relation_type_id") in indexes


def test_relation_evidence_binds_to_evidence_units_with_denormalized_snapshot_fields():
    from app.models.relation_evidence import RelationEvidence

    table = RelationEvidence.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "relation_id",
        "evidence_id",
        "document_id",
        "document_revision_id",
        "chunk_id",
        "support_type",
        "quote_text",
        "evidence_text_snapshot",
        "source_span",
        "confidence",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert _foreign_key_targets(cols.relation_id) == {"knowledge_relations.id"}
    assert _foreign_key_targets(cols.evidence_id) == {"evidence_units.id"}
    assert cols.evidence_id.nullable is False
    assert cols.document_id.nullable is False
    assert cols.document_revision_id.nullable is False
    assert cols.chunk_id.nullable is True
    assert not list(cols.chunk_id.foreign_keys)
    assert isinstance(cols.source_span.type, JSONB)
    assert ("library_id", "relation_id", "evidence_id") in _unique_column_sets(table)
    indexes = _index_column_sets(table)
    assert ("library_id", "relation_id", "status") in indexes
    assert ("library_id", "evidence_id") in indexes
    assert ("library_id", "document_revision_id", "status") in indexes


def test_v03_m2_migration_declares_current_head_and_only_graph_fact_tables():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0020"' in text
    assert 'down_revision: Union[str, None] = "0019"' in text

    for table_name in (
        "entities",
        "entity_aliases",
        "entity_mentions",
        "knowledge_relations",
        "relation_evidence",
    ):
        assert f'op.create_table("{table_name}"' in text
        assert f'op.drop_table("{table_name}")' in text

    assert 'sa.ForeignKey("ontology_versions.id"' in text
    assert 'sa.ForeignKey("entity_types.id"' in text
    assert 'sa.ForeignKey("relation_types.id"' in text
    assert 'sa.ForeignKey("evidence_units.id"' in text
    assert 'sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True)' in text
    assert 'sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False)' in text

    forbidden = (
        'op.add_column("evidence_units"',
        'op.alter_column("evidence_units"',
        'op.add_column("documents"',
        'op.alter_column("documents"',
        'op.add_column("embedding_jobs"',
        'op.alter_column("embedding_jobs"',
        'sa.Column("document_revision",',
        "app/api/chat.py",
        "Qdrant",
        "retrieval",
    )
    for needle in forbidden:
        assert needle not in text
