from __future__ import annotations

from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


MIGRATION = Path("alembic/versions/0019_v03_m1_ontology_schema.py")


def _unique_column_sets(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _index_column_sets(table) -> set[tuple[str, ...]]:
    return {tuple(column.name for column in index.columns) for index in table.indexes}


def test_v03_m1_models_are_exported():
    from app.models import (
        AttributeDefinition,
        EntityType,
        OntologyVersion,
        RelationType,
        RelationTypeConstraint,
    )

    assert OntologyVersion.__tablename__ == "ontology_versions"
    assert EntityType.__tablename__ == "entity_types"
    assert RelationType.__tablename__ == "relation_types"
    assert RelationTypeConstraint.__tablename__ == "relation_type_constraints"
    assert AttributeDefinition.__tablename__ == "attribute_definitions"


def test_ontology_versions_columns_constraints_and_indexes():
    from app.models.ontology_version import OntologyVersion

    table = OntologyVersion.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "version_key",
        "version_no",
        "status",
        "description",
        "parent_version_id",
        "published_at",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.library_id.nullable is False
    assert cols.version_key.type.length == 128
    assert cols.version_no.nullable is False
    assert cols.status.type.length == 32
    assert cols.description.nullable is True
    assert cols.parent_version_id.nullable is True

    assert ("library_id", "version_key", "version_no") in _unique_column_sets(table)
    indexes = _index_column_sets(table)
    assert ("library_id", "status") in indexes
    assert ("library_id", "version_key", "status") in indexes
    assert any(index.name == "uq_ontology_versions_library_version_key_active" for index in table.indexes)


def test_entity_types_columns_constraints_and_indexes():
    from app.models.entity_type import EntityType

    table = EntityType.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "key",
        "label",
        "description",
        "properties_schema",
        "is_seeded",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.library_id.nullable is False
    assert cols.ontology_version_id.nullable is False
    assert cols.key.type.length == 128
    assert cols.label.type.length == 255
    assert isinstance(cols.properties_schema.type, JSONB)
    assert cols.is_seeded.nullable is False
    assert str(cols.is_seeded.server_default.arg) == "false"
    assert ("library_id", "ontology_version_id", "key") in _unique_column_sets(table)
    assert ("library_id", "ontology_version_id", "status") in _index_column_sets(table)


def test_relation_types_columns_constraints_and_indexes():
    from app.models.relation_type import RelationType

    table = RelationType.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "key",
        "label",
        "description",
        "direction",
        "requires_evidence",
        "default_review_policy",
        "properties_schema",
        "is_seeded",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.direction.type.length == 32
    assert cols.requires_evidence.nullable is False
    assert str(cols.requires_evidence.server_default.arg) == "true"
    assert cols.default_review_policy.type.length == 32
    assert isinstance(cols.properties_schema.type, JSONB)
    assert ("library_id", "ontology_version_id", "key") in _unique_column_sets(table)
    assert ("library_id", "ontology_version_id", "status") in _index_column_sets(table)


def test_relation_type_constraints_columns_constraints_and_indexes():
    from app.models.relation_type_constraint import RelationTypeConstraint

    table = RelationTypeConstraint.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "relation_type_id",
        "source_entity_type_id",
        "target_entity_type_id",
        "cardinality",
        "requires_review",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.cardinality.nullable is True
    assert cols.requires_review.nullable is False
    assert str(cols.requires_review.server_default.arg) == "false"
    assert (
        "library_id",
        "ontology_version_id",
        "relation_type_id",
        "source_entity_type_id",
        "target_entity_type_id",
    ) in _unique_column_sets(table)
    indexes = _index_column_sets(table)
    assert ("library_id", "relation_type_id") in indexes
    assert ("library_id", "source_entity_type_id", "target_entity_type_id") in indexes


def test_attribute_definitions_columns_constraints_indexes_and_polymorphic_owner():
    from app.models.attribute_definition import AttributeDefinition

    table = AttributeDefinition.__table__
    cols = table.c

    for name in (
        "id",
        "library_id",
        "ontology_version_id",
        "owner_kind",
        "owner_type_id",
        "key",
        "label",
        "value_type",
        "required",
        "enum_values",
        "validation_schema",
        "indexed",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols

    assert cols.owner_kind.type.length == 32
    assert cols.owner_type_id.nullable is False
    assert cols.value_type.type.length == 32
    assert isinstance(cols.enum_values.type, JSONB)
    assert isinstance(cols.validation_schema.type, JSONB)
    assert str(cols.required.server_default.arg) == "false"
    assert str(cols.indexed.server_default.arg) == "false"
    assert (
        "library_id",
        "ontology_version_id",
        "owner_kind",
        "owner_type_id",
        "key",
    ) in _unique_column_sets(table)
    assert ("library_id", "ontology_version_id", "owner_kind") in _index_column_sets(table)
    assert not list(cols.owner_type_id.foreign_keys)


def test_v03_m1_migration_declares_current_head_and_only_m1_tables():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0019"' in text
    assert 'down_revision: Union[str, None] = "0018"' in text

    for table_name in (
        "ontology_versions",
        "entity_types",
        "relation_types",
        "relation_type_constraints",
        "attribute_definitions",
    ):
        assert f'op.create_table("{table_name}"' in text
        assert f'op.drop_table("{table_name}")' in text

    forbidden = (
        "evidence_units",
        "entities",
        "entity_aliases",
        "entity_mentions",
        "knowledge_relations",
        "relation_evidence",
        "chunks",
        "embedding_jobs",
        "document_revision_id",
        "document_revision",
    )
    for name in forbidden:
        assert name not in text

    assert "op.add_column" not in text
    assert "op.alter_column" not in text
