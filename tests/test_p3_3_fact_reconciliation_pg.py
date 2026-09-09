"""Optional PostgreSQL gates for the P3.3-A reconciliation schema."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database",
)


def _async_dsn() -> str:
    assert _PG_DSN is not None
    if _PG_DSN.startswith("postgresql://"):
        return _PG_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    if _PG_DSN.startswith("postgresql+psycopg2://"):
        return _PG_DSN.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    return _PG_DSN


def _alembic_environment() -> dict[str, str]:
    url = make_url(_async_dsn())
    environment = os.environ.copy()
    environment.update(
        DB_HOST=url.host or "localhost",
        DB_PORT=str(url.port or 5432),
        DB_USER=url.username or "postgres",
        DB_PASSWORD=url.password or "",
        DB_NAME=url.database or "postgres",
    )
    return environment


def _run_alembic(*arguments: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=_alembic_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_00_postgresql_0073_0074_round_trip_leaves_target_revision() -> None:
    async def current_revision() -> str:
        engine = create_async_engine(_async_dsn())
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return str(value)
        finally:
            await engine.dispose()

    initial = asyncio.run(current_revision())
    assert initial in {"0073", "0074"}
    if initial == "0074":
        _run_alembic("downgrade", "0073")
    _run_alembic("upgrade", "0074")
    _run_alembic("downgrade", "0073")
    _run_alembic("upgrade", "0074")
    assert asyncio.run(current_revision()) == "0074"


def test_postgresql_catalog_has_all_p3_3_constraint_triggers() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        try:
            async with engine.connect() as connection:
                counts = dict(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT tgname, count(*)
                                FROM pg_trigger
                                WHERE NOT tgisinternal AND tgname IN (
                                  'trg_fact_reconciliation_append_only',
                                  'ct_fact_reconciliation_child_guard',
                                  'ct_fact_reconciliation_chain_local',
                                  'ct_fact_reconciliation_logical_fact_pair',
                                  'ct_fact_reconciliation_target_slot_pair',
                                  'trg_fact_reconciliation_logical_fact_guard',
                                  'ct_fact_reconciliation_command_has_decision',
                                  'ct_fact_reconciliation_decision_graph'
                                )
                                GROUP BY tgname
                                """
                            )
                        )
                    ).all()
                )
            assert counts == {
                "trg_fact_reconciliation_append_only": 6,
                "ct_fact_reconciliation_child_guard": 4,
                "ct_fact_reconciliation_chain_local": 5,
                "ct_fact_reconciliation_logical_fact_pair": 1,
                "ct_fact_reconciliation_target_slot_pair": 1,
                "trg_fact_reconciliation_logical_fact_guard": 1,
                "ct_fact_reconciliation_command_has_decision": 1,
                "ct_fact_reconciliation_decision_graph": 1,
            }
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _insert_library(db, library_id: uuid.UUID) -> None:
    await db.execute(
        text(
            """
            INSERT INTO sys_libraries (
              id, slug, name, embedding_model, embedding_dim, qdrant_collection
            ) VALUES (
              :id, :slug, 'P3.3 PostgreSQL test', 'test-model', 3, :collection
            )
            """
        ),
        {
            "id": library_id,
            "slug": f"p33-{library_id.hex}",
            "collection": f"p33_{library_id.hex}",
        },
    )


async def _insert_command(db, library_id: uuid.UUID, command_id: uuid.UUID) -> None:
    await db.execute(
        text(
            """
            INSERT INTO fact_reconciliation_commands (
              id, library_id, idempotency_key, command_identity_fingerprint,
              operation_kind, command_payload_snapshot, contract_version
            ) VALUES (
              :id, :library_id, :idempotency_key, :fingerprint,
              'reconcile', '{}'::jsonb, 'p3_3_fact_reconciliation/v1'
            )
            """
        ),
        {
            "id": command_id,
            "library_id": library_id,
            "idempotency_key": f"p33-{command_id.hex}",
            "fingerprint": "1" * 64,
        },
    )


def test_postgresql_rejects_a_command_without_a_decision() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            with pytest.raises(DBAPIError, match="fact_reconciliation_command_without_decision"):
                async with sessions() as db, db.begin():
                    library_id, command_id = uuid.uuid4(), uuid.uuid4()
                    await _insert_library(db, library_id)
                    await _insert_command(db, library_id, command_id)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_rejects_a_root_cancellation() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            with pytest.raises(DBAPIError, match="fact_reconciliation_lifecycle_invalid"):
                async with sessions() as db, db.begin():
                    library_id, command_id = uuid.uuid4(), uuid.uuid4()
                    await _insert_library(db, library_id)
                    await _insert_command(db, library_id, command_id)
                    await db.execute(
                        text(
                            """
                            INSERT INTO fact_reconciliation_decisions (
                              id, library_id, command_id, decision_payload_fingerprint,
                              operation_kind, requested_effect, evaluated_outcome,
                              lifecycle_status, operation_payload_snapshot,
                              expected_precondition_fingerprint,
                              observed_precondition_fingerprint, reason_code,
                              reason_text, method, confidence, evidence_refs,
                              supersedes_decision_id, actor_type, actor_id, request_id
                            ) VALUES (
                              :id, :library_id, :command_id, :decision_fingerprint,
                              'reconcile', 'cancel', 'cancelled', 'cancelled',
                              CAST(:operation_payload AS jsonb),
                              :expected_fingerprint, :observed_fingerprint, 'test',
                              'root cancellation', 'authorized_cancellation', NULL,
                              '[]'::jsonb, NULL, 'test', 'test', 'p33-root-cancel'
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "library_id": library_id,
                            "command_id": command_id,
                            "decision_fingerprint": "2" * 64,
                            "expected_fingerprint": "3" * 64,
                            "observed_fingerprint": "4" * 64,
                            "operation_payload": '{"control_kind":"cancel_pending","expected_pending_decision_id":null}',
                        },
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_accepts_a_rejected_audit_decision_without_children() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                await db.begin()
                try:
                    library_id, command_id = uuid.uuid4(), uuid.uuid4()
                    await _insert_library(db, library_id)
                    await _insert_command(db, library_id, command_id)
                    await db.execute(
                        text(
                            """
                            INSERT INTO fact_reconciliation_decisions (
                              id, library_id, command_id, decision_payload_fingerprint,
                              operation_kind, requested_effect, evaluated_outcome,
                              lifecycle_status, operation_payload_snapshot,
                              expected_precondition_fingerprint,
                              observed_precondition_fingerprint, reason_code,
                              reason_text, method, confidence, evidence_refs,
                              supersedes_decision_id, actor_type, actor_id, request_id
                            ) VALUES (
                              :id, :library_id, :command_id, :decision_fingerprint,
                              'reconcile', 'stage', 'rejected', 'rejected', '{}'::jsonb,
                              :expected_fingerprint, :observed_fingerprint, 'test',
                              'rejected audit', 'manual', NULL, '[]'::jsonb, NULL,
                              'test', 'test', 'p33-rejected-audit'
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "library_id": library_id,
                            "command_id": command_id,
                            "decision_fingerprint": "5" * 64,
                            "expected_fingerprint": "6" * 64,
                            "observed_fingerprint": "7" * 64,
                        },
                    )
                    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                finally:
                    await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
