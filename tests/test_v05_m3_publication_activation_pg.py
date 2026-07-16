from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import Settings, settings
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services import graph_publication_activation as activation_service
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
    plan_graph_publication_rollback,
)
from app.services.graph_publication_planner import plan_graph_publication


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.5 acceptance.",
)


@dataclass(frozen=True, slots=True)
class SeededGraph:
    library_id: uuid.UUID
    ontology_id: uuid.UUID
    first_entity_id: uuid.UUID
    relation_id: uuid.UUID | None = None
    relation_evidence_id: uuid.UUID | None = None


def _parts():
    parsed = urlparse(_DSN.replace("+asyncpg", ""))
    return parsed.hostname, parsed.port or 5432, parsed.username, parsed.password


async def _admin(host: str, port: int, user: str, password: str, sql: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database="postgres",
    )
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _create_database(monkeypatch, prefix: str) -> tuple[str, str]:
    host, port, user, password = _parts()
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(host, port, user, password, f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(host, port, user, password, f'CREATE DATABASE "{name}"'))
    monkeypatch.setattr(settings, "db_host", host)
    monkeypatch.setattr(settings, "db_port", int(port))
    monkeypatch.setattr(settings, "db_user", user)
    monkeypatch.setattr(settings, "db_password", password)
    monkeypatch.setattr(settings, "db_name", name)
    return name, f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _drop_database(name: str) -> None:
    host, port, user, password = _parts()
    asyncio.run(_admin(host, port, user, password, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _alembic_state(dsn: str) -> tuple[str, bool, bool]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            publications = (
                await connection.execute(text("SELECT to_regclass('public.graph_publications')"))
            ).scalar_one()
            items = (
                await connection.execute(text("SELECT to_regclass('public.graph_publication_items')"))
            ).scalar_one()
        return version, publications is not None, items is not None
    finally:
        await engine.dispose()


def test_v05_migration_round_trip_on_disposable_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v05_migration")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        assert asyncio.run(_alembic_state(dsn)) == ("0023", True, True)

        command.downgrade(Config("alembic.ini"), "0022")
        assert asyncio.run(_alembic_state(dsn)) == ("0022", False, False)

        command.upgrade(Config("alembic.ini"), "0023")
        assert asyncio.run(_alembic_state(dsn)) == ("0023", True, True)
    finally:
        _drop_database(name)


def _publication_config() -> Settings:
    return Settings(_env_file=None, graph_publication_enabled=True)


async def _seed_graph(Session, slug: str, *, with_relation: bool = False) -> SeededGraph:
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    first_type_id = uuid.uuid4()
    second_type_id = uuid.uuid4()
    first_entity_id = uuid.uuid4()
    second_entity_id = uuid.uuid4()
    async with Session() as db:
        db.add(
            Library(
                id=library_id,
                slug=slug,
                name=slug,
                embedding_model="bge-m3",
                embedding_dim=1024,
                qdrant_collection=slug,
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
        db.add_all(
            [
                EntityType(
                    id=first_type_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    key="person",
                    label="Person",
                    properties_schema={},
                    status="active",
                ),
                EntityType(
                    id=second_type_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    key="team",
                    label="Team",
                    properties_schema={},
                    status="active",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Entity(
                    id=first_entity_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    entity_type_id=first_type_id,
                    canonical_name="Alice",
                    normalized_name="alice",
                    properties={},
                    status="active",
                    source_type="manual",
                ),
                Entity(
                    id=second_entity_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    entity_type_id=second_type_id,
                    canonical_name="Platform",
                    normalized_name="platform",
                    properties={},
                    status="active",
                    source_type="manual",
                ),
            ]
        )
        await db.flush()
        relation_id = None
        relation_evidence_id = None
        if with_relation:
            relation_type_id = uuid.uuid4()
            relation_id = uuid.uuid4()
            document_id = uuid.uuid4()
            revision_id = uuid.uuid4()
            evidence_id = uuid.uuid4()
            relation_evidence_id = uuid.uuid4()
            db.add(
                RelationType(
                    id=relation_type_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    key="member_of",
                    label="Member of",
                    direction="directed",
                    requires_evidence=True,
                    default_review_policy="auto_active",
                    properties_schema={},
                    status="active",
                )
            )
            await db.flush()
            db.add(
                RelationTypeConstraint(
                    id=uuid.uuid4(),
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    relation_type_id=relation_type_id,
                    source_entity_type_id=first_type_id,
                    target_entity_type_id=second_type_id,
                    cardinality="many_to_one",
                    requires_review=False,
                    status="active",
                )
            )
            db.add(
                KnowledgeRelation(
                    id=relation_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    relation_type_id=relation_type_id,
                    source_entity_id=first_entity_id,
                    target_entity_id=second_entity_id,
                    properties={},
                    status="active",
                    review_status="approved",
                    source_type="extracted",
                    confidence=0.95,
                )
            )
            db.add(
                Document(
                    id=document_id,
                    library_id=library_id,
                    title="Evidence",
                    content_hash=uuid.uuid4().hex,
                    current_revision=1,
                    current_revision_id=revision_id,
                    latest_revision_id=revision_id,
                    status="ready",
                )
            )
            db.add(
                DocumentRevision(
                    id=revision_id,
                    document_id=document_id,
                    library_id=library_id,
                    revision_no=1,
                    content_hash=uuid.uuid4().hex,
                    parser_name="plain",
                    parser_version="v1",
                    chunking_strategy="fixed",
                    chunking_strategy_version="v1",
                    status="ready",
                )
            )
            db.add(
                EvidenceUnit(
                    id=evidence_id,
                    library_id=library_id,
                    document_id=document_id,
                    document_revision_id=revision_id,
                    evidence_kind="text",
                    status="active",
                )
            )
            await db.flush()
            db.add(
                RelationEvidence(
                    id=relation_evidence_id,
                    library_id=library_id,
                    relation_id=relation_id,
                    evidence_id=evidence_id,
                    document_id=document_id,
                    document_revision_id=revision_id,
                    support_type="supports",
                    status="active",
                )
            )
        await db.commit()
    return SeededGraph(
        library_id=library_id,
        ontology_id=ontology_id,
        first_entity_id=first_entity_id,
        relation_id=relation_id,
        relation_evidence_id=relation_evidence_id,
    )


async def _plan(
    Session,
    graph: SeededGraph,
    key: str,
    config: Settings,
    *,
    include_drafts: bool = False,
) -> uuid.UUID:
    async with Session() as db:
        library = await db.get(Library, graph.library_id)
        result = await plan_graph_publication(
            db,
            library,
            ontology_version_id=graph.ontology_id,
            idempotency_key=key,
            include_drafts=include_drafts,
            config=config,
        )
        await db.commit()
        return result.publication.id


async def _activate(Session, publication_id: uuid.UUID, config: Settings):
    async with Session() as db:
        try:
            result = await activate_graph_publication(db, publication_id, config=config)
            return "active", result.publication.id
        except GraphPublicationActivationError as exc:
            return "failed", exc.code


async def _add_manual_entity(Session, graph: SeededGraph, name: str) -> uuid.UUID:
    async with Session() as db:
        entity_type_id = (
            await db.execute(
                select(EntityType.id)
                .where(
                    EntityType.library_id == graph.library_id,
                    EntityType.ontology_version_id == graph.ontology_id,
                )
                .order_by(EntityType.id)
                .limit(1)
            )
        ).scalar_one()
        entity_id = uuid.uuid4()
        db.add(
            Entity(
                id=entity_id,
                library_id=graph.library_id,
                ontology_version_id=graph.ontology_id,
                entity_type_id=entity_type_id,
                canonical_name=name,
                normalized_name=name.lower(),
                properties={},
                status="active",
                source_type="manual",
            )
        )
        await db.commit()
        return entity_id


async def _run_pg_acceptance(dsn: str) -> None:
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    config = _publication_config()
    try:
        concurrency_graph = await _seed_graph(Session, "v05_concurrency_" + uuid.uuid4().hex[:8])

        first_plan_a, first_plan_b = await asyncio.gather(
            _plan(Session, concurrency_graph, "concurrent-plan-a", config),
            _plan(Session, concurrency_graph, "concurrent-plan-b", config),
        )
        assert first_plan_a == first_plan_b
        first_publication_id = first_plan_a

        await _add_manual_entity(Session, concurrency_graph, "Bob")
        second_publication_id = await _plan(Session, concurrency_graph, "second-plan", config)
        assert second_publication_id != first_publication_id

        outcomes = await asyncio.gather(
            _activate(Session, first_publication_id, config),
            _activate(Session, second_publication_id, config),
        )
        assert [status for status, _ in outcomes].count("active") == 1
        assert [status for status, _ in outcomes].count("failed") == 1

        async with Session() as db:
            current = (
                await db.execute(
                    select(GraphPublication).where(
                        GraphPublication.library_id == concurrency_graph.library_id,
                        GraphPublication.ontology_version_id == concurrency_graph.ontology_id,
                        GraphPublication.status.in_(("active", "degraded")),
                    )
                )
            ).scalars().all()
            assert len(current) == 1
            assert current[0].id == second_publication_id
            failed = await db.get(GraphPublication, first_publication_id)
            assert failed.status == "failed"
            assert failed.error_code in {"publication_snapshot_changed", "publication_parent_changed"}

        rollback_graph = await _seed_graph(Session, "v05_rollback_" + uuid.uuid4().hex[:8])
        rollback_target_id = await _plan(Session, rollback_graph, "rollback-base", config)
        assert (await _activate(Session, rollback_target_id, config))[0] == "active"
        await _add_manual_entity(Session, rollback_graph, "Carol")
        replacement_id = await _plan(Session, rollback_graph, "rollback-replacement", config)
        assert (await _activate(Session, replacement_id, config))[0] == "active"

        async with Session() as db:
            rollback_plan = await plan_graph_publication_rollback(
                db,
                rollback_target_id,
                idempotency_key="rollback-command",
                config=config,
            )
            rollback_publication_id = rollback_plan.publication.id
            await db.commit()
        assert (await _activate(Session, rollback_publication_id, config))[0] == "active"

        relation_graph = await _seed_graph(
            Session,
            "v05_stale_" + uuid.uuid4().hex[:8],
            with_relation=True,
        )
        relation_target_id = await _plan(Session, relation_graph, "relation-base", config)
        assert (await _activate(Session, relation_target_id, config))[0] == "active"
        await _add_manual_entity(Session, relation_graph, "Dave")
        relation_replacement_id = await _plan(Session, relation_graph, "relation-replacement", config)
        assert (await _activate(Session, relation_replacement_id, config))[0] == "active"

        async with Session() as db:
            evidence = await db.get(RelationEvidence, relation_graph.relation_evidence_id)
            evidence.status = "stale"
            await db.commit()
        async with Session() as db:
            with pytest.raises(GraphPublicationActivationError) as exc_info:
                await plan_graph_publication_rollback(
                    db,
                    relation_target_id,
                    idempotency_key="stale-rollback",
                    config=config,
                )
            assert exc_info.value.code == "rollback_item_ineligible"
            current_id = (
                await db.execute(
                    select(GraphPublication.id).where(
                        GraphPublication.library_id == relation_graph.library_id,
                        GraphPublication.ontology_version_id == relation_graph.ontology_id,
                        GraphPublication.status.in_(("active", "degraded")),
                    )
                )
            ).scalar_one()
            assert current_id == relation_replacement_id

        failure_graph = await _seed_graph(Session, "v05_failure_" + uuid.uuid4().hex[:8])
        async with Session() as db:
            draft_entity = await db.get(Entity, failure_graph.first_entity_id)
            draft_entity.status = "draft"
            await db.commit()
        failure_publication_id = await _plan(
            Session,
            failure_graph,
            "failure-plan",
            config,
            include_drafts=True,
        )
        real_audit_record = activation_service.audit_log.record
        audit_calls = 0

        async def fail_first_audit(*args, **kwargs):
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 1:
                raise RuntimeError("injected activation audit failure")
            return await real_audit_record(*args, **kwargs)

        activation_service.audit_log.record = fail_first_audit
        try:
            failure_outcome = await _activate(Session, failure_publication_id, config)
        finally:
            activation_service.audit_log.record = real_audit_record
        assert failure_outcome == ("failed", "activation_failed")
        async with Session() as db:
            failed_publication = await db.get(GraphPublication, failure_publication_id)
            rolled_back_entity = await db.get(Entity, failure_graph.first_entity_id)
            assert failed_publication.status == "failed"
            assert failed_publication.error_code == "activation_failed"
            assert rolled_back_entity.status == "draft"
            current_for_failed_scope = (
                await db.execute(
                    select(func.count())
                    .select_from(GraphPublication)
                    .where(
                        GraphPublication.library_id == failure_graph.library_id,
                        GraphPublication.ontology_version_id == failure_graph.ontology_id,
                        GraphPublication.status.in_(("active", "degraded")),
                    )
                )
            ).scalar_one()
            assert current_for_failed_scope == 0

        async with Session() as db:
            current_count = (
                await db.execute(
                    select(func.count())
                    .select_from(GraphPublication)
                    .where(GraphPublication.status.in_(("active", "degraded")))
                )
            ).scalar_one()
            assert current_count == 3
            audits = (await db.execute(select(AuditLog))).scalars().all()
            assert audits
            forbidden_keys = {"quote_text", "evidence_text_snapshot", "fact_snapshot", "properties"}
            assert all(not forbidden_keys.intersection((audit.target or {}).keys()) for audit in audits)
            active_item_count = (
                await db.execute(
                    select(func.count())
                    .select_from(GraphPublicationItem)
                    .where(GraphPublicationItem.status == "active")
                )
            ).scalar_one()
            assert active_item_count > 0
    finally:
        await engine.dispose()


def test_v05_planner_activation_concurrency_and_rollback_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v05_m3")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        asyncio.run(_run_pg_acceptance(dsn))
    finally:
        _drop_database(name)
