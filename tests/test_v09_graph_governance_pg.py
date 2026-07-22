from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, settings
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.user import User
from app.services import graph_governance_actions as actions
from app.services import graph_governance_publication as governance
from app.services.graph_governance_actions import entity_governance_state_hash
from app.services.graph_governance_contracts import (
    PlanGraphGovernancePublicationCommand,
    StageEntityStatusCommand,
)
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
)
from app.services.graph_publication_planner import plan_graph_publication
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


def _runtime_config() -> Settings:
    return Settings(
        _env_file=None,
        graph_publication_enabled=True,
        graph_governance_enabled=True,
        organization_authorization_enabled=True,
    )


async def _seed(Session):
    actor_id = uuid.uuid4()
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    entity_type_id = uuid.uuid4()
    first_entity_id = uuid.uuid4()
    second_entity_id = uuid.uuid4()
    async with Session() as db:
        db.add(
            User(
                id=actor_id,
                email=f"governance-{actor_id}@example.com",
                hashed_password="hash",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        db.add(
            Library(
                id=library_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                slug=f"governance-{library_id.hex[:8]}",
                name="Governance",
                embedding_model="bge-m3",
                embedding_dim=1024,
                qdrant_collection=f"governance_{library_id.hex[:8]}",
            )
        )
        await db.flush()
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
                id=entity_type_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                key="company",
                label="Company",
                properties_schema={},
                status="active",
            )
        )
        await db.flush()
        db.add_all(
            [
                Entity(
                    id=first_entity_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    entity_type_id=entity_type_id,
                    canonical_name="Acme",
                    normalized_name="acme",
                    properties={},
                    status="active",
                    source_type="manual",
                ),
                Entity(
                    id=second_entity_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    entity_type_id=entity_type_id,
                    canonical_name="Beta",
                    normalized_name="beta",
                    properties={},
                    status="active",
                    source_type="manual",
                ),
            ]
        )
        await db.commit()
    return actor_id, library_id, ontology_id, first_entity_id


async def _initial_publication(Session, ids, config: Settings) -> uuid.UUID:
    _actor_id, library_id, ontology_id, _entity_id = ids
    async with Session() as db:
        library = await db.get(Library, library_id)
        result = await plan_graph_publication(
            db,
            library,
            ontology_version_id=ontology_id,
            idempotency_key="initial-publication",
            config=config,
        )
        publication_id = result.publication.id
        await db.commit()
    async with Session() as db:
        await activate_graph_publication(db, publication_id, config=config)
    return publication_id


async def _concurrent_disable(Session, ids):
    actor_id, library_id, _ontology_id, entity_id = ids
    async with Session() as db:
        entity = await db.get(Entity, entity_id)
        expected = entity_governance_state_hash(entity)
    command = StageEntityStatusCommand(
        library_id,
        entity_id,
        actor_id,
        "disable-entity",
        expected,
        "disable",
        "duplicate_fact",
    )

    async def run_one():
        async with Session() as db:
            result = await actions.stage_entity_status(db, command)
            await db.commit()
            return result.created, result.action.id

    return await asyncio.gather(run_one(), run_one())


async def _plan_governance(Session, ids, action_id, parent_id, config):
    actor_id, library_id, ontology_id, _entity_id = ids
    async with Session() as db:
        result, action_set_hash = await governance.plan_graph_governance_publication(
            db,
            PlanGraphGovernancePublicationCommand(
                library_id,
                ontology_id,
                actor_id,
                (action_id,),
                parent_id,
                f"governance-plan-{action_id}",
            ),
            config=config,
        )
        publication_id = result.publication.id
        await db.commit()
    return publication_id, action_set_hash, result.reused


async def _exercise_graph_governance_runtime(database_url: URL) -> None:
    engine = create_async_engine(database_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        ids = await _seed(Session)
        config = _runtime_config()
        initial_id = await _initial_publication(Session, ids, config)

        with patch.object(actions, "_require_access", new=AsyncMock()):
            concurrent = await _concurrent_disable(Session, ids)
        assert sorted(created for created, _action_id in concurrent) == [False, True]
        assert len({action_id for _created, action_id in concurrent}) == 1
        action_id = concurrent[0][1]

        async with Session() as db:
            entity = await db.get(Entity, ids[3])
            action = await db.get(GraphGovernanceAction, action_id)
            current = (
                await db.execute(
                    select(GraphPublication).where(
                        GraphPublication.library_id == ids[1],
                        GraphPublication.ontology_version_id == ids[2],
                        GraphPublication.status == "active",
                    )
                )
            ).scalars().one()
            count = (
                await db.execute(
                    select(func.count(GraphGovernanceAction.id)).where(
                        GraphGovernanceAction.library_id == ids[1],
                        GraphGovernanceAction.idempotency_key == "disable-entity",
                    )
                )
            ).scalar_one()
        assert (entity.status, action.status, current.id, count) == (
            "active",
            "approved",
            initial_id,
            1,
        )
        with patch.object(governance, "_require_management", new=AsyncMock()):
            governed_id, action_set_hash, reused = await _plan_governance(
                Session, ids, action_id, initial_id, config
            )
            replayed_id, replayed_hash, replayed = await _plan_governance(
                Session, ids, action_id, initial_id, config
            )
        assert not reused and replayed
        assert (replayed_id, replayed_hash) == (governed_id, action_set_hash)
        assert len(action_set_hash) == 64
        async with Session() as db:
            await activate_graph_publication(db, governed_id, config=config)
        async with Session() as db:
            entity = await db.get(Entity, ids[3])
            action = await db.get(GraphGovernanceAction, action_id)
            publication = await db.get(GraphPublication, governed_id)
        assert (
            entity.status,
            action.status,
            action.applied_publication_id,
            publication.status,
        ) == (
            "disabled",
            "applied",
            governed_id,
            "active",
        )

        async with Session() as db:
            entity = await db.get(Entity, ids[3])
            expected = entity_governance_state_hash(entity)
        command = StageEntityStatusCommand(
            ids[1],
            ids[3],
            ids[0],
            "restore-entity",
            expected,
            "restore",
        )
        with patch.object(actions, "_require_access", new=AsyncMock()):
            async with Session() as db:
                result = await actions.stage_entity_status(db, command)
                await db.commit()
                restore_action_id = result.action.id
        with patch.object(governance, "_require_management", new=AsyncMock()):
            restore_publication_id, _, _ = await _plan_governance(
                Session,
                ids,
                restore_action_id,
                governed_id,
                config,
            )

        async with Session() as db:
            item = (
                await db.execute(
                    select(GraphGovernanceActionItem).where(
                        GraphGovernanceActionItem.action_id == restore_action_id
                    )
                )
            ).scalars().one()
            item.before_hash = "f" * 64
            await db.commit()
        async with Session() as db:
            with pytest.raises(GraphPublicationActivationError):
                await activate_graph_publication(
                    db,
                    restore_publication_id,
                    config=config,
                )
        async with Session() as db:
            entity = await db.get(Entity, ids[3])
            action = await db.get(GraphGovernanceAction, restore_action_id)
            publication = await db.get(GraphPublication, restore_publication_id)
        assert (
            entity.status,
            action.status,
            action.planned_publication_id,
            publication.status,
        ) == (
            "disabled",
            "approved",
            None,
            "failed",
        )
    finally:
        await engine.dispose()


def test_graph_governance_postgres_migration_concurrency_and_atomic_activation(
    monkeypatch,
):
    name = f"vkt_v09_governance_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        alembic_command.upgrade(Config("alembic.ini"), "0038")
        asyncio.run(_exercise_graph_governance_runtime(_database_url(name)))
        alembic_command.downgrade(Config("alembic.ini"), "0037")
        assert asyncio.run(
            execute_sql(
                _database_url(name),
                "SELECT to_regclass('public.graph_governance_actions'), "
                "to_regclass('public.graph_governance_action_items')",
            )
        ) == [(None, None)]
        alembic_command.upgrade(Config("alembic.ini"), "0038")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
