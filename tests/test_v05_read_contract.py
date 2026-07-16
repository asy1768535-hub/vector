from __future__ import annotations

import asyncio
import uuid

import pytest

from app.models.entity import Entity
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.services import graph_evidence, graph_publication_read


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
PUBLICATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
ENTITY_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
RELATION_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
JOB_IDS = (
    uuid.UUID("60000000-0000-0000-0000-000000000001"),
    uuid.UUID("60000000-0000-0000-0000-000000000002"),
    uuid.UUID("60000000-0000-0000-0000-000000000003"),
    uuid.UUID("60000000-0000-0000-0000-000000000004"),
)


class _Result:
    def __init__(self, rows=(), *, scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def scalar_one(self):
        return self.scalar


class FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected database query"
        return self.results.pop(0)


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="v05-read",
        name="v0.5 Read",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="v05_read",
    )


def _publication(*, status="active", entity_count=1, relation_count=0) -> GraphPublication:
    return GraphPublication(
        id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        status=status,
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="a" * 64,
        idempotency_key="read-key",
        include_drafts=False,
        plan_options={},
        entity_count=entity_count,
        relation_count=relation_count,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
    )


def _entity() -> Entity:
    return Entity(
        id=ENTITY_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=uuid.uuid4(),
        canonical_name="Alice",
        normalized_name="alice",
        properties={},
        status="active",
        source_type="manual",
    )


def _relation() -> KnowledgeRelation:
    return KnowledgeRelation(
        id=RELATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=uuid.uuid4(),
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        properties={},
        status="active",
        review_status="approved",
        source_type="manual",
    )


def _items():
    return [
        GraphPublicationItem(
            id=uuid.UUID("70000000-0000-0000-0000-000000000001"),
            publication_id=PUBLICATION_ID,
            library_id=LIBRARY_ID,
            ontology_version_id=ONTOLOGY_ID,
            item_kind="entity",
            entity_id=ENTITY_ID,
            item_hash="b" * 64,
            status="active",
            support_evidence_ids=[],
            support_counts={},
            fact_snapshot={"canonical_name": "must-not-return"},
        ),
        GraphPublicationItem(
            id=uuid.UUID("70000000-0000-0000-0000-000000000002"),
            publication_id=PUBLICATION_ID,
            library_id=LIBRARY_ID,
            ontology_version_id=ONTOLOGY_ID,
            item_kind="relation",
            relation_id=RELATION_ID,
            item_hash="c" * 64,
            status="active",
            support_evidence_ids=[str(uuid.uuid4())],
            support_counts={"supports": 1},
            fact_snapshot={"properties": "must-not-return"},
        ),
    ]


def test_degraded_current_returns_no_default_rows_but_allows_explicit_diagnostics():
    publication = _publication(status="degraded")
    default_db = FakeDB([_Result([publication])])
    default = asyncio.run(
        graph_publication_read.list_published_entities(default_db, _library())
    )
    assert default.publication is publication
    assert default.healthy is False
    assert default.rows == ()
    assert len(default_db.statements) == 1

    diagnostic_db = FakeDB([_Result([publication]), _Result([_entity()])])
    diagnostic = asyncio.run(
        graph_publication_read.list_published_entities(
            diagnostic_db,
            _library(),
            include_degraded=True,
        )
    )
    assert diagnostic.healthy is False
    assert [row.id for row in diagnostic.rows] == [ENTITY_ID]


def test_healthy_read_joins_publication_membership_while_legacy_helper_stays_lifecycle_only():
    publication_db = FakeDB([_Result([_publication()]), _Result([_entity()])])
    rows = asyncio.run(
        graph_publication_read.list_healthy_published_entities(publication_db, _library())
    )
    assert [row.id for row in rows] == [ENTITY_ID]
    publication_sql = str(publication_db.statements[1]).lower()
    assert "graph_publication_items" in publication_sql
    assert "graph_publication_items.status" in publication_sql

    legacy_db = FakeDB([_Result([_relation()])])
    legacy_rows = asyncio.run(
        graph_evidence.list_active_knowledge_relations(legacy_db, _library())
    )
    assert [row.id for row in legacy_rows] == [RELATION_ID]
    legacy_sql = str(legacy_db.statements[0]).lower()
    assert "graph_publication_items" not in legacy_sql
    assert "knowledge_relations.status" in legacy_sql


def test_healthy_read_fails_closed_instead_of_returning_partial_membership():
    db = FakeDB([_Result([_publication()]), _Result([])])

    with pytest.raises(graph_publication_read.PublicationReadInvariantError):
        asyncio.run(graph_publication_read.list_healthy_published_entities(db, _library()))


@pytest.mark.parametrize(
    ("publication", "helper", "formal_table"),
    [
        (_publication(entity_count=1), graph_publication_read.list_healthy_published_entities, "entities"),
        (
            _publication(entity_count=0, relation_count=1),
            graph_publication_read.list_healthy_published_relations,
            "knowledge_relations",
        ),
    ],
)
def test_healthy_read_fails_closed_when_formal_row_is_not_active(
    publication,
    helper,
    formal_table,
):
    db = FakeDB([_Result([publication]), _Result([])])

    with pytest.raises(graph_publication_read.PublicationReadInvariantError):
        asyncio.run(helper(db, _library()))

    sql = str(db.statements[1]).lower()
    assert f"{formal_table}.status" in sql
    assert "graph_publication_items.status" in sql


def test_item_pagination_is_stable_and_aggregates_formal_row_job_ids():
    entity_item, relation_item = _items()
    db = FakeDB(
        [
            _Result([_publication()]),
            _Result(scalar=2),
            _Result([entity_item, relation_item]),
            _Result([(ENTITY_ID, JOB_IDS[1])]),
            _Result([(ENTITY_ID, JOB_IDS[0])]),
            _Result([(RELATION_ID, JOB_IDS[3])]),
            _Result([(RELATION_ID, JOB_IDS[2])]),
        ]
    )

    result = asyncio.run(
        graph_publication_read.list_graph_publication_items(
            db,
            _library(),
            PUBLICATION_ID,
            item_kind=None,
            item_status=None,
            page=2,
            page_size=100,
        )
    )

    assert result.total == 2
    assert result.rows[0].source_job_ids == JOB_IDS[:2]
    assert result.rows[1].source_job_ids == JOB_IDS[2:]
    item_sql = str(db.statements[2]).lower()
    order = item_sql.split("order by", 1)[1]
    assert order.index("item_kind") < order.index("item_hash") < order.index(".id")
    assert "offset" in item_sql and "limit" in item_sql
