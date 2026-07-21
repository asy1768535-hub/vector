from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.audit import AuditLog
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest, GraphRetrievalSeed
from app.services import graph_retrieval
from tests.test_v05_m3_publication_activation_pg import (
    SeededGraph,
    _activate,
    _add_manual_entity,
    _create_database,
    _drop_database,
    _plan,
    _publication_config,
    _seed_graph,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("VECTOR_KB_PG_TEST_DSN"),
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.6 M3 acceptance.",
)


@dataclass(frozen=True, slots=True)
class M3Fixture:
    graph: SeededGraph
    node_type_id: uuid.UUID
    path_type_id: uuid.UUID
    peer_type_id: uuid.UUID
    fan_type_id: uuid.UUID
    nodes: tuple[uuid.UUID, ...]
    path_relations: tuple[uuid.UUID, ...]
    peer_relation: uuid.UUID
    fan_relations: tuple[uuid.UUID, ...]


async def _add_m3_fixture(Session, graph: SeededGraph) -> M3Fixture:
    async with Session() as db:
        base_support = (
            await db.execute(
                select(RelationEvidence).where(RelationEvidence.relation_id == graph.relation_id)
            )
        ).scalars().one()
        node_type_id = uuid.uuid4()
        path_type_id = uuid.uuid4()
        peer_type_id = uuid.uuid4()
        fan_type_id = uuid.uuid4()
        db.add(
            EntityType(
                id=node_type_id,
                library_id=graph.library_id,
                ontology_version_id=graph.ontology_id,
                key="m3_node",
                label="M3 Node",
                properties_schema={"private": "must-not-project"},
                status="active",
            )
        )
        relation_types = (
            (path_type_id, "m3_path", "directed"),
            (peer_type_id, "m3_peer", "undirected"),
            (fan_type_id, "m3_fan", "directed"),
        )
        for type_id, key, direction in relation_types:
            db.add(
                RelationType(
                    id=type_id,
                    library_id=graph.library_id,
                    ontology_version_id=graph.ontology_id,
                    key=key,
                    label=key.replace("_", " ").title(),
                    direction=direction,
                    requires_evidence=True,
                    default_review_policy="auto_active",
                    properties_schema={"private": "must-not-project"},
                    status="active",
                )
            )
            db.add(
                RelationTypeConstraint(
                    library_id=graph.library_id,
                    ontology_version_id=graph.ontology_id,
                    relation_type_id=type_id,
                    source_entity_type_id=node_type_id,
                    target_entity_type_id=node_type_id,
                    cardinality="many_to_many",
                    requires_review=False,
                    status="active",
                )
            )
        await db.flush()
        nodes = tuple(uuid.uuid4() for _ in range(5))
        for index, entity_id in enumerate(nodes):
            db.add(
                Entity(
                    id=entity_id,
                    library_id=graph.library_id,
                    ontology_version_id=graph.ontology_id,
                    entity_type_id=node_type_id,
                    canonical_name=f"M3 Node {index}",
                    normalized_name=f"m3 node {index}",
                    properties={"private": f"node-{index}"},
                    status="active",
                    source_type="manual",
                )
            )
        await db.flush()

        path_pairs = (
            (nodes[0], nodes[1]),
            (nodes[0], nodes[2]),
            (nodes[1], nodes[3]),
            (nodes[2], nodes[3]),
            (nodes[3], nodes[0]),
            (nodes[0], nodes[0]),
            (nodes[0], nodes[1]),
        )
        path_relations = tuple(uuid.uuid4() for _ in path_pairs)
        peer_relation = uuid.uuid4()
        fan_relations = tuple(uuid.uuid4() for _ in range(205))
        relation_rows = [
            *(
                (relation_id, path_type_id, source_id, target_id)
                for relation_id, (source_id, target_id) in zip(path_relations, path_pairs)
            ),
            (peer_relation, peer_type_id, nodes[2], nodes[4]),
            *(
                (relation_id, fan_type_id, nodes[0], nodes[1])
                for relation_id in fan_relations
            ),
        ]
        for relation_id, relation_type_id, source_id, target_id in relation_rows:
            db.add(
                KnowledgeRelation(
                    id=relation_id,
                    library_id=graph.library_id,
                    ontology_version_id=graph.ontology_id,
                    relation_type_id=relation_type_id,
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    properties={"private": str(relation_id)},
                    status="active",
                    review_status="approved",
                    source_type="manual",
                )
            )
            db.add(
                RelationEvidence(
                    library_id=graph.library_id,
                    relation_id=relation_id,
                    evidence_id=base_support.evidence_id,
                    document_id=base_support.document_id,
                    document_revision_id=base_support.document_revision_id,
                    support_type="supports",
                    quote_text="must not be projected",
                    evidence_text_snapshot="must not be projected",
                    status="active",
                )
            )
        await db.commit()
    return M3Fixture(
        graph=graph,
        node_type_id=node_type_id,
        path_type_id=path_type_id,
        peer_type_id=peer_type_id,
        fan_type_id=fan_type_id,
        nodes=nodes,
        path_relations=path_relations,
        peer_relation=peer_relation,
        fan_relations=fan_relations,
    )


async def _resolved_seed(db, library, snapshot, entity_id):
    return (
        await graph_retrieval.resolve_published_seeds(
            db,
            library,
            snapshot,
            [GraphRetrievalSeed(entity_id=entity_id)],
        )
    )[0]


async def _resolved_type(db, library, snapshot, key):
    return (
        await graph_retrieval.resolve_relation_type_filters(
            db,
            library,
            snapshot,
            [key],
        )
    )[0]


async def _expect_code(awaitable, code: str):
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        await awaitable
    assert exc_info.value.code == code


async def _run_pg_acceptance(dsn: str, monkeypatch) -> None:
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    publication_config = _publication_config()
    retrieval_config = Settings(_env_file=None)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    try:
        graph = await _seed_graph(
            Session,
            "v06_m3_" + uuid.uuid4().hex[:8],
            with_relation=True,
        )
        fixture = await _add_m3_fixture(Session, graph)
        publication_id = await _plan(Session, graph, "v06-m3-base", publication_config)
        assert (await _activate(Session, publication_id, publication_config))[0] == "active"

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db,
                library,
                graph.ontology_id,
                expected_publication_id=publication_id,
            )
            seed_a = await _resolved_seed(db, library, snapshot, fixture.nodes[0])
            seed_b = await _resolved_seed(db, library, snapshot, fixture.nodes[1])
            seed_e = await _resolved_seed(db, library, snapshot, fixture.nodes[4])
            path_type = await _resolved_type(db, library, snapshot, "m3_path")
            peer_type = await _resolved_type(db, library, snapshot, "m3_peer")
            fan_type = await _resolved_type(db, library, snapshot, "m3_fan")

            statements.clear()
            zero = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_b, seed_a, seed_b],
                [path_type],
                direction="both",
                max_hops=0,
                max_nodes=3,
                max_relations=10,
            )
            assert [row.entity_id for row in zero.nodes] == sorted(
                (fixture.nodes[0], fixture.nodes[1]), key=str
            )
            assert not zero.relations and statements == []

            outbound = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_a],
                [path_type],
                direction="outbound",
                max_hops=2,
                max_nodes=100,
                max_relations=200,
            )
            depth_by_node = {row.entity_id: row.depth for row in outbound.nodes}
            assert depth_by_node[fixture.nodes[0]] == 0
            assert depth_by_node[fixture.nodes[1]] == 1
            assert depth_by_node[fixture.nodes[2]] == 1
            assert depth_by_node[fixture.nodes[3]] == 2
            assert fixture.nodes[4] not in depth_by_node
            assert fixture.path_relations[4] not in {
                row.relation_id for row in outbound.relations
            }
            assert set(fixture.path_relations[:4] + fixture.path_relations[5:]) == {
                row.relation_id for row in outbound.relations
            }
            assert len(outbound.nodes) == len({row.entity_id for row in outbound.nodes})
            canonical = tuple(
                (
                    row.depth,
                    row.relation_type_key,
                    str(row.source_entity_id),
                    str(row.target_entity_id),
                    str(row.relation_id),
                )
                for row in outbound.relations
            )
            repeated = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_a],
                [path_type],
                direction="outbound",
                max_hops=2,
                max_nodes=100,
                max_relations=200,
            )
            assert canonical == tuple(
                (
                    row.depth,
                    row.relation_type_key,
                    str(row.source_entity_id),
                    str(row.target_entity_id),
                    str(row.relation_id),
                )
                for row in repeated.relations
            )

            inbound = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_b],
                [path_type],
                direction="inbound",
                max_hops=1,
                max_nodes=100,
                max_relations=200,
            )
            assert {row.source_entity_id for row in inbound.relations} == {
                fixture.nodes[0]
            }
            assert all(row.target_entity_id == fixture.nodes[1] for row in inbound.relations)

            both = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_a],
                [path_type],
                direction="both",
                max_hops=1,
                max_nodes=100,
                max_relations=200,
            )
            assert fixture.path_relations[4] in {
                row.relation_id for row in both.relations
            }
            assert fixture.path_relations[5] in {
                row.relation_id for row in both.relations
            }

            undirected = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_e],
                [peer_type],
                direction="outbound",
                max_hops=1,
                max_nodes=2,
                max_relations=1,
            )
            assert [row.relation_id for row in undirected.relations] == [fixture.peer_relation]
            assert undirected.relations[0].source_entity_id == fixture.nodes[2]
            assert undirected.relations[0].target_entity_id == fixture.nodes[4]
            assert not undirected.truncated.nodes and not undirected.truncated.relations

            fan_statement = graph_retrieval._traversal_statement(
                snapshot,
                (fixture.nodes[0],),
                (),
                (fan_type,),
                "outbound",
                200,
            )
            assert len((await db.execute(fan_statement)).all()) == 201
            fan = await graph_retrieval.traverse_published_graph(
                db,
                library,
                snapshot,
                [seed_a],
                [fan_type],
                direction="outbound",
                max_hops=1,
                max_nodes=2,
                max_relations=200,
            )
            assert len(fan.nodes) == 2
            assert len(fan.relations) == 200
            assert fan.truncated.relations is True
            assert fan.truncated.nodes is False

            audit_before = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            statements.clear()
            request = GraphRetrievalQueryRequest(
                ontology_version_id=graph.ontology_id,
                expected_publication_id=publication_id,
                seeds=[
                    {"entity_id": fixture.nodes[0]},
                    {"canonical_name": "M3 Node 0", "entity_type_key": "m3_node"},
                ],
                direction="outbound",
                relation_type_keys=["m3_path"],
                max_hops=2,
                max_nodes=100,
                max_relations=200,
            )
            result = await graph_retrieval.resolve_and_traverse_graph_retrieval_query(
                db,
                library,
                request,
                config=retrieval_config,
            )
            assert len(statements) == 10
            assert result.resolution.snapshot.publication_id == publication_id
            assert not db.new and not db.dirty and not db.deleted
            audit_after = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            assert audit_after == audit_before

        original_traverse = graph_retrieval.traverse_published_graph
        switched = False

        async def _traverse_then_switch(*args, **kwargs):
            nonlocal switched
            traversal = await original_traverse(*args, **kwargs)
            if not switched:
                switched = True
                await _add_manual_entity(Session, graph, "M3 Replacement")
                replacement_id = await _plan(
                    Session,
                    graph,
                    "v06-m3-replacement",
                    publication_config,
                )
                assert (await _activate(Session, replacement_id, publication_config))[0] == "active"
            return traversal

        monkeypatch.setattr(
            graph_retrieval,
            "traverse_published_graph",
            _traverse_then_switch,
        )
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.resolve_and_traverse_graph_retrieval_query(
                    db,
                    library,
                    GraphRetrievalQueryRequest(
                        ontology_version_id=graph.ontology_id,
                        expected_publication_id=publication_id,
                        seeds=[{"entity_id": fixture.nodes[0]}],
                        relation_type_keys=["m3_path"],
                        max_hops=1,
                    ),
                    config=retrieval_config,
                ),
                "publication_changed",
            )
    finally:
        await engine.dispose()


def test_v06_m3_traversal_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v06_m3")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        asyncio.run(_run_pg_acceptance(dsn, monkeypatch))
    finally:
        _drop_database(name)
