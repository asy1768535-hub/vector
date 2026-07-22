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


def test_0036_round_trip_defaults_claim_shape_and_heartbeat(monkeypatch):
    name = f"vkt_v08_classifier_{uuid.uuid4().hex[:8]}"
    user_id, library_id = uuid.uuid4(), uuid.uuid4()
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    taxonomy_id, job_id = uuid.uuid4(), uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0035")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES ('{user_id}', 'classifier@example.com', 'hash', true, false, true);
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'classifier', 'Classifier',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'lib_classifier'
                );
                INSERT INTO documents (
                    id, library_id, title, content_hash, current_revision,
                    current_revision_id, latest_revision_id, status
                ) VALUES (
                    '{document_id}', '{library_id}', 'Classifier document', '{'a' * 64}', 1,
                    '{revision_id}', '{revision_id}', 'ready'
                );
                INSERT INTO document_revisions (
                    id, document_id, library_id, revision_no, title, content_hash,
                    parser_name, parser_version, chunking_strategy,
                    chunking_strategy_version, security_level, status, finished_at
                ) VALUES (
                    '{revision_id}', '{document_id}', '{library_id}', 1,
                    'Classifier document', '{'a' * 64}', 'test', '1', 'fixed', '1',
                    'internal', 'ready', now()
                );
                INSERT INTO classification_taxonomies (
                    id, organization_id, version_key, version_no, status,
                    created_by_user_id, activated_by_user_id, activated_at
                ) VALUES (
                    '{taxonomy_id}', '{DEFAULT_ORGANIZATION_ID}', 'document-category', 1,
                    'active', '{user_id}', '{user_id}', now()
                );
                """,
            )
        )
        command.upgrade(config, "0036")
        defaults = asyncio.run(
            _execute(
                name,
                f"""
                SELECT classification_auto_enabled,
                       classification_external_model_enabled,
                       classification_allowed_security_levels
                FROM sys_libraries WHERE id = '{library_id}'
                """,
            )
        )[0]
        assert defaults == (False, False, [])
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO document_classification_jobs (
                    id, library_id, document_id, document_revision_id,
                    revision_content_hash, taxonomy_version_id, enabled_label_set_hash,
                    classifier_version, model_provider, model_name, model_config_hash,
                    prompt_version, input_fingerprint, idempotency_key,
                    retry_generation, trigger_type, status, attempt_count
                ) VALUES (
                    '{job_id}', '{library_id}', '{document_id}', '{revision_id}',
                    '{'a' * 64}', '{taxonomy_id}', '{'b' * 64}', 'document-classifier-v1',
                    'deepseek', 'deepseek-v4-pro', '{'c' * 64}',
                    'classification-prompt-v1', '{'d' * 64}', '{'e' * 64}',
                    0, 'revision_ready', 'queued', 0
                );
                INSERT INTO service_heartbeats (
                    id, service_type, instance_id, hostname, pid, started_at
                ) VALUES (
                    '{uuid.uuid4()}', 'classification_worker', 'classifier-test',
                    'host', 1, now()
                );
                """,
            )
        )
        with pytest.raises(Exception, match="ck_document_classification_jobs_claim_state"):
            asyncio.run(
                _execute(
                    name,
                    f"""
                    UPDATE document_classification_jobs
                    SET status = 'processing', attempt_count = 1
                    WHERE id = '{job_id}'
                    """,
                )
            )
        command.downgrade(config, "0035")
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_name = 'document_classification_jobs'
                """,
            )
        )[0][0] == 0
        assert asyncio.run(
            _execute(
                name,
                f"SELECT id FROM document_classification_runs WHERE library_id = '{library_id}'",
            )
        ) == []
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'sys_libraries'
                  AND column_name LIKE 'classification_%'
                  AND column_name IN (
                    'classification_auto_enabled',
                    'classification_external_model_enabled',
                    'classification_allowed_security_levels'
                  )
                """,
            )
        )[0][0] == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
