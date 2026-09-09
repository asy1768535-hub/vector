"""Optional PostgreSQL gates for the P3.3-A reconciliation schema."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.fact_reconciliation import (
    FactReconciliationAssignmentInput,
    FactReconciliationCancellation,
    FactReconciliationCommand,
    FactReconciliationRetryableConflict,
    FactReconciliationTargetSlotInput,
    FactReconciliationTargetSpec,
    FactReconciliationTopology,
    apply_fact_reconciliation,
    build_fact_reconciliation_precondition_fingerprint,
    cancel_pending_fact_reconciliation,
    resolve_current_logical_fact,
)
from app.services.graph_identity_locks import (
    LOGICAL_FACT_LOCK_SCOPE,
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


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


async def _seed_reconciliation_fixture(
    db,
) -> tuple[FactReconciliationCommand, uuid.UUID, str]:
    """Persist the P2 prerequisites only; callers retain transaction ownership."""

    library_id = uuid.uuid4()
    source_fact_id = uuid.uuid4()
    source_assertion_id = uuid.uuid4()
    predicate_id = uuid.uuid4()
    source_subject_id = uuid.uuid4()
    target_subject_id = uuid.uuid4()
    occurrence = {
        "schema_version": "graph_relation_source_occurrence_v1",
        "library_id": str(library_id),
        "document_id": str(uuid.uuid4()),
        "document_revision_id": str(uuid.uuid4()),
        "evidence_id": str(uuid.uuid4()),
        "chunk_id": str(uuid.uuid4()),
        "block_id": None,
        "source_span": {"start": 0, "end": 1},
    }
    await _insert_library(db, library_id)
    await db.execute(
        text(
            """
            INSERT INTO canonical_entities (
              id, library_id, canonical_name, normalized_name, status
            ) VALUES
              (:source_subject_id, :library_id, 'P3.3 source', 'p3.3 source', 'active'),
              (:target_subject_id, :library_id, 'P3.3 target', 'p3.3 target', 'active')
            """
        ),
        {
            "library_id": library_id,
            "source_subject_id": source_subject_id,
            "target_subject_id": target_subject_id,
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO stable_predicate_identities (
              id, library_id, namespace, key, contract_version, temporal_class,
              identity_policy_version, resolution_status, resolution_policy
            ) VALUES (
              :predicate_id, :library_id, 'p3_3_test', :predicate_key, 'v1', 'static_fact',
              'p3_3_test', 'resolved', '{}'::jsonb
            )
            """
        ),
        {"library_id": library_id, "predicate_id": predicate_id, "predicate_key": uuid.uuid4().hex},
    )
    await db.execute(
        text(
            """
            INSERT INTO logical_facts (
              id, library_id, stable_predicate_identity_id, subject_canonical_entity_id,
              identity_qualifiers, identity_policy_version, identity_fingerprint, status
            ) VALUES (
              :source_fact_id, :library_id, :predicate_id, :source_subject_id,
              '{}'::jsonb, 'p3_3_test', :source_fact_fingerprint, 'active'
            )
            """
        ),
        {
            "library_id": library_id,
            "source_fact_id": source_fact_id,
            "predicate_id": predicate_id,
            "source_subject_id": source_subject_id,
            "source_fact_fingerprint": _fingerprint(f"source-fact:{source_fact_id}"),
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO fact_assertions (
              id, library_id, logical_fact_id, assertion_fingerprint, polarity, modality,
              qualifiers, status, source_kind
            ) VALUES (
              :source_assertion_id, :library_id, :source_fact_id, :assertion_fingerprint,
              'affirmed', 'confirmed', '{}'::jsonb, 'active', 'manual'
            )
            """
        ),
        {
            "library_id": library_id,
            "source_fact_id": source_fact_id,
            "source_assertion_id": source_assertion_id,
            "assertion_fingerprint": _fingerprint(f"source-assertion:{source_assertion_id}"),
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO fact_resolution_decisions (
              id, library_id, source_kind, subject_fingerprint, decision_fingerprint,
              stable_predicate_identity_id, logical_fact_id, fact_assertion_id,
              source_snapshot, candidate_snapshot, evidence_refs, status, method,
              reason_code, resolver_version
            ) VALUES (
              :decision_id, :library_id, 'manual', :subject_fingerprint, :decision_fingerprint,
              :predicate_id, :source_fact_id, :source_assertion_id,
              CAST(:source_snapshot AS jsonb), '{}'::jsonb, '[]'::jsonb, 'resolved', 'manual',
              'p3_3_test', 'p3_3_test'
            )
            """
        ),
        {
            "library_id": library_id,
            "decision_id": uuid.uuid4(),
            "predicate_id": predicate_id,
            "source_fact_id": source_fact_id,
            "source_assertion_id": source_assertion_id,
            "subject_fingerprint": _fingerprint(f"subject:{source_assertion_id}"),
            "decision_fingerprint": _fingerprint(f"decision:{source_assertion_id}"),
            "source_snapshot": json.dumps(occurrence),
        },
    )
    target = FactReconciliationTargetSpec(
        stable_predicate_identity_id=predicate_id,
        subject_canonical_entity_id=target_subject_id,
        object_kind=None,
        object_canonical_entity_id=None,
        object_value={"value": "reconciled"},
        identity_qualifiers={},
        temporal_identity_key=None,
        identity_policy_version="p3_3_test",
    )
    command = FactReconciliationCommand(
        library_id=library_id,
        source_logical_fact_ids=(source_fact_id,),
        source_target_topology=(FactReconciliationTopology(source_fact_id, ("target",)),),
        target_slots=(FactReconciliationTargetSlotInput("target", "new", target),),
        assignments=(
            FactReconciliationAssignmentInput(
                source_fact_id, source_assertion_id, "resolved", "target"
            ),
        ),
        requested_effect="apply",
        idempotency_key=f"p3-3-pg-{uuid.uuid4()}",
        reason_code="p3_3_pg_acceptance",
        reason_text="Exercise reconciliation through PostgreSQL.",
        method="manual",
        evidence_refs=({"test": "p3_3_postgresql"},),
        actor_type="test",
        actor_id="p3_3_postgresql",
        request_id=f"p3-3-pg-{uuid.uuid4()}",
    )
    return command, source_assertion_id, _fingerprint(f"source-fact:{source_fact_id}")


def test_00_postgresql_0073_0075_round_trip_leaves_target_revision() -> None:
    async def current_revision() -> str:
        engine = create_async_engine(_async_dsn())
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return str(value)
        finally:
            await engine.dispose()

    initial = asyncio.run(current_revision())
    assert initial in {"0073", "0074", "0075"}
    if initial == "0075":
        _run_alembic("downgrade", "0074")
        initial = "0074"
    if initial == "0074":
        _run_alembic("downgrade", "0073")
    _run_alembic("upgrade", "0074")
    _run_alembic("upgrade", "0075")
    _run_alembic("downgrade", "0074")
    _run_alembic("upgrade", "0075")
    assert asyncio.run(current_revision()) == "0075"


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


def test_postgresql_apply_replay_and_current_resolver_keep_historical_bridges() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                command, assertion_id, _source_fingerprint = await _seed_reconciliation_fixture(db)
                command = replace(
                    command,
                    expected_precondition_fingerprint=(
                        await build_fact_reconciliation_precondition_fingerprint(db, command)
                    ),
                )
                result = await apply_fact_reconciliation(db, command)

                assert result.status == "APPLIED"
                assert result.decision is not None
                assert result.target_slots[0].target_logical_fact_id is not None
                assert result.assignments[0].assignment_state == "resolved"

                replay = await apply_fact_reconciliation(db, command)
                assert replay.status == "REUSED"
                assert replay.reused_decision_id == result.decision.id
                assert replay.effective_outcome == "APPLIED"
                assert replay.current_decision_status == "applied"

                current = await resolve_current_logical_fact(
                    db,
                    command.library_id,
                    command.source_logical_fact_ids[0],
                    fact_assertion_id=assertion_id,
                )
                assert current.status == "resolved"
                assert current.current_logical_fact_id == result.target_slots[0].target_logical_fact_id
                assert await db.scalar(
                    text("SELECT logical_fact_id FROM fact_assertions WHERE id = :id"),
                    {"id": assertion_id},
                ) == command.source_logical_fact_ids[0]
                assert await db.scalar(
                    text(
                        "SELECT reconciliation_target_slot_id FROM logical_facts WHERE id = :id"
                    ),
                    {"id": command.source_logical_fact_ids[0]},
                ) is None

                await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_pending_cancellation_supersedes_the_complete_child_chain() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                command, assertion_id, _source_fingerprint = await _seed_reconciliation_fixture(db)
                first_target = command.target_slots[0]
                second_target = FactReconciliationTargetSlotInput(
                    "target-2",
                    "new",
                    FactReconciliationTargetSpec(
                        stable_predicate_identity_id=(
                            first_target.target_spec.stable_predicate_identity_id
                        ),
                        subject_canonical_entity_id=(
                            first_target.target_spec.subject_canonical_entity_id
                        ),
                        object_kind=None,
                        object_canonical_entity_id=None,
                        object_value={"value": "alternate"},
                        identity_qualifiers={},
                        temporal_identity_key=None,
                        identity_policy_version="p3_3_test",
                    ),
                )
                staged_command = replace(
                    command,
                    source_target_topology=(
                        FactReconciliationTopology(
                            command.source_logical_fact_ids[0], ("target", "target-2")
                        ),
                    ),
                    target_slots=(first_target, second_target),
                    assignments=(
                        FactReconciliationAssignmentInput(
                            command.source_logical_fact_ids[0], assertion_id, "pending", None
                        ),
                    ),
                    requested_effect="stage",
                    idempotency_key=f"p3-3-pg-stage-{uuid.uuid4()}",
                    request_id=f"p3-3-pg-stage-{uuid.uuid4()}",
                )
                staged_command = replace(
                    staged_command,
                    expected_precondition_fingerprint=(
                        await build_fact_reconciliation_precondition_fingerprint(
                            db, staged_command
                        )
                    ),
                )
                staged = await apply_fact_reconciliation(db, staged_command)
                assert staged.status == "PENDING"

                cancelled = await cancel_pending_fact_reconciliation(
                    db,
                    library_id=staged_command.library_id,
                    cancellation=FactReconciliationCancellation(
                        command_id=staged.command.id,
                        original_idempotency_key=staged_command.idempotency_key,
                        expected_pending_decision_id=staged.decision.id,
                        reason_code="operator_cancelled",
                        reason_text="Cancel the incomplete split.",
                        evidence_refs=({"test": "p3_3_postgresql_cancel"},),
                        actor_type="test",
                        actor_id="p3_3_postgresql",
                        request_id=f"p3-3-pg-cancel-{uuid.uuid4()}",
                    ),
                )
                assert cancelled.status == "CANCELLED"
                assert staged.decision.lifecycle_status == "superseded"
                assert all(row.resolution_state == "superseded" for row in staged.source_transitions)
                assert all(row.slot_state == "superseded" for row in staged.target_slots)
                assert all(row.edge_state == "superseded" for row in staged.edges)
                assert all(row.assignment_state == "superseded" for row in staged.assignments)

                await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_busy_reconciliation_lock_leaves_no_partial_writer() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_async_dsn(), pool_size=3, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as seed:
                command, _assertion_id, source_fingerprint = await _seed_reconciliation_fixture(seed)
                await seed.commit()

            async with sessions() as precondition_db:
                command = replace(
                    command,
                    expected_precondition_fingerprint=(
                        await build_fact_reconciliation_precondition_fingerprint(
                            precondition_db, command
                        )
                    ),
                )

            async with sessions() as holder, sessions() as contender:
                await holder.begin()
                await contender.begin()
                try:
                    await lock_graph_identity_scopes(
                        holder,
                        command.library_id,
                        (
                            GraphIdentityLockScope(
                                LOGICAL_FACT_LOCK_SCOPE, source_fingerprint
                            ),
                        ),
                        wait=False,
                    )
                    with pytest.raises(FactReconciliationRetryableConflict) as error:
                        await apply_fact_reconciliation(contender, command)
                    assert str(error.value) == "fact_reconciliation_lock_busy"
                    assert await contender.scalar(
                        text(
                            "SELECT count(*) FROM fact_reconciliation_commands "
                            "WHERE library_id = :library_id"
                        ),
                        {"library_id": command.library_id},
                    ) == 0
                finally:
                    await contender.rollback()
                    await holder.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
