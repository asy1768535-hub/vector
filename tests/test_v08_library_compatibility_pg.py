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


def _create_database(monkeypatch, prefix: str) -> str:
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    return name


def _drop_database(name: str) -> None:
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_0032_snapshot_round_trip_preserves_library(monkeypatch):
    name = _create_database(monkeypatch, "vkt_v08_compatibility")
    library_id = uuid.uuid4()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0031")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_libraries (
                    id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', 'compatibility-pg', 'Compatibility PG', 'bge-m3',
                    1024, 'cosine', 1000, 120, 'lib_compatibility_pg'
                )
                """,
            )
        )
        command.upgrade(config, "0032")
        asyncio.run(
            _execute(
                name,
                f"""
                UPDATE sys_libraries SET
                    embedding_probe_contract_version = 'embedding-probe-v1',
                    embedding_probe_model = 'bge-m3',
                    embedding_probe_dimension = 1024,
                    embedding_probe_endpoint_sha256 = '{'a' * 64}',
                    embedding_probe_fingerprint = '{'b' * 64}',
                    embedding_probe_verified_at = now()
                WHERE id = '{library_id}'
                """,
            )
        )
        snapshot = asyncio.run(
            _execute(
                name,
                f"""
                SELECT embedding_probe_model, embedding_probe_dimension,
                       embedding_probe_fingerprint
                FROM sys_libraries WHERE id = '{library_id}'
                """,
            )
        )
        assert snapshot == [("bge-m3", 1024, "b" * 64)]

        command.downgrade(config, "0031")
        assert asyncio.run(
            _execute(
                name,
                f"SELECT id, slug FROM sys_libraries WHERE id = '{library_id}'",
            )
        ) == [(library_id, "compatibility-pg")]
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'sys_libraries'
                  AND column_name LIKE 'embedding_probe_%'
                """,
            )
        )[0][0] == 0
    finally:
        _drop_database(name)
