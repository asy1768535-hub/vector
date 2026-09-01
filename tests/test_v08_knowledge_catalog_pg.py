from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, settings
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.services.knowledge_catalog import (
    get_catalog_document_detail,
    get_catalog_evidence_detail,
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


async def _exercise(name: str, ids: dict[str, uuid.UUID]) -> None:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await execute_sql(
            _database_url(name),
            f"""
                    INSERT INTO sys_libraries (
                        id, organization_id, slug, name, embedding_model, embedding_dim,
                        vector_distance, chunk_size, chunk_overlap, qdrant_collection
                    ) VALUES (
                        '{ids['library']}', '{DEFAULT_ORGANIZATION_ID}', 'catalog-pg',
                        'Catalog PG', 'bge-m3', 1024, 'cosine', 1000, 120, 'catalog_pg'
                    );
                    INSERT INTO documents (
                        id, library_id, title, content_hash, current_revision,
                        current_revision_id, latest_revision_id, status
                    ) VALUES (
                        '{ids['document']}', '{ids['library']}', 'Contract', '{'a' * 64}',
                        1, '{ids['revision']}', '{ids['revision']}', 'ready'
                    );
                    INSERT INTO document_revisions (
                        id, document_id, library_id, revision_no, title, content_hash,
                        normalized_text, parser_name, parser_version, chunking_strategy,
                        chunking_strategy_version, security_level, status, finished_at
                    ) VALUES (
                        '{ids['revision']}', '{ids['document']}', '{ids['library']}', 1,
                        'Contract', '{'a' * 64}', 'Acme signed the contract.', 'test', '1',
                        'fixed', '1', 'internal', 'ready', now()
                    );
                    INSERT INTO evidence_units (
                        id, library_id, document_id, document_revision_id, evidence_kind,
                        source_start, source_end, page_start, page_end, title_path,
                        text_quote, status
                    ) VALUES (
                        '{ids['evidence']}', '{ids['library']}', '{ids['document']}',
                        '{ids['revision']}', 'chunk', 0, 4, 1, 1, '["Agreement"]'::jsonb,
                        'Acme', 'active'
                    );
                    INSERT INTO ontology_versions (
                        id, library_id, version_key, version_no, status, published_at
                    ) VALUES (
                        '{ids['ontology']}', '{ids['library']}', 'catalog', 1, 'active', now()
                    );
                    INSERT INTO entity_types (
                        id, library_id, ontology_version_id, key, label, status
                    ) VALUES (
                        '{ids['entity_type']}', '{ids['library']}', '{ids['ontology']}',
                        'company', 'Company', 'active'
                    );
                    INSERT INTO entities (
                        id, library_id, ontology_version_id, entity_type_id, canonical_name,
                        normalized_name, status, source_type, confidence
                    ) VALUES (
                        '{ids['entity']}', '{ids['library']}', '{ids['ontology']}',
                        '{ids['entity_type']}', 'Acme', 'acme', 'active', 'extracted', 0.9
                    );
                    INSERT INTO entity_mentions (
                        id, library_id, entity_id, evidence_id, document_id,
                        document_revision_id, mention_text, source_type, status
                    ) VALUES (
                        '{ids['mention']}', '{ids['library']}', '{ids['entity']}',
                        '{ids['evidence']}', '{ids['document']}', '{ids['revision']}',
                        'Acme', 'extracted', 'active'
                    );
                    INSERT INTO graph_publications (
                        id, library_id, ontology_version_id, status, source_mode,
                        manifest_version, policy_version, manifest_hash, idempotency_key,
                        entity_count, relation_count, activated_at
                    ) VALUES (
                        '{ids['publication']}', '{ids['library']}', '{ids['ontology']}',
                        'active', 'initial_seed', 'v1', 'v1', '{'b' * 64}', 'catalog-pg',
                        1, 0, now()
                    );
                    INSERT INTO graph_publication_items (
                        id, publication_id, library_id, ontology_version_id, item_kind,
                        entity_id, item_hash, status, support_evidence_ids
                    ) VALUES (
                        '{ids['item']}', '{ids['publication']}', '{ids['library']}',
                        '{ids['ontology']}', 'entity', '{ids['entity']}', '{'c' * 64}',
                        'active', '["{ids['evidence']}"]'::jsonb
                    );
            """,
        )
        async with sessions() as db:
            library = (
                await db.execute(select(Library).where(Library.id == ids["library"]))
            ).scalar_one()
            config = Settings(
                _env_file=None,
                graph_retrieval_enabled=True,
                knowledge_catalog_max_entity_cards=10,
                knowledge_catalog_max_relation_cards=10,
                knowledge_catalog_max_evidence_per_fact=10,
            )
            detail = await get_catalog_document_detail(
                db,
                library=library,
                document_id=ids["document"],
                config=config,
            )
            assert detail.graph.counts.entities == 1
            assert detail.graph.entities[0].canonical_name == "Acme"
            assert detail.graph.entities[0].evidence[0].evidence_id == ids["evidence"]
            evidence = await get_catalog_evidence_detail(
                db,
                library=library,
                evidence_id=ids["evidence"],
            )
            assert evidence.fact_refs[0].item_id == ids["item"]
            assert evidence.title_path == ["Agreement"]
    finally:
        await engine.dispose()


def test_catalog_document_graph_and_evidence_on_postgresql(monkeypatch):
    name = f"vkt_v08_catalog_{uuid.uuid4().hex[:8]}"
    ids = {
        key: uuid.uuid4()
        for key in (
            "library",
            "document",
            "revision",
            "evidence",
            "ontology",
            "entity_type",
            "entity",
            "mention",
            "publication",
            "item",
        )
    }
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "0037")
        asyncio.run(_exercise(name, ids))
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
