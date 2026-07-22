from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.user import User
from app.services.graph_catalog import (
    search_graph_catalog_entities,
    search_graph_catalog_relations,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogResolvedScope,
    GraphCatalogSelection,
    GraphEntityCatalogQuery,
    GraphRelationCatalogQuery,
)
from app.services.graph_catalog_details import (
    get_graph_catalog_entity_detail,
    get_graph_catalog_relation_detail,
)
from tests.v08_pg_support import execute_sql


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None


def _admin_url() -> URL:
    assert _ADMIN_URL is not None
    return _ADMIN_URL


async def _admin(sql: str) -> None:
    import asyncpg

    url = _admin_url()
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _ids() -> dict[str, uuid.UUID]:
    return {
        key: uuid.uuid4()
        for key in (
            "alpha_library",
            "beta_library",
            "alpha_ontology",
            "beta_ontology",
            "alpha_entity_type",
            "beta_entity_type",
            "relation_type",
            "alpha_acme",
            "alpha_target",
            "beta_acme",
            "relation",
            "document",
            "revision",
            "chunk_one",
            "chunk_two",
            "evidence_one",
            "evidence_two",
            "evidence_stale",
            "mention_one",
            "mention_two",
            "mention_stale",
            "relation_evidence_one",
            "relation_evidence_two",
            "relation_evidence_stale",
            "alpha_publication",
            "beta_publication",
            "alpha_entity_item",
            "alpha_relation_item",
            "beta_entity_item",
        )
    }


async def _seed(name: str, ids: dict[str, uuid.UUID]) -> None:
    await execute_sql(
        _database_url(name),
        f"""
        INSERT INTO sys_libraries (
            id, organization_id, slug, name, embedding_model, embedding_dim,
            vector_distance, chunk_size, chunk_overlap, qdrant_collection
        ) VALUES
            ('{ids['alpha_library']}', '{DEFAULT_ORGANIZATION_ID}', 'catalog-alpha',
             'Catalog Alpha', 'bge-m3', 1024, 'cosine', 1000, 120, 'catalog_alpha'),
            ('{ids['beta_library']}', '{DEFAULT_ORGANIZATION_ID}', 'catalog-beta',
             'Catalog Beta', 'bge-m3', 1024, 'cosine', 1000, 120, 'catalog_beta');

        INSERT INTO ontology_versions (
            id, library_id, version_key, version_no, status, published_at
        ) VALUES
            ('{ids['alpha_ontology']}', '{ids['alpha_library']}', 'catalog', 1,
             'active', now()),
            ('{ids['beta_ontology']}', '{ids['beta_library']}', 'catalog', 1,
             'active', now());

        INSERT INTO entity_types (
            id, library_id, ontology_version_id, key, label, status
        ) VALUES
            ('{ids['alpha_entity_type']}', '{ids['alpha_library']}',
             '{ids['alpha_ontology']}', 'company', 'Company', 'active'),
            ('{ids['beta_entity_type']}', '{ids['beta_library']}',
             '{ids['beta_ontology']}', 'company', 'Company', 'active');

        INSERT INTO relation_types (
            id, library_id, ontology_version_id, key, label, direction,
            requires_evidence, default_review_policy, status
        ) VALUES (
            '{ids['relation_type']}', '{ids['alpha_library']}',
            '{ids['alpha_ontology']}', 'invested_in', 'Invested In', 'directed',
            true, 'pending_review', 'active'
        );

        INSERT INTO entities (
            id, library_id, ontology_version_id, entity_type_id, canonical_name,
            normalized_name, status, source_type, confidence
        ) VALUES
            ('{ids['alpha_acme']}', '{ids['alpha_library']}',
             '{ids['alpha_ontology']}', '{ids['alpha_entity_type']}', 'Acme', 'acme',
             'active', 'extracted', 0.95),
            ('{ids['alpha_target']}', '{ids['alpha_library']}',
             '{ids['alpha_ontology']}', '{ids['alpha_entity_type']}', 'Beta Co',
             'beta co', 'active', 'manual', 1.0),
            ('{ids['beta_acme']}', '{ids['beta_library']}',
             '{ids['beta_ontology']}', '{ids['beta_entity_type']}', 'Acme', 'acme',
             'pending_review', 'extracted', 0.72);

        INSERT INTO knowledge_relations (
            id, library_id, ontology_version_id, relation_type_id,
            source_entity_id, target_entity_id, properties, status, review_status,
            source_type, confidence
        ) VALUES (
            '{ids['relation']}', '{ids['alpha_library']}', '{ids['alpha_ontology']}',
            '{ids['relation_type']}', '{ids['alpha_acme']}', '{ids['alpha_target']}',
            '{{"percent": 12.5}}'::jsonb, 'active', 'approved', 'extracted', 0.91
        );

        INSERT INTO documents (
            id, library_id, title, content_hash, current_revision,
            current_revision_id, latest_revision_id, status
        ) VALUES (
            '{ids['document']}', '{ids['alpha_library']}', 'Investment Agreement',
            '{'a' * 64}', 1, '{ids['revision']}', '{ids['revision']}', 'ready'
        );

        INSERT INTO document_revisions (
            id, document_id, library_id, revision_no, title, content_hash,
            normalized_text, parser_name, parser_version, chunking_strategy,
            chunking_strategy_version, security_level, status, finished_at
        ) VALUES (
            '{ids['revision']}', '{ids['document']}', '{ids['alpha_library']}', 1,
            'Investment Agreement', '{'a' * 64}', 'Acme invested in Beta Co.',
            'test', '1', 'fixed', '1', 'internal', 'ready', now()
        );

        INSERT INTO chunks (
            id, document_id, library_id, document_revision_id, seq, text,
            token_count, page_start, page_end, title_path, source_start, source_end
        ) VALUES
            ('{ids['chunk_one']}', '{ids['document']}', '{ids['alpha_library']}',
             '{ids['revision']}', 0, 'Acme invested', 2, 3, 3,
             '["Investment", "Parties"]'::jsonb, 0, 13),
            ('{ids['chunk_two']}', '{ids['document']}', '{ids['alpha_library']}',
             '{ids['revision']}', 1, 'in Beta Co', 3, 4, 4,
             '["Investment", "Terms"]'::jsonb, 14, 24);

        INSERT INTO evidence_units (
            id, library_id, document_id, document_revision_id, evidence_kind,
            source_start, source_end, page_start, page_end, title_path, text_quote, status
        ) VALUES
            ('{ids['evidence_one']}', '{ids['alpha_library']}', '{ids['document']}',
             '{ids['revision']}', 'chunk', 0, 13, 3, 3,
             '["Investment", "Parties"]'::jsonb, 'Acme invested', 'active'),
            ('{ids['evidence_two']}', '{ids['alpha_library']}', '{ids['document']}',
             '{ids['revision']}', 'chunk', 14, 24, 4, 4,
             '["Investment", "Terms"]'::jsonb, 'in Beta Co', 'active'),
            ('{ids['evidence_stale']}', '{ids['alpha_library']}', '{ids['document']}',
             '{ids['revision']}', 'chunk', 25, 30, 5, 5,
             '["Old"]'::jsonb, 'old', 'stale');

        INSERT INTO entity_mentions (
            id, library_id, entity_id, evidence_id, document_id,
            document_revision_id, chunk_id, mention_text, source_type, status
        ) VALUES
            ('{ids['mention_one']}', '{ids['alpha_library']}', '{ids['alpha_acme']}',
             '{ids['evidence_one']}', '{ids['document']}', '{ids['revision']}',
             '{ids['chunk_one']}', 'Acme', 'extracted', 'active'),
            ('{ids['mention_two']}', '{ids['alpha_library']}', '{ids['alpha_acme']}',
             '{ids['evidence_two']}', '{ids['document']}', '{ids['revision']}',
             '{ids['chunk_two']}', 'Acme', 'extracted', 'active'),
            ('{ids['mention_stale']}', '{ids['alpha_library']}', '{ids['alpha_acme']}',
             '{ids['evidence_stale']}', '{ids['document']}', '{ids['revision']}', NULL,
             'Acme', 'extracted', 'active');

        INSERT INTO relation_evidence (
            id, library_id, relation_id, evidence_id, document_id,
            document_revision_id, chunk_id, support_type, status
        ) VALUES
            ('{ids['relation_evidence_one']}', '{ids['alpha_library']}',
             '{ids['relation']}', '{ids['evidence_one']}', '{ids['document']}',
             '{ids['revision']}', '{ids['chunk_one']}', 'supports', 'active'),
            ('{ids['relation_evidence_two']}', '{ids['alpha_library']}',
             '{ids['relation']}', '{ids['evidence_two']}', '{ids['document']}',
             '{ids['revision']}', '{ids['chunk_two']}', 'source', 'active'),
            ('{ids['relation_evidence_stale']}', '{ids['alpha_library']}',
             '{ids['relation']}', '{ids['evidence_stale']}', '{ids['document']}',
             '{ids['revision']}', NULL, 'supports', 'active');

        INSERT INTO graph_publications (
            id, library_id, ontology_version_id, status, source_mode,
            manifest_version, policy_version, manifest_hash, idempotency_key,
            entity_count, relation_count, activated_at
        ) VALUES
            ('{ids['alpha_publication']}', '{ids['alpha_library']}',
             '{ids['alpha_ontology']}', 'active', 'initial_seed', 'v1', 'v1',
             '{'b' * 64}', 'catalog-alpha', 1, 1, now()),
            ('{ids['beta_publication']}', '{ids['beta_library']}',
             '{ids['beta_ontology']}', 'active', 'initial_seed', 'v1', 'v1',
             '{'c' * 64}', 'catalog-beta', 1, 0, now());

        INSERT INTO graph_publication_items (
            id, publication_id, library_id, ontology_version_id, item_kind,
            entity_id, relation_id, item_hash, status, support_evidence_ids
        ) VALUES
            ('{ids['alpha_entity_item']}', '{ids['alpha_publication']}',
             '{ids['alpha_library']}', '{ids['alpha_ontology']}', 'entity',
             '{ids['alpha_acme']}', NULL, '{'d' * 64}', 'active',
             '["{ids['evidence_one']}", "{ids['evidence_two']}"]'::jsonb),
            ('{ids['alpha_relation_item']}', '{ids['alpha_publication']}',
             '{ids['alpha_library']}', '{ids['alpha_ontology']}', 'relation', NULL,
             '{ids['relation']}', '{'e' * 64}', 'active',
             '["{ids['evidence_one']}", "{ids['evidence_two']}"]'::jsonb),
            ('{ids['beta_entity_item']}', '{ids['beta_publication']}',
             '{ids['beta_library']}', '{ids['beta_ontology']}', 'entity',
             '{ids['beta_acme']}', NULL, '{'f' * 64}', 'active', '[]'::jsonb);
        """,
    )


async def _exercise(name: str, ids: dict[str, uuid.UUID]) -> None:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            libraries = (
                await db.execute(
                    select(Library)
                    .where(Library.id.in_((ids["alpha_library"], ids["beta_library"])))
                    .order_by(Library.slug)
                )
            ).scalars().all()
            alpha, beta = libraries
            user = User(
                id=uuid.uuid4(),
                email="catalog-pg@example.com",
                hashed_password="not-used",
            )
            multi_scope = GraphCatalogResolvedScope(
                DEFAULT_ORGANIZATION_ID,
                (alpha, beta),
            )
            selection = GraphCatalogSelection(
                DEFAULT_ORGANIZATION_ID,
                library_slugs=(alpha.slug, beta.slug),
            )
            entity_query = GraphEntityCatalogQuery(
                selection,
                query_text="Acme",
                limit=1,
            )
            with patch(
                "app.services.graph_catalog.resolve_graph_catalog_scope",
                new=AsyncMock(return_value=multi_scope),
            ):
                first = await search_graph_catalog_entities(
                    db,
                    user=user,
                    query=entity_query,
                )
                second = await search_graph_catalog_entities(
                    db,
                    user=user,
                    query=entity_query,
                    cursor_value=first.next_cursor,
                )

            assert [item.id for item in first.items] == [ids["alpha_acme"]]
            assert [item.id for item in second.items] == [ids["beta_acme"]]
            assert first.items[0].library.slug == "catalog-alpha"
            assert second.items[0].library.slug == "catalog-beta"
            assert first.items[0].publication_state == "published"
            assert second.items[0].publication_state == "staged"
            assert second.items[0].publication is None
            assert first.items[0].counts.evidence == 2
            assert first.items[0].counts.documents == 1

            published_query = GraphEntityCatalogQuery(
                selection,
                query_text="Acme",
                publication_state="published",
            )
            with patch(
                "app.services.graph_catalog.resolve_graph_catalog_scope",
                new=AsyncMock(return_value=multi_scope),
            ):
                published = await search_graph_catalog_entities(
                    db,
                    user=user,
                    query=published_query,
                )
            assert [item.id for item in published.items] == [ids["alpha_acme"]]

            alpha_scope = GraphCatalogResolvedScope(
                DEFAULT_ORGANIZATION_ID,
                (alpha,),
            )
            relation_query = GraphRelationCatalogQuery(
                GraphCatalogSelection(
                    DEFAULT_ORGANIZATION_ID,
                    library_slugs=(alpha.slug,),
                ),
                limit=10,
            )
            with patch(
                "app.services.graph_catalog.resolve_graph_catalog_scope",
                new=AsyncMock(return_value=alpha_scope),
            ):
                relations = await search_graph_catalog_relations(
                    db,
                    user=user,
                    query=relation_query,
                )
            assert [item.id for item in relations.items] == [ids["relation"]]
            assert relations.items[0].source.id == ids["alpha_acme"]
            assert relations.items[0].target.id == ids["alpha_target"]
            assert relations.items[0].publication_state == "published"
            assert relations.items[0].counts.evidence == 2
            assert relations.items[0].counts.documents == 1

            with patch(
                "app.services.graph_catalog_details.resolve_graph_catalog_scope",
                new=AsyncMock(return_value=alpha_scope),
            ):
                entity_detail = await get_graph_catalog_entity_detail(
                    db,
                    user=user,
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    library_slug=alpha.slug,
                    entity_id=ids["alpha_acme"],
                )
                relation_detail = await get_graph_catalog_relation_detail(
                    db,
                    user=user,
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    library_slug=alpha.slug,
                    relation_id=ids["relation"],
                )

            expected_evidence = {ids["evidence_one"], ids["evidence_two"]}
            expected_chunks = {
                ids["evidence_one"]: ids["chunk_one"],
                ids["evidence_two"]: ids["chunk_two"],
            }
            assert entity_detail.evidence_count == 2
            assert entity_detail.document_count == 1
            assert {item.evidence_id for item in entity_detail.evidence} == expected_evidence
            assert {
                item.evidence_id: item.chunk_id for item in entity_detail.evidence
            } == expected_chunks
            assert all(
                item.document_revision_id == ids["revision"]
                for item in entity_detail.evidence
            )
            assert {tuple(item.title_path or ()) for item in entity_detail.evidence} == {
                ("Investment", "Parties"),
                ("Investment", "Terms"),
            }
            assert relation_detail.evidence_count == 2
            assert relation_detail.document_count == 1
            assert {item.evidence_id for item in relation_detail.evidence} == expected_evidence
            assert {item.support_type for item in relation_detail.evidence} == {
                "supports",
                "source",
            }
    finally:
        await engine.dispose()


def test_graph_catalog_read_model_on_postgresql(monkeypatch):
    name = f"vkt_v09_graph_catalog_{uuid.uuid4().hex[:8]}"
    ids = _ids()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "head")
        asyncio.run(_seed(name, ids))
        asyncio.run(_exercise(name, ids))
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
