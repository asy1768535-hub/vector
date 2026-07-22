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


def test_0035_round_trip_failure_shape_and_effective_revision_constraint(monkeypatch):
    name = f"vkt_v08_decisions_{uuid.uuid4().hex[:8]}"
    user_id, library_id = uuid.uuid4(), uuid.uuid4()
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    taxonomy_id, label_id = uuid.uuid4(), uuid.uuid4()
    run_id, proposal_id = uuid.uuid4(), uuid.uuid4()
    first_set_id, second_set_id = uuid.uuid4(), uuid.uuid4()
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
                ) VALUES ('{user_id}', 'decision@example.com', 'hash', true, false, true);
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'decision', 'Decision',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'lib_decision'
                );
                INSERT INTO documents (
                    id, library_id, title, content_hash, current_revision,
                    current_revision_id, latest_revision_id, status
                ) VALUES (
                    '{document_id}', '{library_id}', 'Decision document', '{'a' * 64}', 1,
                    '{revision_id}', '{revision_id}', 'ready'
                );
                INSERT INTO document_revisions (
                    id, document_id, library_id, revision_no, title, content_hash,
                    parser_name, parser_version, chunking_strategy,
                    chunking_strategy_version, status, finished_at
                ) VALUES (
                    '{revision_id}', '{document_id}', '{library_id}', 1,
                    'Decision document', '{'a' * 64}', 'test', '1', 'fixed', '1',
                    'ready', now()
                );
                INSERT INTO classification_taxonomies (
                    id, organization_id, version_key, version_no, status,
                    created_by_user_id, activated_by_user_id, activated_at
                ) VALUES (
                    '{taxonomy_id}', '{DEFAULT_ORGANIZATION_ID}', 'document-category', 1,
                    'active', '{user_id}', '{user_id}', now()
                );
                INSERT INTO classification_labels (
                    id, taxonomy_version_id, key, label, sort_order, status
                ) VALUES ('{label_id}', '{taxonomy_id}', 'legal', 'Legal', 0, 'active');
                INSERT INTO library_classification_labels (
                    id, library_id, taxonomy_version_id, label_id, ordinal
                ) VALUES ('{uuid.uuid4()}', '{library_id}', '{taxonomy_id}', '{label_id}', 0);
                INSERT INTO document_classification_runs (
                    id, library_id, document_id, document_revision_id,
                    revision_content_hash, taxonomy_version_id, enabled_label_set_hash,
                    policy_version, min_confidence_micros, min_margin_micros,
                    max_secondary_labels, classifier_version, model_provider, model_name,
                    model_config_hash, prompt_version, input_fingerprint, idempotency_key,
                    generation_no, retry_generation, trigger_type, status, error_code,
                    requested_by_user_id, finished_at
                ) VALUES (
                    '{run_id}', '{library_id}', '{document_id}', '{revision_id}',
                    '{'a' * 64}', '{taxonomy_id}', '{'b' * 64}',
                    'classification-policy-v1', 900000, 150000, 8, 'classifier-v1',
                    'local', 'model-v1', '{'c' * 64}', 'prompt-v1', '{'d' * 64}',
                    '{'e' * 64}', 1, 0, 'revision_ready', 'failed', 'provider_timeout',
                    '{user_id}', now()
                );
                INSERT INTO document_classification_proposals (
                    id, run_id, label_id, role, rank, confidence_micros, status
                ) VALUES (
                    '{proposal_id}', '{run_id}', '{label_id}', 'primary', 0, 900000,
                    'pending_review'
                );
                INSERT INTO document_classification_decision_sets (
                    id, library_id, document_id, document_revision_id,
                    taxonomy_version_id, source, lifecycle, generation_no,
                    reviewed_by_user_id, reviewed_at
                ) VALUES (
                    '{first_set_id}', '{library_id}', '{document_id}', '{revision_id}',
                    '{taxonomy_id}', 'manual', 'effective', 1, '{user_id}', now()
                );
                INSERT INTO document_classification_decisions (
                    id, decision_set_id, label_id, role, ordinal
                ) VALUES ('{uuid.uuid4()}', '{first_set_id}', '{label_id}', 'primary', 0);
                """,
            )
        )
        with pytest.raises(
            Exception,
            match="uq_document_classification_decision_sets_effective_revision",
        ):
            asyncio.run(
                _execute(
                    name,
                    f"""
                    INSERT INTO document_classification_decision_sets (
                        id, library_id, document_id, document_revision_id,
                        taxonomy_version_id, source, lifecycle, generation_no,
                        reviewed_by_user_id, reviewed_at
                    ) VALUES (
                        '{second_set_id}', '{library_id}', '{document_id}', '{revision_id}',
                        '{taxonomy_id}', 'manual', 'effective', 2, '{user_id}', now()
                    )
                    """,
                )
            )
        asyncio.run(
            _execute(
                name,
                f"""
                UPDATE document_classification_decision_sets
                SET lifecycle = 'superseded', superseded_at = now()
                WHERE id = '{first_set_id}';
                INSERT INTO document_classification_decision_sets (
                    id, library_id, document_id, document_revision_id,
                    taxonomy_version_id, source, lifecycle, generation_no,
                    reviewed_by_user_id, reviewed_at, supersedes_decision_set_id
                ) VALUES (
                    '{second_set_id}', '{library_id}', '{document_id}', '{revision_id}',
                    '{taxonomy_id}', 'manual', 'effective', 2, '{user_id}', now(),
                    '{first_set_id}'
                );
                """,
            )
        )
        assert asyncio.run(
            _execute(
                name,
                f"""
                SELECT status, error_code FROM document_classification_runs
                WHERE id = '{run_id}'
                """,
            )
        ) == [("failed", "provider_timeout")]
        command.downgrade(config, "0034")
        assert asyncio.run(
            _execute(name, f"SELECT id FROM documents WHERE id = '{document_id}'")
        ) == [(document_id,)]
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_name LIKE 'document_classification_%'
                """,
            )
        )[0][0] == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
