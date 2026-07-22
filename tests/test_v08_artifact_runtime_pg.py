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


async def _execute(name: str, sql: str, params: dict | None = None):
    engine = create_async_engine(_database_url(name))
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql), params or {})
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


def test_0025_upgrade_constraints_defaults_and_downgrade(monkeypatch):
    name = "vkt_v08_runtime_" + uuid.uuid4().hex[:8]
    library_id = uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0024")
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO sys_libraries (
                    id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap,
                    retrieval_mode, qdrant_collection,
                    lifecycle_mode, index_state
                ) VALUES (
                    :id, 'v08_runtime', 'v08 Runtime', 'bge-m3', 1024,
                    'cosine', 1000, 120, 'dense', 'lib_v08_runtime',
                    'managed', 'ready'
                )
                """,
                {"id": library_id},
            )
        )

        command.upgrade(config, "0025")
        version = asyncio.run(
            _execute(name, "SELECT version_num FROM alembic_version")
        )[0][0]
        assert version == "0025"
        defaults = asyncio.run(
            _execute(
                name,
                """
                SELECT knowledge_artifact_auto_enabled,
                       summary_artifact_enabled,
                       outline_artifact_enabled,
                       knowledge_artifact_external_model_enabled,
                       knowledge_artifact_allowed_security_levels
                FROM sys_libraries WHERE id = :id
                """,
                {"id": library_id},
            )
        )[0]
        assert defaults == (False, False, False, False, [])
        constraints = {
            row[0]
            for row in asyncio.run(
                _execute(
                    name,
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conname IN (
                        'ck_knowledge_artifact_jobs_attempt_count',
                        'ck_knowledge_artifact_jobs_claim_state',
                        'ck_lib_knowledge_artifact_security_levels_array',
                        'ck_heartbeat_service_type'
                    )
                    """,
                )
            )
        }
        assert constraints == {
            "ck_knowledge_artifact_jobs_attempt_count",
            "ck_knowledge_artifact_jobs_claim_state",
            "ck_lib_knowledge_artifact_security_levels_array",
            "ck_heartbeat_service_type",
        }
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO service_heartbeats (
                    id, service_type, instance_id, hostname, pid, started_at
                ) VALUES (
                    :id, 'knowledge_artifact_worker', 'runtime-test', 'host', 1, now()
                )
                """,
                {"id": uuid.uuid4()},
            )
        )

        command.downgrade(config, "0024")
        version = asyncio.run(
            _execute(name, "SELECT version_num FROM alembic_version")
        )[0][0]
        assert version == "0024"
        runtime_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND (
                    (table_name = 'knowledge_artifact_jobs'
                     AND column_name IN (
                        'attempt_count', 'claim_token', 'claimed_by',
                        'lease_expires_at', 'last_heartbeat_at'
                     ))
                    OR
                    (table_name = 'sys_libraries'
                     AND column_name IN (
                        'knowledge_artifact_auto_enabled',
                        'summary_artifact_enabled',
                        'outline_artifact_enabled',
                        'knowledge_artifact_external_model_enabled',
                        'knowledge_artifact_allowed_security_levels'
                     ))
                  )
                """,
            )
        )
        assert runtime_columns == []
        heartbeat_rows = asyncio.run(
            _execute(
                name,
                "SELECT count(*) FROM service_heartbeats "
                "WHERE service_type = 'knowledge_artifact_worker'",
            )
        )[0][0]
        assert heartbeat_rows == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
