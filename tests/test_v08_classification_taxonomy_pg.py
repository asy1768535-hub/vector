from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from app.config import settings
from app.models.organization import DEFAULT_ORGANIZATION_ID
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


async def _execute(name: str, sql: str):
    return await execute_sql(_database_url(name), sql)


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def test_0034_round_trip_and_one_active_constraint(monkeypatch):
    name = f"vkt_v08_taxonomy_{uuid.uuid4().hex[:8]}"
    user_id, library_id = uuid.uuid4(), uuid.uuid4()
    first_id, second_id, label_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0033")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES ('{user_id}', 'taxonomy@example.com', 'hash', true, false, true);
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'taxonomy', 'Taxonomy',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'lib_taxonomy'
                );
                """,
            )
        )
        command.upgrade(config, "0034")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO classification_taxonomies (
                    id, organization_id, version_key, version_no, status,
                    created_by_user_id, activated_by_user_id, activated_at
                ) VALUES (
                    '{first_id}', '{DEFAULT_ORGANIZATION_ID}', 'document-category', 1,
                    'active', '{user_id}', '{user_id}', now()
                );
                INSERT INTO classification_taxonomies (
                    id, organization_id, version_key, version_no, status,
                    parent_version_id, created_by_user_id
                ) VALUES (
                    '{second_id}', '{DEFAULT_ORGANIZATION_ID}', 'document-category', 2,
                    'draft', '{first_id}', '{user_id}'
                );
                INSERT INTO classification_labels (
                    id, taxonomy_version_id, key, label, sort_order, status
                ) VALUES ('{label_id}', '{first_id}', 'legal', 'Legal', 0, 'active');
                INSERT INTO library_classification_labels (
                    id, library_id, taxonomy_version_id, label_id, ordinal
                ) VALUES ('{uuid.uuid4()}', '{library_id}', '{first_id}', '{label_id}', 0);
                """,
            )
        )
        with pytest.raises(Exception, match="uq_classification_taxonomies_one_active_org"):
            asyncio.run(
                _execute(
                    name,
                    f"""
                    UPDATE classification_taxonomies
                    SET status = 'active', activated_by_user_id = '{user_id}', activated_at = now()
                    WHERE id = '{second_id}'
                    """,
                )
            )
        command.downgrade(config, "0033")
        assert asyncio.run(
            _execute(name, f"SELECT id FROM sys_users WHERE id = '{user_id}'")
        ) == [(user_id,)]
        assert asyncio.run(
            _execute(name, f"SELECT id FROM sys_libraries WHERE id = '{library_id}'")
        ) == [(library_id,)]
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_name IN (
                    'classification_taxonomies', 'classification_labels',
                    'library_classification_labels'
                )
                """,
            )
        )[0][0] == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
