from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.audit import AuditLog
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
from app.services import graph_evidence
from app.services import graph_publication_read
from app.services.graph_publication_reconcile import reconcile_current_publication
from tests.test_v05_m3_publication_activation_pg import (
    _activate,
    _create_database,
    _drop_database,
    _plan,
    _publication_config,
    _seed_graph,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("VECTOR_KB_PG_TEST_DSN"),
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.5 acceptance.",
)


async def _active_publication(Session, graph, key, config):
    publication_id = await _plan(Session, graph, key, config)
    assert (await _activate(Session, publication_id, config))[0] == "active"
    return publication_id


async def _assert_degraded(db, publication_id, *, minimum_items=1):
    publication = await db.get(GraphPublication, publication_id)
    degraded_items = (
        await db.execute(
            select(GraphPublicationItem).where(
                GraphPublicationItem.publication_id == publication_id,
                GraphPublicationItem.status == "degraded",
            )
        )
    ).scalars().all()
    assert publication.status == "degraded"
    assert len(degraded_items) >= minimum_items
    assert publication.last_reconciled_at is not None
    return publication, degraded_items


async def _run_evidence_and_recovery_acceptance(Session, config) -> None:
    graph = await _seed_graph(
        Session,
        "v05_m5_evidence_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    publication_id = await _active_publication(Session, graph, "m5-evidence-base", config)

    async with Session() as db:
        library = await db.get(Library, graph.library_id)
        support = await db.get(RelationEvidence, graph.relation_evidence_id)
        result = await graph_evidence.mark_document_graph_evidence_stale(
            db,
            library,
            document_id=support.document_id,
        )
        assert result.stale_relation_evidence == 1
        assert result.stale_relations == 1
        publication, degraded_items = await _assert_degraded(db, publication_id)
        assert any(item.relation_id == graph.relation_id for item in degraded_items)
        audits = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "graph_publication.degraded")
            )
        ).scalars().all()
        assert len(audits) == 1
        forbidden = {"quote_text", "evidence_text_snapshot", "fact_snapshot", "properties"}
        assert not forbidden.intersection((audits[0].target or {}).keys())
        assert publication.status == "degraded"
        await db.commit()

    async with Session() as db:
        library = await db.get(Library, graph.library_id)
        healthy = await graph_publication_read.list_published_relations(
            db,
            library,
            graph.ontology_id,
        )
        assert healthy.healthy is False
        assert healthy.rows == ()

    replacement_id = await _plan(Session, graph, "m5-clean-replacement", config)
    assert replacement_id != publication_id
    assert (await _activate(Session, replacement_id, config))[0] == "active"
    async with Session() as db:
        previous = await db.get(GraphPublication, publication_id)
        replacement = await db.get(GraphPublication, replacement_id)
        assert previous.status == "superseded"
        assert replacement.status == "active"


async def _run_contradiction_acceptance(Session, config) -> None:
    graph = await _seed_graph(
        Session,
        "v05_m5_contradiction_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    publication_id = await _active_publication(Session, graph, "m5-contradiction", config)

    async with Session() as db:
        library = await db.get(Library, graph.library_id)
        support = await db.get(RelationEvidence, graph.relation_evidence_id)
        source_evidence = await db.get(EvidenceUnit, support.evidence_id)
        contradictory_evidence_id = uuid.uuid4()
        db.add(
            EvidenceUnit(
                id=contradictory_evidence_id,
                library_id=graph.library_id,
                document_id=source_evidence.document_id,
                document_revision_id=source_evidence.document_revision_id,
                evidence_kind="text",
                text_quote="contradictory source",
                status="active",
            )
        )
        await db.flush()
        await graph_evidence.create_relation_evidence(
            db,
            library,
            relation_id=graph.relation_id,
            evidence_id=contradictory_evidence_id,
            support_type="contradicts",
            status="active",
        )
        _, degraded_items = await _assert_degraded(db, publication_id)
        assert any(item.relation_id == graph.relation_id for item in degraded_items)
        await db.rollback()

    async with Session() as db:
        publication = await db.get(GraphPublication, publication_id)
        assert publication.status == "active"


async def _run_formal_drift_acceptance(Session, config) -> None:
    entity_graph = await _seed_graph(Session, "v05_m5_entity_" + uuid.uuid4().hex[:8])
    entity_publication_id = await _active_publication(
        Session,
        entity_graph,
        "m5-entity-status",
        config,
    )
    async with Session() as db:
        entity = await db.get(Entity, entity_graph.first_entity_id)
        entity.status = "disabled"
        result = await reconcile_current_publication(db, entity_publication_id, config=config)
        assert result.reason_counts == {"formal_status_not_active": 1}
        await _assert_degraded(db, entity_publication_id)
        await db.rollback()

    relation_graph = await _seed_graph(
        Session,
        "v05_m5_relation_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    relation_publication_id = await _active_publication(
        Session,
        relation_graph,
        "m5-relation-status",
        config,
    )
    async with Session() as db:
        relation = await db.get(KnowledgeRelation, relation_graph.relation_id)
        relation.status = "pending_review"
        result = await reconcile_current_publication(db, relation_publication_id, config=config)
        assert result.reason_counts == {"formal_status_not_active": 1}
        await _assert_degraded(db, relation_publication_id)
        await db.rollback()


async def _run_schema_and_ontology_acceptance(Session, config) -> None:
    type_graph = await _seed_graph(Session, "v05_m5_type_" + uuid.uuid4().hex[:8])
    type_publication_id = await _active_publication(Session, type_graph, "m5-type", config)
    async with Session() as db:
        entity = await db.get(Entity, type_graph.first_entity_id)
        entity_type = await db.get(EntityType, entity.entity_type_id)
        entity_type.status = "disabled"
        result = await reconcile_current_publication(db, type_publication_id, config=config)
        assert result.reason_counts == {"eligibility_changed": 1}
        await _assert_degraded(db, type_publication_id)
        await db.rollback()

    schema_graph = await _seed_graph(Session, "v05_m5_schema_" + uuid.uuid4().hex[:8])
    schema_publication_id = await _active_publication(Session, schema_graph, "m5-schema", config)
    async with Session() as db:
        entity = await db.get(Entity, schema_graph.first_entity_id)
        entity_type = await db.get(EntityType, entity.entity_type_id)
        entity_type.properties_schema = {
            "type": "object",
            "required": ["level"],
            "properties": {"level": {"type": "integer"}},
        }
        result = await reconcile_current_publication(db, schema_publication_id, config=config)
        assert result.reason_counts == {"eligibility_changed": 1}
        await _assert_degraded(db, schema_publication_id)
        await db.rollback()

    relation_type_graph = await _seed_graph(
        Session,
        "v05_m5_relation_type_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    relation_type_publication_id = await _active_publication(
        Session,
        relation_type_graph,
        "m5-relation-type",
        config,
    )
    async with Session() as db:
        relation = await db.get(KnowledgeRelation, relation_type_graph.relation_id)
        relation_type = await db.get(RelationType, relation.relation_type_id)
        relation_type.status = "disabled"
        result = await reconcile_current_publication(
            db,
            relation_type_publication_id,
            config=config,
        )
        assert result.reason_counts == {"eligibility_changed": 1}
        await _assert_degraded(db, relation_type_publication_id)
        await db.rollback()

    constraint_graph = await _seed_graph(
        Session,
        "v05_m5_constraint_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    constraint_publication_id = await _active_publication(
        Session,
        constraint_graph,
        "m5-constraint",
        config,
    )
    async with Session() as db:
        constraint = (
            await db.execute(
                select(RelationTypeConstraint).where(
                    RelationTypeConstraint.library_id == constraint_graph.library_id
                )
            )
        ).scalars().one()
        constraint.status = "disabled"
        result = await reconcile_current_publication(
            db,
            constraint_publication_id,
            config=config,
        )
        assert result.reason_counts == {"eligibility_changed": 1}
        await _assert_degraded(db, constraint_publication_id)
        await db.rollback()

    ontology_graph = await _seed_graph(Session, "v05_m5_ontology_" + uuid.uuid4().hex[:8])
    ontology_publication_id = await _active_publication(
        Session,
        ontology_graph,
        "m5-ontology",
        config,
    )
    async with Session() as db:
        ontology = await db.get(OntologyVersion, ontology_graph.ontology_id)
        ontology.status = "disabled"
        result = await reconcile_current_publication(db, ontology_publication_id, config=config)
        assert result.reason_counts == {"ontology_not_active": 2}
        await _assert_degraded(db, ontology_publication_id, minimum_items=2)
        await db.rollback()


async def _run_pg_acceptance(dsn: str) -> None:
    engine = create_async_engine(dsn)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    config = _publication_config()
    try:
        await _run_evidence_and_recovery_acceptance(Session, config)
        await _run_contradiction_acceptance(Session, config)
        await _run_formal_drift_acceptance(Session, config)
        await _run_schema_and_ontology_acceptance(Session, config)
    finally:
        await engine.dispose()


def test_v05_m5_reconciliation_matrix_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v05_m5")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        monkeypatch.setattr(graph_evidence.settings, "graph_publication_enabled", True)
        asyncio.run(_run_pg_acceptance(dsn))
    finally:
        _drop_database(name)
