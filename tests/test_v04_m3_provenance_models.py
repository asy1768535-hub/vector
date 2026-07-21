from __future__ import annotations

from sqlalchemy import Index


def _assert_fk(table, column_name: str, constraint_name: str) -> None:
    foreign_keys = list(table.c[column_name].foreign_keys)
    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert foreign_key.target_fullname == "graph_extraction_jobs.id"
    assert foreign_key.constraint.name == constraint_name
    assert foreign_key.ondelete == "SET NULL"


def _index(table, name: str) -> Index:
    for index in table.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"missing index {name}")


def _assert_partial_unique_extraction_key(table, name: str) -> None:
    index = _index(table, name)
    assert index.unique is True
    assert tuple(column.name for column in index.columns) == ("extraction_key",)
    predicate = index.dialect_options["postgresql"]["where"]
    assert str(predicate).lower() == "extraction_key is not null"


def test_entities_have_nullable_job_provenance_with_named_set_null_fk():
    from app.models.entity import Entity

    table = Entity.__table__
    assert table.c.created_by_job_id.nullable is True
    _assert_fk(table, "created_by_job_id", "fk_entities_created_by_job")

    row = Entity()
    assert row.created_by_job_id is None


def test_entity_mentions_have_nullable_job_and_partial_unique_extraction_key():
    from app.models.entity_mention import EntityMention

    table = EntityMention.__table__
    assert table.c.created_by_job_id.nullable is True
    assert table.c.extraction_key.nullable is True
    assert table.c.extraction_key.type.length == 64
    _assert_fk(
        table,
        "created_by_job_id",
        "fk_entity_mentions_created_by_job",
    )
    _assert_partial_unique_extraction_key(
        table,
        "uq_entity_mentions_extraction_key",
    )

    row = EntityMention()
    assert row.created_by_job_id is None
    assert row.extraction_key is None


def test_relations_have_nullable_job_and_partial_unique_extraction_key():
    from app.models.knowledge_relation import KnowledgeRelation

    table = KnowledgeRelation.__table__
    assert table.c.created_by_job_id.nullable is True
    assert table.c.extraction_key.nullable is True
    assert table.c.extraction_key.type.length == 64
    _assert_fk(
        table,
        "created_by_job_id",
        "fk_knowledge_relations_created_by_job",
    )
    _assert_partial_unique_extraction_key(
        table,
        "uq_knowledge_relations_extraction_key",
    )

    row = KnowledgeRelation()
    assert row.created_by_job_id is None
    assert row.extraction_key is None


def test_relation_evidence_has_nullable_job_provenance_only():
    from app.models.relation_evidence import RelationEvidence

    table = RelationEvidence.__table__
    assert table.c.created_by_job_id.nullable is True
    assert "extraction_key" not in table.c
    _assert_fk(
        table,
        "created_by_job_id",
        "fk_relation_evidence_created_by_job",
    )

    row = RelationEvidence()
    assert row.created_by_job_id is None
