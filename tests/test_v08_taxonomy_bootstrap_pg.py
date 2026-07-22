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


def test_0037_round_trip_run_source_constraints_and_draft_preservation(monkeypatch):
    name = f"vkt_v08_tax_bootstrap_{uuid.uuid4().hex[:8]}"
    user_id, library_id = uuid.uuid4(), uuid.uuid4()
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    taxonomy_id, direct_run_id, llm_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0036")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES ('{user_id}', 'bootstrap@example.com', 'hash', true, false, true);
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'bootstrap', 'Bootstrap',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'lib_bootstrap'
                );
                INSERT INTO documents (
                    id, library_id, title, content_hash, current_revision,
                    current_revision_id, latest_revision_id, status
                ) VALUES (
                    '{document_id}', '{library_id}', 'Sample', '{'a' * 64}', 1,
                    '{revision_id}', '{revision_id}', 'ready'
                );
                INSERT INTO document_revisions (
                    id, document_id, library_id, revision_no, title, content_hash,
                    parser_name, parser_version, chunking_strategy,
                    chunking_strategy_version, security_level, status, finished_at
                ) VALUES (
                    '{revision_id}', '{document_id}', '{library_id}', 1, 'Sample',
                    '{'a' * 64}', 'test', '1', 'fixed', '1', 'internal', 'ready', now()
                );
                INSERT INTO classification_taxonomies (
                    id, organization_id, version_key, version_no, status,
                    created_by_user_id, description
                ) VALUES (
                    '{taxonomy_id}', '{DEFAULT_ORGANIZATION_ID}', 'document-category', 1,
                    'draft', '{user_id}', 'Bootstrap output'
                );
                """,
            )
        )
        command.upgrade(config, "0037")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO classification_taxonomy_bootstrap_runs (
                    id, organization_id, request_id, source_type, source_key,
                    source_version, source_hash, input_fingerprint, idempotency_key,
                    status, output_taxonomy_id, created_by_user_id, started_at, finished_at
                ) VALUES (
                    '{direct_run_id}', '{DEFAULT_ORGANIZATION_ID}', '{uuid.uuid4()}',
                    'builtin_template', 'general-enterprise', '1.0.0', '{'b' * 64}',
                    '{'c' * 64}', '{'d' * 64}', 'succeeded', '{taxonomy_id}',
                    '{user_id}', now(), now()
                );
                INSERT INTO classification_taxonomy_bootstrap_runs (
                    id, organization_id, request_id, source_type, source_key,
                    source_version, source_hash, input_fingerprint, idempotency_key,
                    status, model_provider, model_name, model_config_hash,
                    prompt_version, attempt_token, expires_at, created_by_user_id, started_at
                ) VALUES (
                    '{llm_run_id}', '{DEFAULT_ORGANIZATION_ID}', '{uuid.uuid4()}',
                    'llm_proposal', 'organization-revision-samples', '1', '{'e' * 64}',
                    '{'f' * 64}', '{'1' * 64}', 'processing', 'deepseek',
                    'deepseek-v4-pro', '{'2' * 64}', 'taxonomy-bootstrap-v1',
                    '{uuid.uuid4()}', now() + interval '3 minutes', '{user_id}', now()
                );
                INSERT INTO classification_taxonomy_bootstrap_sources (
                    id, bootstrap_run_id, ordinal, library_id, document_id,
                    document_revision_id, revision_content_hash, security_level
                ) VALUES (
                    '{uuid.uuid4()}', '{llm_run_id}', 0, '{library_id}', '{document_id}',
                    '{revision_id}', '{'a' * 64}', 'internal'
                );
                """,
            )
        )
        rows = asyncio.run(
            _execute(
                name,
                """
                SELECT r.status, count(s.id)
                FROM classification_taxonomy_bootstrap_runs r
                LEFT JOIN classification_taxonomy_bootstrap_sources s
                  ON s.bootstrap_run_id = r.id
                GROUP BY r.status ORDER BY r.status
                """,
            )
        )
        assert rows == [("processing", 1), ("succeeded", 0)]
        with pytest.raises(Exception, match="uq_taxonomy_bootstrap_one_processing_org"):
            asyncio.run(
                _execute(
                    name,
                    f"""
                    INSERT INTO classification_taxonomy_bootstrap_runs (
                        id, organization_id, request_id, source_type, source_key,
                        source_version, source_hash, input_fingerprint, idempotency_key,
                        status, model_provider, model_name, model_config_hash,
                        prompt_version, attempt_token, expires_at, created_by_user_id, started_at
                    ) VALUES (
                        '{uuid.uuid4()}', '{DEFAULT_ORGANIZATION_ID}', '{uuid.uuid4()}',
                        'llm_proposal', 'organization-revision-samples', '1', '{'3' * 64}',
                        '{'4' * 64}', '{'5' * 64}', 'processing', 'deepseek',
                        'deepseek-v4-pro', '{'6' * 64}', 'taxonomy-bootstrap-v1',
                        '{uuid.uuid4()}', now() + interval '3 minutes', '{user_id}', now()
                    )
                    """,
                )
            )
        command.downgrade(config, "0036")
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_name IN (
                    'classification_taxonomy_bootstrap_runs',
                    'classification_taxonomy_bootstrap_sources'
                )
                """,
            )
        )[0][0] == 0
        assert asyncio.run(
            _execute(
                name,
                f"SELECT status FROM classification_taxonomies WHERE id = '{taxonomy_id}'",
            )
        ) == [("draft",)]
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
