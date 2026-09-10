"""Optional P3.1 PostgreSQL integration gates.

The configured DSN must point to a disposable database already migrated to
``0072``.  This module deliberately never falls back to application settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.canonical_entity_evolution import (
    CanonicalEntityEvolutionDecision,
    CanonicalEntityEvolutionSource,
    CanonicalEntityProjectionAssignment,
)
from app.models.entity import Entity
from app.models.entity_resolution_decision import EntityResolutionDecision
from app.services.canonical_entity_evolution import (
    CanonicalEvolutionCancellation,
    CanonicalEvolutionContext,
    CanonicalMergeCommand,
    CanonicalSplitCommand,
    EvolutionProjectionPartition,
    apply_canonical_evolution,
    build_evolution_precondition_fingerprint,
    cancel_pending_canonical_evolution,
    complete_pending_canonical_evolution,
    resolve_current_canonical_identity,
)
from app.services.graph_identity_locks import (
    ENTITY_PROJECTION_LOCK_SCOPE,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)

_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


def _fingerprint(value: uuid.UUID) -> str:
    return hashlib.sha256(value.bytes).hexdigest()


async def _seed_library(db) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    library_id, ontology_id, entity_type_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO sys_libraries (
                id, slug, name, embedding_model, embedding_dim, qdrant_collection
            ) VALUES (
                :id, :slug, 'P3.1 PostgreSQL test', 'test-model', 3, :collection
            )
            """
        ),
        {
            "id": library_id,
            "slug": f"p31-{library_id.hex}",
            "collection": f"p31_{library_id.hex}",
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO ontology_versions (id, library_id, version_key, version_no, status)
            VALUES (:id, :library_id, 'p31', 1, 'active')
            """
        ),
        {"id": ontology_id, "library_id": library_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO entity_types (id, library_id, ontology_version_id, key, label, status)
            VALUES (:id, :library_id, :ontology_id, 'p31_entity', 'P3.1 entity', 'active')
            """
        ),
        {"id": entity_type_id, "library_id": library_id, "ontology_id": ontology_id},
    )
    return library_id, ontology_id, entity_type_id


async def _seed_canonical(db, library_id: uuid.UUID, name: str) -> uuid.UUID:
    canonical_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO canonical_entities (id, library_id, canonical_name, normalized_name, status)
            VALUES (:id, :library_id, :name, :normalized_name, 'active')
            """
        ),
        {
            "id": canonical_id,
            "library_id": library_id,
            "name": name,
            "normalized_name": name.lower(),
        },
    )
    return canonical_id


async def _seed_projection(
    db,
    *,
    library_id: uuid.UUID,
    ontology_id: uuid.UUID,
    entity_type_id: uuid.UUID,
    canonical_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    entity_id, resolution_id = uuid.uuid4(), uuid.uuid4()
    subject = _fingerprint(entity_id)
    await db.execute(
        text(
            """
            INSERT INTO entities (
                id, library_id, ontology_version_id, entity_type_id, canonical_entity_id,
                canonical_name, normalized_name, status, source_type
            ) VALUES (
                :id, :library_id, :ontology_id, :entity_type_id, :canonical_id,
                'projection', :normalized_name, 'active', 'manual'
            )
            """
        ),
        {
            "id": entity_id,
            "library_id": library_id,
            "ontology_id": ontology_id,
            "entity_type_id": entity_type_id,
            "canonical_id": canonical_id,
            "normalized_name": f"projection-{entity_id.hex}",
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO entity_resolution_decisions (
                id, library_id, subject_fingerprint, decision_fingerprint, entity_id,
                canonical_entity_id, observed_name, observed_normalized_name,
                candidate_snapshot, evidence_refs, decision_kind, lifecycle_status,
                method, confidence, resolver_version
            ) VALUES (
                :id, :library_id, :subject, :decision_fingerprint, :entity_id,
                :canonical_id, 'projection', 'projection', '[]'::jsonb, '[]'::jsonb,
                'link_existing', 'active', 'p31_test', 1, 'entity_resolution_v1'
            )
            """
        ),
        {
            "id": resolution_id,
            "library_id": library_id,
            "subject": subject,
            "decision_fingerprint": _fingerprint(resolution_id),
            "entity_id": entity_id,
            "canonical_id": canonical_id,
        },
    )
    return entity_id, resolution_id


async def _seed_split_fixture(
    db,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    library_id, ontology_id, entity_type_id = await _seed_library(db)
    source_id = await _seed_canonical(db, library_id, "source")
    left_id = await _seed_canonical(db, library_id, "left")
    right_id = await _seed_canonical(db, library_id, "right")
    first_entity_id, _first_resolution_id = await _seed_projection(
        db,
        library_id=library_id,
        ontology_id=ontology_id,
        entity_type_id=entity_type_id,
        canonical_id=source_id,
    )
    second_entity_id, _second_resolution_id = await _seed_projection(
        db,
        library_id=library_id,
        ontology_id=ontology_id,
        entity_type_id=entity_type_id,
        canonical_id=source_id,
    )
    return library_id, source_id, left_id, right_id, first_entity_id, second_entity_id


def _split_command(
    *,
    library_id: uuid.UUID,
    source_id: uuid.UUID,
    targets: tuple[uuid.UUID, uuid.UUID],
    partition: tuple[EvolutionProjectionPartition, EvolutionProjectionPartition],
    idempotency_key: str,
) -> CanonicalSplitCommand:
    return CanonicalSplitCommand(
        library_id=library_id,
        source_canonical_entity_id=source_id,
        targets=targets,
        partition=partition,
        idempotency_key=idempotency_key,
        reason_code="identity_overload",
        reason_text="PostgreSQL split lineage check",
        method="p31_pg_test",
        evidence_refs=(),
        actor_type="service",
        actor_id=uuid.uuid4(),
        request_id=f"p31:{idempotency_key}",
    )


def test_scope_40_serializes_two_postgresql_connections():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        library_id, entity_id = uuid.uuid4(), uuid.uuid4()
        first_locked = asyncio.Event()
        release_first = asyncio.Event()
        second_acquired = asyncio.Event()

        async def first() -> None:
            async with sessions() as db, db.begin():
                await lock_graph_identity_scopes(
                    db,
                    library_id,
                    (GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, entity_id),),
                )
                first_locked.set()
                await release_first.wait()

        async def second() -> None:
            async with sessions() as db, db.begin():
                await lock_graph_identity_scopes(
                    db,
                    library_id,
                    (GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, entity_id),),
                )
                second_acquired.set()

        try:
            first_task = asyncio.create_task(first())
            await asyncio.wait_for(first_locked.wait(), timeout=2)
            second_task = asyncio.create_task(second())
            await asyncio.sleep(0.1)
            assert not second_acquired.is_set()
            release_first.set()
            await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
            assert second_acquired.is_set()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_0072_installs_deferred_evolution_integrity_triggers():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN)
        try:
            async with engine.connect() as connection:
                triggers = set(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT c.relname, t.tgdeferrable, t.tginitdeferred, p.proname
                                FROM pg_trigger t
                                JOIN pg_class c ON c.oid = t.tgrelid
                                JOIN pg_proc p ON p.oid = t.tgfoid
                                WHERE p.proname = 'canonical_entity_evolution_integrity'
                                """
                            )
                        )
                    ).all()
                )
            assert triggers == {
                ('canonical_entity_evolution_commands', True, True, 'canonical_entity_evolution_integrity'),
                ('canonical_entity_evolution_decisions', True, True, 'canonical_entity_evolution_integrity'),
                ('canonical_entity_evolution_sources', True, True, 'canonical_entity_evolution_integrity'),
                ('canonical_entity_evolution_successors', True, True, 'canonical_entity_evolution_integrity'),
                ('canonical_entity_projection_assignments', True, True, 'canonical_entity_evolution_integrity'),
                ('entities', True, True, 'canonical_entity_evolution_integrity'),
                ('entity_resolution_decisions', True, True, 'canonical_entity_evolution_integrity'),
            }
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_merge_pairs_projection_and_rejects_bare_pointer_rewrite():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db, db.begin():
                library_id, ontology_id, entity_type_id = await _seed_library(db)
                loser_id = await _seed_canonical(db, library_id, "loser")
                survivor_id = await _seed_canonical(db, library_id, "survivor")
                entity_id, previous_resolution_id = await _seed_projection(
                    db,
                    library_id=library_id,
                    ontology_id=ontology_id,
                    entity_type_id=entity_type_id,
                    canonical_id=loser_id,
                )
                command = CanonicalMergeCommand(
                    library_id=library_id,
                    source_canonical_entity_ids=(loser_id,),
                    survivor_canonical_entity_id=survivor_id,
                    idempotency_key="pg-merge",
                    reason_code="duplicate_identity",
                    reason_text="PostgreSQL projection lineage check",
                    method="p31_pg_test",
                    evidence_refs=(),
                    actor_type="service",
                    actor_id=uuid.uuid4(),
                    request_id="p31:pg-merge",
                )
                command = command.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, command)
                )
                result = await apply_canonical_evolution(db, command)
                assert result.status == "APPLIED"
                assert result.command is not None
                command_id = result.command.id

            async with sessions() as db:
                entity = await db.get(Entity, entity_id)
                decisions = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionDecision).where(
                        CanonicalEntityEvolutionDecision.command_id == command_id
                    ))).all()
                )
                assignments = tuple(
                    (await db.scalars(select(CanonicalEntityProjectionAssignment).where(
                        CanonicalEntityProjectionAssignment.command_id == command_id
                    ))).all()
                )
                resolutions = tuple(
                    (await db.scalars(select(EntityResolutionDecision).where(
                        EntityResolutionDecision.entity_id == entity_id
                    ))).all()
                )
                current = await resolve_current_canonical_identity(
                    db,
                    library_id,
                    loser_id,
                    CanonicalEvolutionContext(entity_id=entity_id),
                )

            assert entity is not None and entity.canonical_entity_id == survivor_id
            assert len(decisions) == 1 and decisions[0].lifecycle_status == "applied"
            assert len(assignments) == 1
            assignment = assignments[0]
            assert assignment.target_canonical_entity_id == survivor_id
            previous = next(row for row in resolutions if row.id == previous_resolution_id)
            replacement = next(row for row in resolutions if row.id != previous_resolution_id)
            assert previous.lifecycle_status == "superseded"
            assert replacement.lifecycle_status == "active"
            assert replacement.evolution_assignment_id == assignment.id
            assert current.status == "resolved"
            assert current.current_canonical_entity_id == survivor_id

            with pytest.raises(DBAPIError, match="Entity pointer is not paired"):
                async with sessions() as db, db.begin():
                    await db.execute(
                        text(
                            "UPDATE entities SET canonical_entity_id = :canonical_id "
                            "WHERE id = :entity_id"
                        ),
                        {"canonical_id": loser_id, "entity_id": entity_id},
                    )

            async with sessions() as db:
                entity = await db.get(Entity, entity_id)
            assert entity is not None and entity.canonical_entity_id == survivor_id
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_rolls_back_direct_root_without_a_decision():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        command_id = uuid.uuid4()
        try:
            async with sessions() as db, db.begin():
                library_id, _ontology_id, _entity_type_id = await _seed_library(db)

            with pytest.raises(DBAPIError, match="canonical entity evolution chain is invalid"):
                async with sessions() as db, db.begin():
                    await db.execute(
                        text(
                            """
                            INSERT INTO canonical_entity_evolution_commands (
                                id, library_id, idempotency_key, command_identity_fingerprint,
                                operation_kind, source_identity_snapshot, command_scope_snapshot,
                                contract_version
                            ) VALUES (
                                :id, :library_id, 'root-without-decision', :fingerprint,
                                'merge', '{}'::jsonb, '{}'::jsonb, 'canonical_entity_evolution/v1'
                            )
                            """
                        ),
                        {
                            "id": command_id,
                            "library_id": library_id,
                            "fingerprint": "0" * 64,
                        },
                    )

            async with sessions() as db:
                count = await db.scalar(
                    text(
                        "SELECT count(*) FROM canonical_entity_evolution_commands WHERE id = :id"
                    ),
                    {"id": command_id},
                )
            assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_pending_split_correction_is_atomic_and_keeps_lineage():
    class _Rollback(Exception):
        pass

    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db, db.begin():
                (
                    library_id,
                    source_id,
                    left_id,
                    right_id,
                    first_entity_id,
                    second_entity_id,
                ) = await _seed_split_fixture(db)
                first_command = _split_command(
                    library_id=library_id,
                    source_id=source_id,
                    targets=(left_id, right_id),
                    partition=(
                        EvolutionProjectionPartition(first_entity_id, left_id, {"slot": "left"}),
                        EvolutionProjectionPartition(second_entity_id, None, {"slot": "pending"}),
                    ),
                    idempotency_key="pg-correction",
                ).with_expected_precondition(None)
                first_command = first_command.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, first_command)
                )
                first = await apply_canonical_evolution(db, first_command)
                assert first.status == "PENDING"
                assert first.decision is not None
                first_decision_id = first.decision.id
                assert first.command is not None
                command_id = first.command.id

            try:
                async with sessions() as db, db.begin():
                    correction = _split_command(
                        library_id=library_id,
                        source_id=source_id,
                        targets=(left_id, right_id),
                        partition=(
                            EvolutionProjectionPartition(first_entity_id, left_id, {"slot": "left"}),
                            EvolutionProjectionPartition(second_entity_id, right_id, {"slot": "right"}),
                        ),
                        idempotency_key="pg-correction",
                    )
                    correction = correction.with_expected_precondition(
                        await build_evolution_precondition_fingerprint(db, correction)
                    )
                    correction = replace(
                        correction, expected_predecessor_decision_id=first_decision_id
                    )
                    corrected = await complete_pending_canonical_evolution(db, correction)
                    assert corrected.status == "APPLIED"
                    raise _Rollback()
            except _Rollback:
                pass

            async with sessions() as db:
                decisions = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionDecision).where(
                        CanonicalEntityEvolutionDecision.command_id == command_id
                    ))).all()
                )
                sources = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionSource).where(
                        CanonicalEntityEvolutionSource.command_id == command_id
                    ))).all()
                )
                assignments = tuple(
                    (await db.scalars(select(CanonicalEntityProjectionAssignment).where(
                        CanonicalEntityProjectionAssignment.command_id == command_id
                    ))).all()
                )
            assert [row.lifecycle_status for row in decisions] == ["pending"]
            assert [row.resolution_state for row in sources] == ["pending"]
            assert {row.assignment_state for row in assignments} == {"pending"}

            async with sessions() as db, db.begin():
                correction = _split_command(
                    library_id=library_id,
                    source_id=source_id,
                    targets=(left_id, right_id),
                    partition=(
                        EvolutionProjectionPartition(first_entity_id, left_id, {"slot": "left"}),
                        EvolutionProjectionPartition(second_entity_id, right_id, {"slot": "right"}),
                    ),
                    idempotency_key="pg-correction",
                )
                correction = correction.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, correction)
                )
                correction = replace(
                    correction, expected_predecessor_decision_id=first_decision_id
                )
                corrected = await complete_pending_canonical_evolution(db, correction)
                assert corrected.status == "APPLIED"
                assert corrected.decision is not None
                second_decision_id = corrected.decision.id

            async with sessions() as db:
                decisions = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionDecision).where(
                        CanonicalEntityEvolutionDecision.command_id == command_id
                    ))).all()
                )
                sources = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionSource).where(
                        CanonicalEntityEvolutionSource.command_id == command_id
                    ))).all()
                )
                assignments = tuple(
                    (await db.scalars(select(CanonicalEntityProjectionAssignment).where(
                        CanonicalEntityProjectionAssignment.command_id == command_id
                    ))).all()
                )
            first_decision = next(row for row in decisions if row.id == first_decision_id)
            second_decision = next(row for row in decisions if row.id == second_decision_id)
            first_source = next(row for row in sources if row.evolution_decision_id == first_decision_id)
            second_source = next(row for row in sources if row.evolution_decision_id == second_decision_id)
            assert first_decision.lifecycle_status == "superseded"
            assert second_decision.lifecycle_status == "applied"
            assert second_decision.supersedes_decision_id == first_decision_id
            assert first_source.resolution_state == "superseded"
            assert second_source.resolution_state == "applied"
            assert second_source.supersedes_source_transition_id == first_source.id
            assert len(assignments) == 4
            assert {row.assignment_state for row in assignments if row.evolution_decision_id == first_decision_id} == {"superseded"}
            assert {row.assignment_state for row in assignments if row.evolution_decision_id == second_decision_id} == {"resolved"}
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_pending_cancellation_releases_slot_for_new_identity():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db, db.begin():
                (
                    library_id,
                    source_id,
                    left_id,
                    right_id,
                    first_entity_id,
                    second_entity_id,
                ) = await _seed_split_fixture(db)
                third_id = await _seed_canonical(db, library_id, "third")
                pending_command = _split_command(
                    library_id=library_id,
                    source_id=source_id,
                    targets=(left_id, right_id),
                    partition=(
                        EvolutionProjectionPartition(first_entity_id, left_id, {"slot": "left"}),
                        EvolutionProjectionPartition(second_entity_id, None, {"slot": "pending"}),
                    ),
                    idempotency_key="pg-cancelled",
                ).with_expected_precondition(None)
                pending_command = pending_command.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, pending_command)
                )
                pending = await apply_canonical_evolution(db, pending_command)
                assert pending.status == "PENDING"
                assert pending.command is not None and pending.decision is not None
                pending_command_id = pending.command.id
                pending_decision_id = pending.decision.id

            async with sessions() as db, db.begin():
                cancelled = await cancel_pending_canonical_evolution(
                    db,
                    library_id,
                    CanonicalEvolutionCancellation(
                        command_id=pending_command_id,
                        original_idempotency_key="pg-cancelled",
                        expected_pending_decision_id=pending_decision_id,
                        reason_code="incorrect_pending_intent",
                        reason_text="PostgreSQL cancellation check",
                        evidence_refs=(),
                        actor_type="service",
                        actor_id=uuid.uuid4(),
                        request_id="p31:pg-cancelled",
                    ),
                )
                assert cancelled.status == "CANCELLED"

            async with sessions() as db, db.begin():
                takeover = _split_command(
                    library_id=library_id,
                    source_id=source_id,
                    targets=(left_id, third_id),
                    partition=(
                        EvolutionProjectionPartition(first_entity_id, left_id, {"slot": "left"}),
                        EvolutionProjectionPartition(second_entity_id, third_id, {"slot": "third"}),
                    ),
                    idempotency_key="pg-takeover",
                )
                takeover = takeover.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, takeover)
                )
                applied = await apply_canonical_evolution(db, takeover)
                assert applied.status == "APPLIED"
                assert applied.command is not None
                takeover_command_id = applied.command.id

            async with sessions() as db:
                pending_decisions = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionDecision).where(
                        CanonicalEntityEvolutionDecision.command_id == pending_command_id
                    ))).all()
                )
                pending_sources = tuple(
                    (await db.scalars(select(CanonicalEntityEvolutionSource).where(
                        CanonicalEntityEvolutionSource.command_id == pending_command_id
                    ))).all()
                )
                takeover_decision = await db.scalar(
                    select(CanonicalEntityEvolutionDecision).where(
                        CanonicalEntityEvolutionDecision.command_id == takeover_command_id
                    )
                )
            assert {row.lifecycle_status for row in pending_decisions} == {"superseded", "cancelled"}
            assert {row.resolution_state for row in pending_sources} == {"superseded"}
            assert takeover_decision is not None
            assert takeover_decision.supersedes_decision_id is None
            assert takeover_decision.lifecycle_status == "applied"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_same_merge_replay_is_serialized_to_one_root_and_decision():
    async def exercise() -> None:
        engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first_locked = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        try:
            async with sessions() as db, db.begin():
                library_id, _ontology_id, _entity_type_id = await _seed_library(db)
                loser_id = await _seed_canonical(db, library_id, "loser")
                survivor_id = await _seed_canonical(db, library_id, "survivor")
                command = CanonicalMergeCommand(
                    library_id=library_id,
                    source_canonical_entity_ids=(loser_id,),
                    survivor_canonical_entity_id=survivor_id,
                    idempotency_key="pg-race",
                    reason_code="duplicate_identity",
                    reason_text="PostgreSQL replay race",
                    method="p31_pg_test",
                    evidence_refs=(),
                    actor_type="service",
                    actor_id=uuid.uuid4(),
                    request_id="p31:pg-race",
                )
                command = command.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, command)
                )

            async def first() -> str:
                async with sessions() as db, db.begin():
                    result = await apply_canonical_evolution(db, command)
                    first_locked.set()
                    await release_first.wait()
                    return result.status

            async def second() -> str:
                await asyncio.wait_for(first_locked.wait(), timeout=2)
                async with sessions() as db, db.begin():
                    result = await apply_canonical_evolution(db, command)
                    second_finished.set()
                    return result.status

            first_task = asyncio.create_task(first())
            await asyncio.wait_for(first_locked.wait(), timeout=2)
            second_task = asyncio.create_task(second())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_finished.wait(), timeout=0.1)
            release_first.set()
            statuses = await asyncio.wait_for(
                asyncio.gather(first_task, second_task), timeout=2
            )

            async with sessions() as db:
                command_count = await db.scalar(
                    text(
                        "SELECT count(*) FROM canonical_entity_evolution_commands "
                        "WHERE library_id = :library_id"
                    ),
                    {"library_id": library_id},
                )
                decision_count = await db.scalar(
                    text(
                        "SELECT count(*) FROM canonical_entity_evolution_decisions "
                        "WHERE library_id = :library_id"
                    ),
                    {"library_id": library_id},
                )
            assert sorted(statuses) == ["APPLIED", "REUSED"]
            assert command_count == 1
            assert decision_count == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())
