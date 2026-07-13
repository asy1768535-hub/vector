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
_ADMIN_URL = (
    make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None
)


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
            if result.returns_rows:
                return result.fetchall()
            return None
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


def test_migration_0021_upgrade_and_downgrade(monkeypatch):
    name = "vkt_v04_m1_" + uuid.uuid4().hex[:8]
    library_id = uuid.uuid4()
    heartbeat_id = uuid.uuid4()
    _configure_alembic(monkeypatch, name)

    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0020")
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
                    :id, 'migration_lib', 'Migration Library', 'bge-m3', 1024,
                    'cosine', 1000, 120, 'dense', 'lib_migration_lib',
                    'managed', 'ready'
                )
                """,
                {"id": library_id},
            )
        )

        command.upgrade(config, "0021")
        version = asyncio.run(
            _execute(name, "SELECT version_num FROM alembic_version")
        )[0][0]
        assert version == "0021"

        defaults = asyncio.run(
            _execute(
                name,
                """
                SELECT graph_extraction_enabled,
                       external_llm_enabled,
                       graph_extraction_allowed_security_levels
                FROM sys_libraries
                WHERE id = :id
                """,
                {"id": library_id},
            )
        )[0]
        assert defaults[0] is False
        assert defaults[1] is False
        assert defaults[2] == []

        tables = asyncio.run(
            _execute(
                name,
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'graph_extraction_jobs',
                    'graph_extraction_units',
                    'extraction_context_snapshots',
                    'extraction_raw_output_attempts'
                  )
                ORDER BY table_name
                """,
            )
        )
        assert {row[0] for row in tables} == {
            "graph_extraction_jobs",
            "graph_extraction_units",
            "extraction_context_snapshots",
            "extraction_raw_output_attempts",
        }

        required_constraints = {
            "ck_heartbeat_service_type",
            "fk_graph_extraction_jobs_library",
            "fk_graph_extraction_jobs_document",
            "fk_graph_extraction_jobs_revision",
            "fk_graph_extraction_jobs_ontology",
            "fk_graph_extraction_jobs_rerun",
            "fk_graph_extraction_jobs_requested_by",
            "uq_graph_extraction_jobs_idempotency_key",
            "ck_graph_extraction_jobs_trigger_type",
            "ck_graph_extraction_jobs_execution_mode",
            "ck_graph_extraction_jobs_status",
            "ck_graph_extraction_jobs_current_stage",
            "ck_graph_extraction_jobs_retry_generation",
            "ck_graph_extraction_jobs_rerun_scope",
            "fk_graph_extraction_units_job",
            "fk_graph_extraction_units_library",
            "fk_graph_extraction_units_revision",
            "fk_graph_extraction_units_center_chunk",
            "fk_graph_extraction_units_center_evidence",
            "uq_graph_extraction_units_job_chunk",
            "uq_graph_extraction_units_job_ordinal",
            "uq_graph_extraction_units_job_fingerprint",
            "ck_graph_extraction_units_status",
            "ck_graph_extraction_units_attempt_count",
            "ck_graph_extraction_units_claim_fields",
            "fk_extraction_context_snapshots_job",
            "fk_extraction_context_snapshots_unit",
            "fk_extraction_context_snapshots_chunk",
            "fk_extraction_context_snapshots_evidence",
            "uq_extraction_context_snapshots_unit",
            "ck_extraction_context_snapshots_payload_or_purged",
            "fk_extraction_raw_attempts_unit",
            "fk_extraction_raw_attempts_context",
            "uq_extraction_raw_attempts_unit_no",
            "ck_extraction_raw_attempts_attempt_no",
            "ck_extraction_raw_attempts_request_status",
            "ck_extraction_raw_attempts_parse_status",
            "ck_extraction_raw_attempts_latency",
            "ck_extraction_raw_attempts_parse_by_request",
            "ck_extraction_raw_attempts_abandoned_fields",
            "ck_extraction_raw_attempts_abandoned_reason",
            "ck_extraction_raw_attempts_payload_or_purged",
        }
        constraints = asyncio.run(
            _execute(
                name,
                """
                SELECT constraint_row.conname
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row
                  ON table_row.oid = constraint_row.conrelid
                WHERE table_row.relname IN (
                    'service_heartbeats',
                    'graph_extraction_jobs',
                    'graph_extraction_units',
                    'extraction_context_snapshots',
                    'extraction_raw_output_attempts'
                )
                """,
            )
        )
        assert required_constraints <= {row[0] for row in constraints}

        rerun_check = asyncio.run(
            _execute(
                name,
                """
                SELECT pg_get_constraintdef(constraint_row.oid)
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row
                  ON table_row.oid = constraint_row.conrelid
                WHERE table_row.relname = 'graph_extraction_jobs'
                  AND constraint_row.conname = 'ck_graph_extraction_jobs_rerun_scope'
                """,
            )
        )[0][0].lower()
        assert "full_rerun" in rerun_check
        assert "rerun_of_job_id is not null" in rerun_check
        assert "repair" in rerun_check
        assert "manual" in rerun_check
        assert "revision_published" in rerun_check
        assert "eval" in rerun_check
        assert "rerun_of_job_id is null" in rerun_check

        context_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'extraction_context_snapshots'
                  AND column_name IN ('created_at', 'updated_at', 'purged_at')
                ORDER BY column_name
                """,
            )
        )
        assert {row[0] for row in context_columns} == {"created_at", "purged_at"}

        required_indexes = {
            "ix_graph_extraction_jobs_status_created",
            "ix_graph_extraction_jobs_library_status_created",
            "ix_graph_extraction_jobs_revision_created",
            "ix_graph_extraction_jobs_rerun_of",
            "ix_graph_extraction_jobs_sensitive_purge",
            "ix_graph_extraction_units_claimable",
            "ix_graph_extraction_units_job_status_ordinal",
            "ix_extraction_context_snapshots_job",
            "ix_extraction_context_snapshots_purge",
            "ix_extraction_raw_attempts_unit_no",
            "ix_extraction_raw_attempts_pending_claim",
            "ix_extraction_raw_attempts_purge",
        }
        indexes = asyncio.run(
            _execute(
                name,
                """
                SELECT indexname
                FROM pg_indexes
                WHERE indexname IN (
                    'ix_graph_extraction_jobs_status_created',
                    'ix_graph_extraction_jobs_library_status_created',
                    'ix_graph_extraction_jobs_revision_created',
                    'ix_graph_extraction_jobs_rerun_of',
                    'ix_graph_extraction_jobs_sensitive_purge',
                    'ix_graph_extraction_units_claimable',
                    'ix_graph_extraction_units_job_status_ordinal',
                    'ix_extraction_context_snapshots_job',
                    'ix_extraction_context_snapshots_purge',
                    'ix_extraction_raw_attempts_unit_no',
                    'ix_extraction_raw_attempts_pending_claim',
                    'ix_extraction_raw_attempts_purge'
                )
                """,
            )
        )
        assert {row[0] for row in indexes} == required_indexes

        asyncio.run(
            _execute(
                name,
                """
                INSERT INTO service_heartbeats (
                    id, service_type, instance_id, hostname, pid,
                    started_at, status
                ) VALUES (
                    :id, 'graph_extractor', 'test-instance', 'host', 1,
                    NOW(), 'online'
                )
                """,
                {"id": heartbeat_id},
            )
        )

        command.downgrade(config, "0020")
        downgraded_version = asyncio.run(
            _execute(name, "SELECT version_num FROM alembic_version")
        )[0][0]
        assert downgraded_version == "0020"
        remaining = asyncio.run(
            _execute(
                name,
                """
                SELECT to_regclass('public.graph_extraction_jobs'),
                       to_regclass('public.graph_extraction_units'),
                       to_regclass('public.extraction_context_snapshots'),
                       to_regclass('public.extraction_raw_output_attempts')
                """,
            )
        )[0]
        assert tuple(remaining) == (None, None, None, None)
        graph_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'sys_libraries'
                  AND column_name IN (
                    'graph_extraction_enabled',
                    'external_llm_enabled',
                    'graph_extraction_allowed_security_levels'
                  )
                """,
            )
        )[0][0]
        assert graph_columns == 0
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
