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
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.schema_lifecycle_action import SchemaLifecycleAction
from app.models.user import User
from app.services.schema_lifecycle_actions import (
    activate_schema_version,
    apply_schema_item_command,
    clone_schema_version,
    delete_schema_draft,
    disable_schema_version,
)
from app.services.schema_lifecycle_contracts import (
    SchemaLifecycleCommand,
    SchemaLifecycleError,
    deterministic_schema_item_id,
)
from app.services.schema_lifecycle_read import (
    load_schema_version_bundle,
    schema_version_state_hash,
)


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None


def _admin_url() -> URL:
    assert _ADMIN_URL is not None
    return _ADMIN_URL


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


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


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


async def _seed(Session):
    actor_id = uuid.uuid4()
    library_id = uuid.uuid4()
    version_id = uuid.uuid4()
    unrelated_version_id = uuid.uuid4()
    type_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    async with Session() as db:
        db.add(
            User(
                id=actor_id,
                email=f"schema-{actor_id}@example.com",
                hashed_password="hash",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        library = Library(
            id=library_id,
            organization_id=DEFAULT_ORGANIZATION_ID,
            slug=f"schema-{library_id.hex[:8]}",
            name="Schema Lifecycle",
            embedding_model="bge-m3",
            embedding_dim=1024,
            qdrant_collection=f"schema_{library_id.hex[:8]}",
        )
        db.add(library)
        await db.flush()
        db.add(
            OntologyVersion(
                id=version_id,
                library_id=library_id,
                version_key="enterprise",
                version_no=1,
                status="active",
            )
        )
        db.add(
            OntologyVersion(
                id=unrelated_version_id,
                library_id=library_id,
                version_key="compliance",
                version_no=1,
                status="active",
            )
        )
        await db.flush()
        db.add(
            EntityType(
                id=type_id,
                library_id=library_id,
                ontology_version_id=version_id,
                key="company",
                label="Company",
                properties_schema={"type": "object"},
                status="active",
            )
        )
        await db.flush()
        db.add(
            Entity(
                id=entity_id,
                library_id=library_id,
                ontology_version_id=version_id,
                entity_type_id=type_id,
                canonical_name="Acme",
                normalized_name="acme",
                properties={"country": "CN"},
                status="active",
                source_type="manual",
            )
        )
        db.add(
            GraphPublication(
                id=publication_id,
                library_id=library_id,
                ontology_version_id=version_id,
                status="active",
                source_mode="initial_seed",
                manifest_hash="a" * 64,
                idempotency_key="initial-publication",
                policy_snapshot={},
                entity_count=1,
                relation_count=0,
            )
        )
        await db.commit()
    return (
        actor_id,
        library_id,
        version_id,
        unrelated_version_id,
        entity_id,
        publication_id,
    )


async def _exercise(database_url: URL) -> None:
    engine = create_async_engine(database_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        (
            actor_id,
            library_id,
            source_id,
            unrelated_version_id,
            entity_id,
            publication_id,
        ) = await _seed(Session)
        async with Session() as db:
            library = await db.get(Library, library_id)
            source = await load_schema_version_bundle(db, library, source_id)
            source_hash = schema_version_state_hash(source)
        clone_command = SchemaLifecycleCommand(
            library_id=library_id,
            ontology_version_id=source_id,
            actor_user_id=actor_id,
            action_kind="clone_version",
            target_kind="ontology_version",
            target_id=source_id,
            expected_state_hash=source_hash,
            idempotency_key="clone-enterprise-v2",
            payload={"description": "Enterprise v2"},
        )

        async def clone_once():
            async with Session() as db:
                library = await db.get(Library, library_id)
                result = await clone_schema_version(db, library, clone_command)
                await db.commit()
                return result.reused, result.bundle.version.id

        concurrent = await asyncio.gather(clone_once(), clone_once())
        assert sorted(reused for reused, _version_id in concurrent) == [False, True]
        assert len({version_id for _reused, version_id in concurrent}) == 1
        draft_id = concurrent[0][1]

        async with Session() as db:
            library = await db.get(Library, library_id)
            draft = await load_schema_version_bundle(db, library, draft_id)
            draft_hash = schema_version_state_hash(draft)
        invalid_reference_command = SchemaLifecycleCommand(
            library_id=library_id,
            ontology_version_id=draft_id,
            actor_user_id=actor_id,
            action_kind="create_item",
            target_kind="attribute",
            target_id=deterministic_schema_item_id(
                draft_id, "attribute", "create-invalid-owner"
            ),
            expected_state_hash=draft_hash,
            idempotency_key="create-invalid-owner",
            payload={
                "owner_kind": "entity_type",
                "owner_type_id": str(uuid.uuid4()),
                "key": "invalid_owner",
                "label": "Invalid Owner",
                "value_type": "string",
            },
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            with pytest.raises(SchemaLifecycleError) as invalid_reference:
                await apply_schema_item_command(
                    db, library, invalid_reference_command
                )
            await db.rollback()
        assert invalid_reference.value.code == "schema_lifecycle_dependency_conflict"

        new_type_id = deterministic_schema_item_id(
            draft_id, "entity_type", "create-person-type"
        )
        create_command = SchemaLifecycleCommand(
            library_id=library_id,
            ontology_version_id=draft_id,
            actor_user_id=actor_id,
            action_kind="create_item",
            target_kind="entity_type",
            target_id=new_type_id,
            expected_state_hash=draft_hash,
            idempotency_key="create-person-type",
            payload={"key": "person", "label": "Person"},
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            created = await apply_schema_item_command(db, library, create_command)
            await db.commit()
            assert not created.reused
        async with Session() as db:
            library = await db.get(Library, library_id)
            replay = await apply_schema_item_command(db, library, create_command)
            await db.commit()
            assert replay.reused
            assert any(row.id == new_type_id for row in replay.bundle.entity_types)

        stale_command = SchemaLifecycleCommand(
            library_id=library_id,
            ontology_version_id=draft_id,
            actor_user_id=actor_id,
            action_kind="create_item",
            target_kind="entity_type",
            target_id=deterministic_schema_item_id(
                draft_id, "entity_type", "create-stale-type"
            ),
            expected_state_hash=draft_hash,
            idempotency_key="create-stale-type",
            payload={"key": "stale_type", "label": "Stale"},
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            with pytest.raises(SchemaLifecycleError) as stale:
                await apply_schema_item_command(db, library, stale_command)
            await db.rollback()
        assert stale.value.code == "schema_lifecycle_state_changed"

        async with Session() as db:
            library = await db.get(Library, library_id)
            draft = await load_schema_version_bundle(db, library, draft_id)
            activation = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=draft_id,
                actor_user_id=actor_id,
                action_kind="activate_version",
                target_kind="ontology_version",
                target_id=draft_id,
                expected_state_hash=schema_version_state_hash(draft),
                idempotency_key="activate-enterprise-v2",
                payload={
                    "confirmation": "activate_schema_version",
                    "expected_active_version_id": str(source_id),
                },
            )
            activated = await activate_schema_version(db, library, activation)
            await db.commit()
            assert activated.bundle.version.status == "active"

        async with Session() as db:
            source = await db.get(OntologyVersion, source_id)
            draft = await db.get(OntologyVersion, draft_id)
            unrelated = await db.get(OntologyVersion, unrelated_version_id)
            entity = await db.get(Entity, entity_id)
            publication = await db.get(GraphPublication, publication_id)
            action_count = (
                await db.execute(
                    select(func.count(SchemaLifecycleAction.id)).where(
                        SchemaLifecycleAction.library_id == library_id
                    )
                )
            ).scalar_one()
            source_type_statuses = (
                await db.execute(
                    select(EntityType.status).where(
                        EntityType.ontology_version_id == source_id
                    )
                )
            ).scalars().all()
            draft_type_statuses = (
                await db.execute(
                    select(EntityType.status).where(
                        EntityType.ontology_version_id == draft_id
                    )
                )
            ).scalars().all()
        assert (source.status, draft.status) == ("disabled", "active")
        assert unrelated.status == "active"
        assert (entity.ontology_version_id, entity.status, entity.properties) == (
            source_id,
            "active",
            {"country": "CN"},
        )
        assert (
            publication.ontology_version_id,
            publication.status,
            publication.manifest_hash,
        ) == (source_id, "active", "a" * 64)
        assert source_type_statuses == ["active"]
        assert set(draft_type_statuses) == {"active"}
        assert action_count == 3
    finally:
        await engine.dispose()


def test_schema_lifecycle_postgres_migration_concurrency_and_activation(monkeypatch):
    name = f"vkt_v09_schema_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        alembic_command.upgrade(config, "0039")
        asyncio.run(_exercise(_database_url(name)))
        alembic_command.downgrade(config, "0038")
        alembic_command.upgrade(config, "0039")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _exercise_version_retirement(database_url: URL) -> None:
    engine = create_async_engine(database_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        actor_id, library_id, source_id, _, entity_id, publication_id = await _seed(
            Session
        )
        async with Session() as db:
            library = await db.get(Library, library_id)
            source = await load_schema_version_bundle(db, library, source_id)
            clone = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=source_id,
                actor_user_id=actor_id,
                action_kind="clone_version",
                target_kind="ontology_version",
                target_id=source_id,
                expected_state_hash=schema_version_state_hash(source),
                idempotency_key="retirement-clone-v2",
                payload={"description": "Disposable draft"},
            )
            draft_id = (await clone_schema_version(db, library, clone)).bundle.version.id
            await db.commit()

        async with Session() as db:
            library = await db.get(Library, library_id)
            draft = await load_schema_version_bundle(db, library, draft_id)
            delete = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=draft_id,
                actor_user_id=actor_id,
                action_kind="delete_version",
                target_kind="ontology_version",
                target_id=draft_id,
                expected_state_hash=schema_version_state_hash(draft),
                idempotency_key="delete-retirement-draft-v2",
                payload={"confirmation": "delete_schema_draft"},
            )
            deleted = await delete_schema_draft(db, library, delete)
            await db.commit()
            assert deleted.reused is False

        async with Session() as db:
            library = await db.get(Library, library_id)
            replay = await delete_schema_draft(db, library, delete)
            await db.commit()
            assert replay.reused is True

        async with Session() as db:
            library = await db.get(Library, library_id)
            source = await load_schema_version_bundle(db, library, source_id)
            referenced_clone = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=source_id,
                actor_user_id=actor_id,
                action_kind="clone_version",
                target_kind="ontology_version",
                target_id=source_id,
                expected_state_hash=schema_version_state_hash(source),
                idempotency_key="retirement-clone-v3",
                payload={"description": "Referenced draft"},
            )
            referenced_draft = (
                await clone_schema_version(db, library, referenced_clone)
            ).bundle
            referenced_draft_id = referenced_draft.version.id
            referenced_type_id = referenced_draft.entity_types[0].id
            await db.commit()

        async with Session() as db:
            db.add(
                Entity(
                    library_id=library_id,
                    ontology_version_id=referenced_draft_id,
                    entity_type_id=referenced_type_id,
                    canonical_name="Referenced draft entity",
                    normalized_name="referenced draft entity",
                    status="draft",
                    source_type="manual",
                )
            )
            await db.commit()

        async with Session() as db:
            library = await db.get(Library, library_id)
            referenced_draft = await load_schema_version_bundle(
                db, library, referenced_draft_id
            )
            referenced_delete = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=referenced_draft_id,
                actor_user_id=actor_id,
                action_kind="delete_version",
                target_kind="ontology_version",
                target_id=referenced_draft_id,
                expected_state_hash=schema_version_state_hash(referenced_draft),
                idempotency_key="delete-referenced-draft-v3",
                payload={"confirmation": "delete_schema_draft"},
            )
            with pytest.raises(SchemaLifecycleError) as referenced_error:
                await delete_schema_draft(db, library, referenced_delete)
            await db.rollback()
            assert referenced_error.value.code == "schema_lifecycle_dependency_conflict"

        async with Session() as db:
            library = await db.get(Library, library_id)
            source = await load_schema_version_bundle(db, library, source_id)
            disable = SchemaLifecycleCommand(
                library_id=library_id,
                ontology_version_id=source_id,
                actor_user_id=actor_id,
                action_kind="disable_version",
                target_kind="ontology_version",
                target_id=source_id,
                expected_state_hash=schema_version_state_hash(source),
                idempotency_key="disable-retirement-source-v1",
                payload={"confirmation": "disable_schema_version"},
            )
            disabled = await disable_schema_version(db, library, disable)
            await db.commit()
            assert disabled.bundle.version.status == "disabled"

        async with Session() as db:
            source = await db.get(OntologyVersion, source_id)
            draft = await db.get(OntologyVersion, draft_id)
            referenced_draft = await db.get(OntologyVersion, referenced_draft_id)
            entity = await db.get(Entity, entity_id)
            publication = await db.get(GraphPublication, publication_id)
            source_type_statuses = (
                await db.execute(
                    select(EntityType.status).where(
                        EntityType.ontology_version_id == source_id
                    )
                )
            ).scalars().all()
            draft_type_statuses = (
                await db.execute(
                    select(EntityType.status).where(
                        EntityType.ontology_version_id == draft_id
                    )
                )
            ).scalars().all()
            actions = set(
                (
                    await db.execute(
                        select(SchemaLifecycleAction.action_kind).where(
                            SchemaLifecycleAction.library_id == library_id
                        )
                    )
                ).scalars().all()
            )
        assert source.status == "disabled"
        assert draft.status == "deleted"
        assert referenced_draft.status == "draft"
        assert source_type_statuses == ["active"]
        assert draft_type_statuses == ["deleted"]
        assert entity.status == "active"
        assert publication.status == "active"
        assert {"clone_version", "delete_version", "disable_version"} <= actions
    finally:
        await engine.dispose()


def test_schema_version_retirement_postgres_state_and_audit(monkeypatch):
    name = f"vkt_v09_schema_retire_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        alembic_command.upgrade(config, "0043")
        alembic_command.downgrade(config, "0042")
        alembic_command.upgrade(config, "0043")
        asyncio.run(_exercise_version_retirement(_database_url(name)))
        with pytest.raises(Exception, match="Cannot downgrade"):  # noqa: B017
            alembic_command.downgrade(config, "0042")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
