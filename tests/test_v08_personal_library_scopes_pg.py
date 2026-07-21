from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.models.organization import DEFAULT_ORGANIZATION_ID


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
    engine = create_async_engine(_database_url(name))
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql))
            return result.fetchall() if result.returns_rows else None
    finally:
        await engine.dispose()


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def test_0033_scope_round_trip_preserves_existing_identity(monkeypatch):
    name = f"vkt_v08_scope_{uuid.uuid4().hex[:8]}"
    user_id, library_id, scope_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0032")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES ('{user_id}', 'scope@example.com', 'hash', true, false, true);
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'scope', 'Scope',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'lib_scope'
                );
                """,
            )
        )
        command.upgrade(config, "0033")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO user_library_scopes (
                    id, organization_id, user_id, scope_kind, name, normalized_name
                ) VALUES (
                    '{scope_id}', '{DEFAULT_ORGANIZATION_ID}', '{user_id}',
                    'named', 'Legal', 'legal'
                );
                INSERT INTO user_library_scope_items (id, scope_id, library_id, ordinal)
                VALUES ('{uuid.uuid4()}', '{scope_id}', '{library_id}', 0);
                """,
            )
        )
        assert asyncio.run(
            _execute(
                name,
                "SELECT name, normalized_name FROM user_library_scopes",
            )
        ) == [("Legal", "legal")]
        command.downgrade(config, "0032")
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
                WHERE table_name IN ('user_library_scopes', 'user_library_scope_items')
                """,
            )
        )[0][0] == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
