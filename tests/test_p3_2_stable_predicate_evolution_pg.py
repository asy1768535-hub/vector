"""Optional PostgreSQL gates for P3.2 predicate evolution.

The configured DSN must point to a disposable database at revision 0072 or
0073. The migration round trip intentionally changes that database and leaves
it at 0073; no application or production DSN fallback is allowed.
"""

from __future__ import annotations

import asyncio
import json
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

from app.services.graph_identity_locks import (
    STABLE_PREDICATE_LOCK_SCOPE,
    GraphIdentityLockBusy,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)

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


def test_00_postgresql_0072_0073_round_trip_leaves_target_revision() -> None:
    async def current_revision() -> str:
        engine = create_async_engine(_async_dsn())
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return str(value)
        finally:
            await engine.dispose()

    initial = asyncio.run(current_revision())
    assert initial in {"0072", "0073"}
    if initial == "0073":
        _run_alembic("downgrade", "0072")
    _run_alembic("upgrade", "0073")
    _run_alembic("downgrade", "0072")
    _run_alembic("upgrade", "0073")
    assert asyncio.run(current_revision()) == "0073"


def test_postgresql_catalog_has_all_p3_2_constraint_triggers() -> None:
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
                                  'trg_stable_predicate_evolution_append_only',
                                  'trg_stable_predicate_evolution_child_guard',
                                  'ct_stable_predicate_evolution_chain_local',
                                  'ct_stable_predicate_mapping_evolution_pair',
                                  'ct_stable_predicate_identity_evolution_guard',
                                  'ct_stable_predicate_evolution_command_has_decision',
                                  'ct_stable_predicate_evolution_decision_graph'
                                )
                                GROUP BY tgname
                                """
                            )
                        )
                    ).all()
                )
            assert counts == {
                "trg_stable_predicate_evolution_append_only": 5,
                "trg_stable_predicate_evolution_child_guard": 3,
                "ct_stable_predicate_evolution_chain_local": 3,
                "ct_stable_predicate_mapping_evolution_pair": 2,
                "ct_stable_predicate_identity_evolution_guard": 1,
                "ct_stable_predicate_evolution_command_has_decision": 1,
                "ct_stable_predicate_evolution_decision_graph": 1,
            }
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_fail_fast_partial_advisory_locks_release_on_outer_rollback() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn(), pool_size=3, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        first_scope = GraphIdentityLockScope(
            STABLE_PREDICATE_LOCK_SCOPE,
            uuid.UUID("00000000-0000-4000-8000-000000000001"),
        )
        busy_scope = GraphIdentityLockScope(
            STABLE_PREDICATE_LOCK_SCOPE,
            uuid.UUID("00000000-0000-4000-8000-000000000002"),
        )
        try:
            async with sessions() as holder, sessions() as contender, sessions() as observer:
                await holder.begin()
                await contender.begin()
                await lock_graph_identity_scopes(holder, library_id, (busy_scope,))

                with pytest.raises(GraphIdentityLockBusy):
                    await lock_graph_identity_scopes(
                        contender,
                        library_id,
                        (first_scope, busy_scope),
                        wait=False,
                    )

                await observer.begin()
                with pytest.raises(GraphIdentityLockBusy):
                    await lock_graph_identity_scopes(
                        observer,
                        library_id,
                        (first_scope,),
                        wait=False,
                    )
                await observer.rollback()

                await contender.rollback()
                await observer.begin()
                await lock_graph_identity_scopes(
                    observer,
                    library_id,
                    (first_scope,),
                    wait=False,
                )
                await observer.rollback()
                await holder.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_rejects_cancellation_of_rejected_predecessor() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        command_id = uuid.uuid4()
        predecessor_id = uuid.uuid4()
        source_id, survivor_id = uuid.uuid4(), uuid.uuid4()
        command_payload = json.dumps(
            {
                "schema": "p3_2_stable_predicate_evolution_command_v1",
                "operation": "merge",
                "library_id": str(library_id),
                "source_predicate_ids": [str(source_id)],
                "survivor_predicate_id": str(survivor_id),
            }
        )
        rejected_payload = json.dumps(
            {
                "mapping_assignments": [],
                "policy_compatibility": [],
                "survivor_predicate_id": str(survivor_id),
            }
        )
        cancellation_payload = json.dumps(
            {
                "control_kind": "cancel_pending",
                "expected_pending_decision_id": str(predecessor_id),
            }
        )
        try:
            with pytest.raises(
                DBAPIError,
                match="predicate_evolution_cancellation_predecessor_invalid",
            ):
                async with sessions() as db, db.begin():
                    await db.execute(
                        text(
                            """
                            INSERT INTO sys_libraries (
                              id, slug, name, embedding_model, embedding_dim,
                              qdrant_collection
                            ) VALUES (
                              :library_id, :slug, 'P3.2 lifecycle test',
                              'test-model', 3, :collection
                            )
                            """
                        ),
                        {
                            "library_id": library_id,
                            "slug": f"p32-{library_id.hex}",
                            "collection": f"p32_{library_id.hex}",
                        },
                    )
                    await db.execute(
                        text(
                            """
                            INSERT INTO stable_predicate_evolution_commands (
                              id, library_id, idempotency_key,
                              command_identity_fingerprint, operation_kind,
                              command_payload_snapshot, contract_version
                            ) VALUES (
                              :id, :library_id, 'invalid-cancel', :fingerprint,
                              'merge', CAST(:payload AS jsonb),
                              'p3_2_stable_predicate_evolution/v1'
                            )
                            """
                        ),
                        {
                            "id": command_id,
                            "library_id": library_id,
                            "fingerprint": "1" * 64,
                            "payload": command_payload,
                        },
                    )
                    common = {
                        "library_id": library_id,
                        "command_id": command_id,
                        "actor_id": uuid.uuid4(),
                    }
                    await db.execute(
                        text(
                            """
                            INSERT INTO stable_predicate_evolution_decisions (
                              id, library_id, command_id,
                              decision_payload_fingerprint, operation_kind,
                              requested_effect, evaluated_outcome, lifecycle_status,
                              operation_payload_snapshot, reason_code, reason_text,
                              method, confidence, evidence_refs,
                              expected_precondition_fingerprint,
                              observed_precondition_fingerprint,
                              supersedes_decision_id, actor_type, actor_id, request_id
                            ) VALUES (
                              :id, :library_id, :command_id, :fingerprint, 'merge',
                              'apply', 'rejected', 'rejected', CAST(:payload AS jsonb),
                              'policy_incompatible', 'rejected predecessor', 'manual',
                              NULL, '[]'::jsonb, :expected, :observed, NULL,
                              'service', :actor_id, 'p32:rejected'
                            )
                            """
                        ),
                        {
                            **common,
                            "id": predecessor_id,
                            "fingerprint": "2" * 64,
                            "payload": rejected_payload,
                            "expected": "3" * 64,
                            "observed": "4" * 64,
                        },
                    )
                    await db.execute(
                        text(
                            "UPDATE stable_predicate_evolution_decisions "
                            "SET lifecycle_status='superseded' WHERE id=:id"
                        ),
                        {"id": predecessor_id},
                    )
                    await db.execute(
                        text(
                            """
                            INSERT INTO stable_predicate_evolution_decisions (
                              id, library_id, command_id,
                              decision_payload_fingerprint, operation_kind,
                              requested_effect, evaluated_outcome, lifecycle_status,
                              operation_payload_snapshot, reason_code, reason_text,
                              method, confidence, evidence_refs,
                              expected_precondition_fingerprint,
                              observed_precondition_fingerprint,
                              supersedes_decision_id, actor_type, actor_id, request_id
                            ) VALUES (
                              :id, :library_id, :command_id, :fingerprint, 'merge',
                              'cancel', 'cancelled', 'cancelled', CAST(:payload AS jsonb),
                              'incorrect_pending_intent', 'invalid cancellation',
                              'authorized_cancellation', NULL, '[]'::jsonb,
                              :expected, :observed, :predecessor_id,
                              'service', :actor_id, 'p32:invalid-cancel'
                            )
                            """
                        ),
                        {
                            **common,
                            "id": uuid.uuid4(),
                            "fingerprint": "5" * 64,
                            "payload": cancellation_payload,
                            "expected": "4" * 64,
                            "observed": "6" * 64,
                            "predecessor_id": predecessor_id,
                        },
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
