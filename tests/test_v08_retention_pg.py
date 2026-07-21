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
    from app.config import settings

    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def test_0027_upgrades_and_downgrades_exactly(monkeypatch):
    name = "vkt_v08_retention_" + uuid.uuid4().hex[:8]
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0026")
        command.upgrade(config, "0027")
        columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'revision_retention_records'
                ORDER BY column_name
                """,
            )
        )
        names = {row[0] for row in columns}
        assert {
            "revision_file_id",
            "cleanup_not_before",
            "impact_snapshot",
            "impact_hash",
            "hold_reason_code",
        } <= names
        library_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'sys_libraries'
                  AND column_name LIKE 'revision_retention%'
                """,
            )
        )
        assert {row[0] for row in library_columns} == {
            "revision_retention_enabled",
            "revision_retention_days",
            "revision_retention_notice_days",
        }
        command.downgrade(config, "0026")
        assert asyncio.run(
            _execute(
                name,
                "SELECT to_regclass('public.revision_retention_records')",
            )
        )[0][0] is None
        library_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'sys_libraries'
                  AND column_name LIKE 'revision_retention%'
                """,
            )
        )
        assert library_columns == []
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
