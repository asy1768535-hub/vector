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


def test_0026_backfills_current_legacy_file_without_moving_and_downgrades(monkeypatch):
    name = "vkt_v08_storage_" + uuid.uuid4().hex[:8]
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0025")
        seed_params = {
            "library_id": library_id,
            "document_id": document_id,
            "revision_id": revision_id,
            "sha": "a" * 64,
        }
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO sys_libraries (
                    id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap,
                    retrieval_mode, qdrant_collection, lifecycle_mode, index_state
                ) VALUES (
                    :library_id, 'storage_pg', 'Storage PG', 'bge-m3', 1024,
                    'cosine', 1000, 120, 'dense', 'lib_storage_pg',
                    'managed', 'ready'
                )
                """,
                seed_params,
            )
        )
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO documents (
                    id, library_id, title, content_hash, current_revision,
                    current_revision_id, latest_revision_id, status
                ) VALUES (
                    :document_id, :library_id, 'Legacy', :sha, 1,
                    :revision_id, :revision_id, 'ready'
                )
                """,
                seed_params,
            )
        )
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO document_revisions (
                    id, document_id, library_id, revision_no, title, content_hash,
                    normalized_text, parser_name, parser_version, chunking_strategy,
                    chunking_strategy_version, status
                ) VALUES (
                    :revision_id, :document_id, :library_id, 1, 'Legacy', :sha,
                    'legacy', 'legacy', 'v1', 'text', 'v1', 'ready'
                )
                """,
                seed_params,
            )
        )
        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO document_files (
                    document_id, revision, file_name, content_type, storage_path,
                    size_bytes, sha256
                ) VALUES (
                    :document_id, 1, 'legacy.txt', 'text/plain',
                    'legacy/path.txt', 6, :sha
                )
                """,
                seed_params,
            )
        )
        command.upgrade(config, "0026")
        row = asyncio.run(
            _execute(
                name,
                """
                SELECT storage_path, storage_provider, endpoint_ref, object_key,
                       immutability_mode, managed_snapshot, verified_at
                FROM document_revision_files
                WHERE document_revision_id = :revision_id
                """,
                {"revision_id": revision_id},
            )
        )[0]
        assert row == (
            "legacy/path.txt",
            "local",
            "primary",
            "legacy/path.txt",
            "content_hash",
            True,
            None,
        )
        command.downgrade(config, "0025")
        columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'document_revision_files'
                  AND column_name IN (
                    'storage_provider', 'endpoint_ref', 'bucket', 'object_key',
                    'object_version', 'etag', 'immutability_mode',
                    'managed_snapshot', 'source_locator', 'verified_at'
                  )
                """,
            )
        )
        assert columns == []
        legacy = asyncio.run(
            _execute(
                name,
                "SELECT storage_path FROM document_revision_files "
                "WHERE document_revision_id = :revision_id",
                {"revision_id": revision_id},
            )
        )[0][0]
        assert legacy == "legacy/path.txt"
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
