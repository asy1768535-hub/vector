from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.audit import AuditLog
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest, GraphRetrievalSeed
from app.services import graph_retrieval
from tests.test_v05_m3_publication_activation_pg import (
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
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.6 M2 acceptance.",
)


async def _expect_code(awaitable, code: str):
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        await awaitable
    assert exc_info.value.code == code


async def _run_pg_acceptance(dsn: str) -> None:
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    publication_config = _publication_config()
    retrieval_config = Settings(_env_file=None)
    try:
        graph = await _seed_graph(
            Session,
            "v06_m2_primary_" + uuid.uuid4().hex[:8],
            with_relation=True,
        )
        publication_id = await _plan(Session, graph, "v06-m2-base", publication_config)
        assert (await _activate(Session, publication_id, publication_config))[0] == "active"

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            audit_before = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            request = GraphRetrievalQueryRequest(
                ontology_version_id=graph.ontology_id,
                expected_publication_id=publication_id,
                seeds=[{"entity_id": graph.first_entity_id}],
                relation_type_keys=["member_of"],
            )
            result = await graph_retrieval.resolve_graph_retrieval_query(
                db,
                library,
                request,
                config=retrieval_config,
            )
            assert result.snapshot.publication_id == publication_id
            assert result.seeds[0].entity_id == graph.first_entity_id
            assert result.relation_types[0].key == "member_of"

            by_name = await graph_retrieval.resolve_published_seeds(
                db,
                library,
                result.snapshot,
                [GraphRetrievalSeed(canonical_name="  ALICE  ", entity_type_key="person")],
            )
            assert by_name[0].entity_id == graph.first_entity_id
            audit_after = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            assert audit_after == audit_before
            assert not db.new and not db.dirty and not db.deleted

        foreign_graph = await _seed_graph(
            Session,
            "v06_m2_foreign_" + uuid.uuid4().hex[:8],
            with_relation=True,
        )
        foreign_publication_id = await _plan(
            Session,
            foreign_graph,
            "v06-m2-foreign",
            publication_config,
        )
        assert (await _activate(Session, foreign_publication_id, publication_config))[0] == "active"

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db,
                library,
                graph.ontology_id,
            )
            await _expect_code(
                graph_retrieval.resolve_published_seeds(
                    db,
                    library,
                    snapshot,
                    [GraphRetrievalSeed(entity_id=foreign_graph.first_entity_id)],
                ),
                "seed_not_found",
            )
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(
                    db,
                    library,
                    graph.ontology_id,
                    expected_publication_id=foreign_publication_id,
                ),
                "publication_changed",
            )

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            publication = await db.get(GraphPublication, publication_id)
            publication.status = "degraded"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_unavailable",
            )
            publication = await db.get(GraphPublication, publication_id)
            publication.status = "active"
            await db.commit()

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            item = (
                await db.execute(
                    select(GraphPublicationItem)
                    .where(GraphPublicationItem.publication_id == publication_id)
                    .order_by(GraphPublicationItem.id)
                    .limit(1)
                )
            ).scalars().one()
            item.status = "degraded"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            item = await db.get(GraphPublicationItem, item.id)
            item.status = "active"
            await db.commit()

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            entity = await db.get(Entity, graph.first_entity_id)
            entity.status = "stale"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            entity = await db.get(Entity, graph.first_entity_id)
            entity.status = "active"
            await db.commit()

        async with Session() as db:
            entity = await db.get(Entity, graph.first_entity_id)
            entity_type = await db.get(EntityType, entity.entity_type_id)
            entity_type.status = "disabled"
            await db.commit()
            entity_type_id = entity_type.id
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            entity_type = await db.get(EntityType, entity_type_id)
            entity_type.status = "active"
            await db.commit()

        async with Session() as db:
            relation = await db.get(KnowledgeRelation, graph.relation_id)
            relation.review_status = "pending_review"
            relation_type = await db.get(RelationType, relation.relation_type_id)
            relation_type_id = relation_type.id
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            relation = await db.get(KnowledgeRelation, graph.relation_id)
            relation.review_status = "approved"
            relation_type = await db.get(RelationType, relation_type_id)
            relation_type.status = "disabled"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            relation_type = await db.get(RelationType, relation_type_id)
            relation_type.status = "active"
            await db.commit()

        async with Session() as db:
            ontology = await db.get(OntologyVersion, graph.ontology_id)
            ontology.status = "disabled"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            ontology = await db.get(OntologyVersion, graph.ontology_id)
            ontology.status = "active"
            await db.commit()

        outside_id = await _add_manual_entity(Session, graph, "Outside")
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            relation = await db.get(KnowledgeRelation, graph.relation_id)
            original_target = relation.target_entity_id
            relation.target_entity_id = outside_id
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(db, library, graph.ontology_id),
                "graph_publication_invariant_failed",
            )
            relation = await db.get(KnowledgeRelation, graph.relation_id)
            relation.target_entity_id = original_target
            await db.commit()

        async with Session() as db:
            team_type_id = (
                await db.execute(
                    select(EntityType.id).where(
                        EntityType.library_id == graph.library_id,
                        EntityType.ontology_version_id == graph.ontology_id,
                        EntityType.key == "team",
                    )
                )
            ).scalar_one()
            db.add(
                Entity(
                    library_id=graph.library_id,
                    ontology_version_id=graph.ontology_id,
                    entity_type_id=team_type_id,
                    canonical_name="ALICE",
                    normalized_name="alice",
                    properties={"secret": "must-not-load"},
                    status="active",
                    source_type="manual",
                )
            )
            await db.commit()
        ambiguous_publication_id = await _plan(
            Session,
            graph,
            "v06-m2-ambiguous",
            publication_config,
        )
        assert (await _activate(Session, ambiguous_publication_id, publication_config))[0] == "active"

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db,
                library,
                graph.ontology_id,
            )
            await _expect_code(
                graph_retrieval.resolve_published_seeds(
                    db,
                    library,
                    snapshot,
                    [GraphRetrievalSeed(canonical_name="Alice")],
                ),
                "seed_ambiguous",
            )
            typed = await graph_retrieval.resolve_published_seeds(
                db,
                library,
                snapshot,
                [GraphRetrievalSeed(canonical_name="Alice", entity_type_key="person")],
            )
            assert typed[0].entity_id == graph.first_entity_id

        async with Session() as db_a:
            library_a = await db_a.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db_a,
                library_a,
                graph.ontology_id,
            )
            await _add_manual_entity(Session, graph, "Replacement")
            replacement_id = await _plan(
                Session,
                graph,
                "v06-m2-replacement",
                publication_config,
            )
            assert (await _activate(Session, replacement_id, publication_config))[0] == "active"
            await _expect_code(
                graph_retrieval.assert_graph_snapshot_still_current(
                    db_a,
                    library_a,
                    snapshot,
                ),
                "publication_changed",
            )

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            await _expect_code(
                graph_retrieval.load_healthy_graph_snapshot(
                    db,
                    library,
                    graph.ontology_id,
                    expected_publication_id=ambiguous_publication_id,
                ),
                "publication_changed",
            )

        async with Session() as db:
            foreign_only = RelationType(
                library_id=foreign_graph.library_id,
                ontology_version_id=foreign_graph.ontology_id,
                key="foreign_only",
                label="Foreign Only",
                direction="directed",
                requires_evidence=True,
                default_review_policy="auto_active",
                properties_schema={},
                status="active",
            )
            db.add(foreign_only)
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db,
                library,
                graph.ontology_id,
            )
            await _expect_code(
                graph_retrieval.resolve_relation_type_filters(
                    db,
                    library,
                    snapshot,
                    ["foreign_only"],
                ),
                "relation_type_not_found",
            )

        async with Session() as db:
            ontology = OntologyVersion(
                library_id=graph.library_id,
                version_key="secondary",
                version_no=1,
                status="active",
            )
            db.add(ontology)
            await db.flush()
            entity_type = EntityType(
                library_id=graph.library_id,
                ontology_version_id=ontology.id,
                key="secondary_person",
                label="Secondary Person",
                properties_schema={},
                status="active",
            )
            db.add(entity_type)
            await db.flush()
            cross_ontology_entity = Entity(
                library_id=graph.library_id,
                ontology_version_id=ontology.id,
                entity_type_id=entity_type.id,
                canonical_name="Cross Ontology",
                normalized_name="cross ontology",
                properties={},
                status="active",
                source_type="manual",
            )
            db.add(cross_ontology_entity)
            await db.commit()
            cross_ontology_id = cross_ontology_entity.id
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            snapshot = await graph_retrieval.load_healthy_graph_snapshot(
                db,
                library,
                graph.ontology_id,
            )
            await _expect_code(
                graph_retrieval.resolve_published_seeds(
                    db,
                    library,
                    snapshot,
                    [GraphRetrievalSeed(entity_id=cross_ontology_id)],
                ),
                "seed_not_found",
            )
    finally:
        await engine.dispose()


def test_v06_m2_snapshot_resolver_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v06_m2")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        asyncio.run(_run_pg_acceptance(dsn))
    finally:
        _drop_database(name)
