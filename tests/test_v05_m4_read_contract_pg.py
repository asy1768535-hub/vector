from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.entity import Entity
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.services import graph_publication_read
from tests.test_v05_m3_publication_activation_pg import (
    _activate,
    _create_database,
    _drop_database,
    _plan,
    _publication_config,
    _seed_graph,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("VECTOR_KB_PG_TEST_DSN"),
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.5 acceptance.",
)


async def _run_read_contract(dsn: str) -> None:
    engine = create_async_engine(dsn)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    config = _publication_config()
    try:
        graph = await _seed_graph(
            Session,
            "v05_m4_read_" + uuid.uuid4().hex[:8],
            with_relation=True,
        )
        publication_id = await _plan(Session, graph, "m4-read-plan", config)
        assert (await _activate(Session, publication_id, config))[0] == "active"

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            current = await graph_publication_read.list_current_graph_publication(
                db,
                library,
                graph.ontology_id,
            )
            entities = await graph_publication_read.list_healthy_published_entities(
                db,
                library,
                graph.ontology_id,
            )
            relations = await graph_publication_read.list_healthy_published_relations(
                db,
                library,
                graph.ontology_id,
            )
            item_page = await graph_publication_read.list_graph_publication_items(
                db,
                library,
                publication_id,
                item_kind=None,
                item_status=None,
                page=1,
                page_size=2,
            )
            assert current.id == publication_id
            assert current.status == "active"
            assert len(entities) == 2
            assert len(relations) == 1
            assert item_page.total == 3
            assert len(item_page.rows) == 2
            assert [view.item.item_kind for view in item_page.rows] == ["entity", "entity"]
            assert all(view.source_job_ids == () for view in item_page.rows)

        async with Session() as db:
            drifted_entity = await db.get(Entity, graph.first_entity_id)
            drifted_entity.status = "stale"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            with pytest.raises(graph_publication_read.PublicationReadInvariantError):
                await graph_publication_read.list_healthy_published_entities(
                    db,
                    library,
                    graph.ontology_id,
                )
            drifted_entity = await db.get(Entity, graph.first_entity_id)
            drifted_entity.status = "active"
            await db.commit()

        async with Session() as db:
            publication = await db.get(GraphPublication, publication_id)
            publication.status = "degraded"
            first_item = (
                await db.execute(
                    select(GraphPublicationItem)
                    .where(GraphPublicationItem.publication_id == publication_id)
                    .order_by(GraphPublicationItem.item_kind, GraphPublicationItem.item_hash)
                    .limit(1)
                )
            ).scalars().first()
            first_item.status = "degraded"
            await db.commit()

        async with Session() as db:
            library = await db.get(Library, graph.library_id)
            healthy = await graph_publication_read.list_published_relations(
                db,
                library,
                graph.ontology_id,
            )
            diagnostic = await graph_publication_read.list_published_relations(
                db,
                library,
                graph.ontology_id,
                include_degraded=True,
            )
            wrong_ontology = await graph_publication_read.list_current_graph_publication(
                db,
                library,
                uuid.uuid4(),
            )
            assert healthy.publication.status == "degraded"
            assert healthy.healthy is False
            assert healthy.rows == ()
            assert diagnostic.healthy is False
            assert len(diagnostic.rows) == 1
            assert wrong_ontology is None
    finally:
        await engine.dispose()


def test_v05_m4_membership_reads_and_degraded_health_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v05_m4")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        asyncio.run(_run_read_contract(dsn))
    finally:
        _drop_database(name)
