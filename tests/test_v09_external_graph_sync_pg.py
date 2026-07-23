from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.external_graph_sync import (
    GraphExternalFactMapping,
    GraphExternalSyncConflict,
    GraphExternalSyncOperation,
)
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.relation_type import RelationType
from app.models.sync_source import SyncSource
from app.models.user import User
from app.schemas.external_graph_sync import (
    ExternalGraphConflictDecision,
    ExternalGraphSyncBatchRequest,
    GraphSyncPolicyWrite,
)
from app.services.external_graph_sync import (
    ExternalGraphSyncError,
    decide_conflict,
    put_source_policy,
    sync_batch,
)
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


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _entity(external_id: str, name: str, ontology_id: uuid.UUID) -> dict:
    return {
        "action": "upsert",
        "fact_kind": "entity",
        "external_type": "employee",
        "external_id": external_id,
        "ontology_version_id": str(ontology_id),
        "entity_type_key": "person",
        "canonical_name": name,
        "normalized_name": name.lower(),
        "properties": {"department": "Engineering"},
    }


def _relation(ontology_id: uuid.UUID) -> dict:
    return {
        "action": "upsert",
        "fact_kind": "relation",
        "external_type": "reports_to",
        "external_id": "R-1",
        "ontology_version_id": str(ontology_id),
        "relation_type_key": "reports_to",
        "source_entity": {"external_type": "employee", "external_id": "E-1"},
        "target_entity": {"external_type": "employee", "external_id": "E-2"},
        "properties": {"since": 2026},
    }


async def _seed(Session):
    actor_id = uuid.uuid4()
    library_id = uuid.uuid4()
    source_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    async with Session() as db:
        db.add(
            User(
                id=actor_id,
                email=f"graph-sync-{actor_id}@example.com",
                hashed_password="hash",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        library = Library(
            id=library_id,
            organization_id=DEFAULT_ORGANIZATION_ID,
            slug=f"graph-sync-{library_id.hex[:8]}",
            name="Graph Sync",
            embedding_model="bge-m3",
            embedding_dim=1024,
            qdrant_collection=f"graph_sync_{library_id.hex[:8]}",
        )
        db.add(library)
        await db.flush()
        source = SyncSource(
            id=source_id,
            library_id=library_id,
            source_key="hr",
            display_name="HR",
            source_type="hr",
            status="active",
        )
        db.add(source)
        db.add(
            SyncSource(
                library_id=library_id,
                source_key="crm",
                display_name="CRM",
                source_type="crm",
                status="active",
            )
        )
        db.add(
            OntologyVersion(
                id=ontology_id,
                library_id=library_id,
                version_key="default",
                version_no=1,
                status="active",
            )
        )
        await db.flush()
        db.add(
            EntityType(
                library_id=library_id,
                ontology_version_id=ontology_id,
                key="person",
                label="Person",
                properties_schema={},
                status="active",
            )
        )
        db.add(
            RelationType(
                library_id=library_id,
                ontology_version_id=ontology_id,
                key="reports_to",
                label="Reports to",
                direction="directed",
                requires_evidence=False,
                default_review_policy="pending_review",
                properties_schema={},
                status="active",
            )
        )
        await db.flush()
        await put_source_policy(
            db,
            library,
            "hr",
            GraphSyncPolicyWrite(authority_rank=20),
        )
        await put_source_policy(
            db,
            library,
            "crm",
            GraphSyncPolicyWrite(authority_rank=20),
        )
        await db.commit()
    return actor_id, library_id, ontology_id


async def _exercise(database_url: URL) -> None:
    engine = create_async_engine(database_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        actor_id, library_id, ontology_id = await _seed(Session)
        initial = ExternalGraphSyncBatchRequest(
            idempotency_key="initial",
            snapshot_id="snapshot-1",
            items=[
                _relation(ontology_id),
                _entity("E-2", "Bob", ontology_id),
                _entity("E-1", "Alice", ontology_id),
            ],
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            first = await sync_batch(db, library, "hr", initial, actor_id=actor_id)
            await db.commit()
        assert first.status == "applied"
        assert first.created_count == 3

        async with Session() as db:
            library = await db.get(Library, library_id)
            replay = await sync_batch(db, library, "hr", initial, actor_id=actor_id)
            await db.commit()
        assert replay.replayed is True
        assert replay.model_copy(update={"replayed": False}) == first

        async with Session() as db:
            counts = [
                (
                    await db.execute(select(func.count(model.id)))
                ).scalar_one()
                for model in (
                    GraphExternalSyncOperation,
                    GraphExternalFactMapping,
                    Entity,
                    KnowledgeRelation,
                )
            ]
        assert counts == [1, 3, 2, 1]

        same_payload = initial.model_copy(
            update={"idempotency_key": "same-payload"}
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            unchanged = await sync_batch(
                db, library, "hr", same_payload, actor_id=actor_id
            )
            await db.commit()
        assert unchanged.unchanged_count == 3

        event_body = ExternalGraphSyncBatchRequest(
            idempotency_key="event-key-1",
            source_event_id="source-event-1",
            items=[_entity("E-1", "Alice", ontology_id)],
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            event_first = await sync_batch(
                db, library, "hr", event_body, actor_id=actor_id
            )
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, library_id)
            event_replay = await sync_batch(
                db,
                library,
                "hr",
                event_body.model_copy(update={"idempotency_key": "event-key-2"}),
                actor_id=actor_id,
            )
            await db.commit()
        assert event_replay.replayed is True
        assert event_replay.operation_id == event_first.operation_id
        conflicting_event = event_body.model_copy(
            update={
                "idempotency_key": "event-key-3",
                "items": [
                    event_body.items[0].model_copy(
                        update={"properties": {"department": "Finance"}}
                    )
                ],
            }
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            with pytest.raises(
                ExternalGraphSyncError,
                match="graph_sync_idempotency_conflict",
            ):
                await sync_batch(
                    db,
                    library,
                    "hr",
                    conflicting_event,
                    actor_id=actor_id,
                )
            await db.rollback()

        equal_authority = ExternalGraphSyncBatchRequest(
            idempotency_key="crm-equal-authority",
            items=[
                _entity("CRM-1", "Alice", ontology_id)
                | {"properties": {"department": "Sales"}}
            ],
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            conflicted = await sync_batch(
                db, library, "crm", equal_authority, actor_id=actor_id
            )
            await db.commit()
        assert (conflicted.status, conflicted.conflict_count) == ("conflicted", 1)
        async with Session() as db:
            alice = (
                await db.execute(
                    select(Entity).where(Entity.normalized_name == "alice")
                )
            ).scalars().one()
        assert alice.properties == {"department": "Engineering"}

        delete = ExternalGraphSyncBatchRequest(
            idempotency_key="delete-active",
            items=[
                {
                    "action": "delete",
                    "fact_kind": "entity",
                    "external_type": "employee",
                    "external_id": "E-1",
                }
            ],
        )
        async with Session() as db:
            mapping = (
                await db.execute(
                    select(GraphExternalFactMapping).where(
                        GraphExternalFactMapping.external_id == "E-1"
                    )
                )
            ).scalars().one()
            entity = await db.get(Entity, mapping.entity_id)
            entity.status = "active"
            await db.commit()
        async with Session() as db:
            library = await db.get(Library, library_id)
            blocked = await sync_batch(db, library, "hr", delete, actor_id=actor_id)
            await db.commit()
        assert (blocked.status, blocked.conflict_count) == ("conflicted", 1)

        snapshot = ExternalGraphSyncBatchRequest(
            idempotency_key="snapshot-2",
            snapshot_id="snapshot-2",
            complete_snapshot=True,
            items=[_entity("E-1", "Alice", ontology_id)],
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            stale = await sync_batch(db, library, "hr", snapshot, actor_id=actor_id)
            await db.commit()
        assert stale.stale_count == 2
        async with Session() as db:
            lifecycle_counts = dict(
                (
                    await db.execute(
                        select(
                            GraphExternalFactMapping.lifecycle,
                            func.count(GraphExternalFactMapping.id),
                        ).group_by(GraphExternalFactMapping.lifecycle)
                    )
                ).all()
            )
            open_conflicts = (
                await db.execute(
                    select(func.count(GraphExternalSyncConflict.id)).where(
                        GraphExternalSyncConflict.status == "open"
                    )
                )
            ).scalar_one()
        assert lifecycle_counts == {"active": 1, "stale": 3}
        assert open_conflicts == 4

        concurrent_body = ExternalGraphSyncBatchRequest(
            idempotency_key="concurrent-create",
            items=[_entity("E-3", "Carol", ontology_id)],
        )

        async def run_concurrent():
            async with Session() as db:
                library = await db.get(Library, library_id)
                result = await sync_batch(
                    db,
                    library,
                    "hr",
                    concurrent_body,
                    actor_id=actor_id,
                )
                await db.commit()
                return result

        concurrent = await asyncio.gather(run_concurrent(), run_concurrent())
        assert sorted(result.replayed for result in concurrent) == [False, True]
        assert len({result.operation_id for result in concurrent}) == 1
        async with Session() as db:
            carol_mappings = (
                await db.execute(
                    select(func.count(GraphExternalFactMapping.id)).where(
                        GraphExternalFactMapping.external_id == "E-3"
                    )
                )
            ).scalar_one()
        assert carol_mappings == 1

        async with Session() as db:
            library = await db.get(Library, library_id)
            await put_source_policy(
                db,
                library,
                "crm",
                GraphSyncPolicyWrite(authority_rank=10),
            )
            await db.commit()

        async def sync_one(
            source_key: str,
            key: str,
            external_id: str,
            name: str,
            department: str,
        ):
            body = ExternalGraphSyncBatchRequest(
                idempotency_key=key,
                items=[
                    _entity(external_id, name, ontology_id)
                    | {"properties": {"department": department}}
                ],
            )
            async with Session() as db:
                library = await db.get(Library, library_id)
                result = await sync_batch(
                    db, library, source_key, body, actor_id=actor_id
                )
                await db.commit()
                return result

        await sync_one("hr", "dana-weak-first", "D-HR", "Dana", "Engineering")
        dana_strong = await sync_one(
            "crm", "dana-strong-second", "D-CRM", "Dana", "Sales"
        )
        await sync_one("crm", "erin-strong-first", "E-CRM", "Erin", "Sales")
        erin_weak = await sync_one(
            "hr", "erin-weak-second", "E-HR", "Erin", "Engineering"
        )
        assert dana_strong.status == "applied"
        assert erin_weak.status == "conflicted"
        async with Session() as db:
            final_departments = dict(
                (
                    await db.execute(
                        select(Entity.normalized_name, Entity.properties).where(
                            Entity.normalized_name.in_(("dana", "erin"))
                        )
                    )
                ).all()
            )
        assert final_departments == {
            "dana": {"department": "Sales"},
            "erin": {"department": "Sales"},
        }

        async with Session() as db:
            library = await db.get(Library, library_id)
            conflict = (
                await db.execute(
                    select(GraphExternalSyncConflict)
                    .where(GraphExternalSyncConflict.status == "open")
                    .order_by(GraphExternalSyncConflict.created_at)
                    .limit(1)
                )
            ).scalars().one()
            decided = await decide_conflict(
                db,
                library,
                conflict.id,
                ExternalGraphConflictDecision(decision="resolved"),
            )
            replayed_decision = await decide_conflict(
                db,
                library,
                conflict.id,
                ExternalGraphConflictDecision(decision="resolved"),
            )
            assert decided.id == replayed_decision.id
            with pytest.raises(
                ExternalGraphSyncError,
                match="graph_sync_conflict_already_decided",
            ):
                await decide_conflict(
                    db,
                    library,
                    conflict.id,
                    ExternalGraphConflictDecision(decision="dismissed"),
                )
            await db.commit()
    finally:
        await engine.dispose()


def test_external_graph_sync_postgres_idempotency_conflict_delete_and_rollback(
    monkeypatch,
):
    name = f"vkt_v09_graph_sync_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        alembic_command.upgrade(Config("alembic.ini"), "0041")
        asyncio.run(_exercise(_database_url(name)))
        alembic_command.downgrade(Config("alembic.ini"), "0040")
        assert asyncio.run(
            execute_sql(
                _database_url(name),
                "SELECT to_regclass('public.graph_sync_source_policies'), "
                "to_regclass('public.graph_external_sync_operations'), "
                "to_regclass('public.graph_external_fact_mappings'), "
                "to_regclass('public.graph_external_sync_conflicts')",
            )
        ) == [(None, None, None, None)]
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
