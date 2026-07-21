from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_review import GraphEntityMergeCandidate
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.services.graph_extraction_eval import (
    GraphEvalDatasetCounts,
    GraphEvalDatasetMinimums,
    GraphEvalDocument,
    GraphEvalManifest,
    LoadedGraphEvalDataset,
    canonical_graph_eval_hash,
)
from app.services.graph_extraction_eval_runtime import (
    create_clean_eval_database,
    create_eval_jobs,
    drop_eval_database,
    eval_uuid,
    run_eval_workers,
    seed_eval_dataset,
    upgrade_eval_database,
)
from app.services.graph_extraction_jobs import create_graph_extraction_job
from app.services.graph_extraction_materializer import materialize_graph_extraction_job
from app.services.graph_extraction_provider import (
    GraphExtractionProviderError,
    MockGraphExtractor,
)
from app.services.graph_extraction_worker import (
    claim_graph_extraction_unit,
    process_graph_extraction_unit,
)


ROOT = Path(__file__).resolve().parents[1]
_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)


def _loaded_gold() -> LoadedGraphEvalDataset:
    payload = json.loads(
        (ROOT / "eval/graph_extraction/gold_v1.json").read_text(encoding="utf-8")
    )
    document = GraphEvalDocument.model_validate(payload)
    manifest = GraphEvalManifest(
        schema_version="graph-extraction-eval-manifest-v1",
        dataset_id="gold-v1",
        source_file="eval/graph_extraction/gold_v1.json",
        synthetic=True,
        document_keys=("gold-v1",),
        minimums=GraphEvalDatasetMinimums(
            documents=1,
            units=6,
            entities=6,
            relations=5,
        ),
    )
    counts = GraphEvalDatasetCounts(documents=1, units=6, entities=6, relations=5)
    return LoadedGraphEvalDataset(
        manifest=manifest,
        documents=(document,),
        counts=counts,
        dataset_manifest_sha256=canonical_graph_eval_hash(
            manifest.model_dump(mode="json")
        ),
        dataset_content_sha256=canonical_graph_eval_hash(
            [document.model_dump(mode="json")]
        ),
    )


def _entity(local_id: str, name: str, type_key: str, quote: str, *, aliases=()):
    return {
        "local_id": local_id,
        "name": name,
        "entity_type_key": type_key,
        "aliases": list(aliases),
        "properties": {},
        "external_mapping_hints": [],
        "confidence": 0.95,
        "evidence": [{"context_ref": "c0", "quote": quote}],
    }


def _relation(source: str, type_key: str, target: str, quote: str):
    return {
        "source_local_id": source,
        "relation_type_key": type_key,
        "target_local_id": target,
        "properties": {},
        "confidence": 0.95,
        "evidence": [{"context_ref": "c0", "quote": quote}],
    }


def _payloads() -> dict[str, dict]:
    quotes = {
        "u1": "人力资源部负责员工入职流程。",
        "u2": "张三隶属于人力资源部。",
        "u3": "员工入职管理制度适用于人力资源部。",
        "u4": "员工入职流程受员工入职管理制度约束。",
        "u5": "人力资源部经理审批正式员工入职申请。",
        "u6": "HR 是人力资源部的常用简称。",
    }
    return {
        "u1": {
            "entities": [
                _entity("hr", "人力资源部", "department", quotes["u1"]),
                _entity("flow", "员工入职流程", "process", quotes["u1"]),
            ],
            "relations": [_relation("hr", "responsible_for", "flow", quotes["u1"])],
        },
        "u2": {
            "entities": [
                _entity("person", "张三", "person", quotes["u2"]),
                _entity("hr", "人力资源部", "department", quotes["u2"]),
            ],
            "relations": [_relation("person", "belongs_to", "hr", quotes["u2"])],
        },
        "u3": {
            "entities": [
                _entity("policy", "员工入职管理制度", "policy", quotes["u3"]),
                _entity("hr", "人力资源部", "department", quotes["u3"]),
            ],
            "relations": [_relation("policy", "applies_to", "hr", quotes["u3"])],
        },
        "u4": {
            "entities": [
                _entity("flow", "员工入职流程", "process", quotes["u4"]),
                _entity("policy", "员工入职管理制度", "policy", quotes["u4"]),
            ],
            "relations": [_relation("policy", "constrains", "flow", quotes["u4"])],
        },
        "u5": {
            "entities": [
                _entity("manager", "人力资源部经理", "position", quotes["u5"]),
                _entity("application", "正式员工入职申请", "process", quotes["u5"]),
            ],
            "relations": [
                _relation("manager", "approves", "application", quotes["u5"])
            ],
        },
        "u6": {
            "entities": [
                _entity("hr", "人力资源部", "department", quotes["u6"], aliases=("HR",)),
            ],
            "relations": [],
        },
    }


def _provider_factory(dataset_id: str):
    providers = {
        eval_uuid(dataset_id, "chunk", "gold-v1", unit_key): MockGraphExtractor(payload)
        for unit_key, payload in _payloads().items()
    }

    def factory(unit: GraphExtractionUnit):
        return providers[unit.center_chunk_id]

    return factory


class AlwaysTimeoutProvider:
    async def extract(self, messages):  # noqa: ARG002
        raise GraphExtractionProviderError("timeout", "mock timeout", latency_ms=1)


async def _create_production_job(sessions, seed):
    async with sessions() as db, db.begin():
        library = await db.get(Library, seed.library_id)
        document = await db.get(Document, seed.document_ids_by_key["gold-v1"])
        revision = await db.get(
            DocumentRevision,
            seed.revision_ids_by_key["gold-v1"],
        )
        return await create_graph_extraction_job(
            db,
            library=library,
            document=document,
            revision=revision,
            trigger_type="manual",
            execution_mode="production",
            requested_by=None,
            idempotency_key="m6-gold-v1-production",
        )


async def _formal_checksum(db) -> tuple:
    entities = (
        await db.execute(
            select(Entity.id, Entity.status, Entity.normalized_name)
            .where(Entity.status == "active")
            .order_by(Entity.id)
        )
    ).all()
    relations = (
        await db.execute(
            select(KnowledgeRelation.id, KnowledgeRelation.status)
            .where(KnowledgeRelation.status == "active")
            .order_by(KnowledgeRelation.id)
        )
    ).all()
    evidence = (
        await db.execute(
            select(RelationEvidence.id, RelationEvidence.status)
            .where(RelationEvidence.status == "active")
            .order_by(RelationEvidence.id)
        )
    ).all()
    return tuple(entities), tuple(relations), tuple(evidence)


async def _exercise(database_url) -> None:
    loaded = _loaded_gold()
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await seed_eval_dataset(sessions, loaded=loaded)
        production = await _create_production_job(sessions, seed)
        production_id = production.id

        async with sessions() as db:
            first = await claim_graph_extraction_unit(
                db,
                worker_id="m6-gold-fence",
                lease_seconds=180,
                max_attempts=3,
            )
        assert first is not None and first.claim_token is not None
        wrong = await process_graph_extraction_unit(
            sessions,
            unit_id=first.id,
            claim_token=uuid.uuid4(),
            provider=_provider_factory("gold-v1")(first),
        )
        assert wrong.outcome == "lost_lease"
        async with sessions() as db:
            assert await db.scalar(
                select(func.count())
                .select_from(ExtractionRawOutputAttempt)
                .where(ExtractionRawOutputAttempt.extraction_unit_id == first.id)
            ) == 0
            for model in (GraphEntityCandidate, GraphRelationCandidate):
                assert await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.job_id == production_id)
                ) == 0
            for model in (Entity, EntityMention, KnowledgeRelation, RelationEvidence):
                assert await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.created_by_job_id == production_id)
                ) == 0
        correct = await process_graph_extraction_unit(
            sessions,
            unit_id=first.id,
            claim_token=first.claim_token,
            provider=_provider_factory("gold-v1")(first),
        )
        if correct.outcome != "succeeded":
            async with sessions() as db:
                failed_unit = await db.get(GraphExtractionUnit, first.id)
                attempts = (
                    await db.execute(
                        select(
                            ExtractionRawOutputAttempt.request_status,
                            ExtractionRawOutputAttempt.parse_status,
                            ExtractionRawOutputAttempt.parse_error,
                        ).where(
                            ExtractionRawOutputAttempt.extraction_unit_id == first.id
                        )
                    )
                ).all()
            pytest.fail(
                "gold unit processing failed: "
                f"result={correct!r}, unit_error={failed_unit.error_code!r}, "
                f"unit_message={failed_unit.error_message!r}, "
                f"attempts={attempts!r}"
            )
        await run_eval_workers(
            sessions,
            library_id=seed.library_id,
            workers=2,
            provider_factory=_provider_factory("gold-v1"),
        )
        materialized = await materialize_graph_extraction_job(
            sessions,
            job_id=production_id,
        )
        assert materialized.entity_count == 6
        assert materialized.entity_mention_count == 11
        assert materialized.relation_count == 5
        assert materialized.relation_evidence_count == 5

        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Entity)) == 6
            assert await db.scalar(
                select(func.count()).select_from(Entity).where(Entity.status == "draft")
            ) == 6
            assert await db.scalar(select(func.count()).select_from(EntityMention)) == 11
            assert await db.scalar(
                select(func.count())
                .select_from(EntityMention)
                .where(EntityMention.status == "active")
            ) == 11
            assert await db.scalar(select(func.count()).select_from(EntityAlias)) == 0
            assert await db.scalar(
                select(func.count()).select_from(KnowledgeRelation)
            ) == 5
            assert await db.scalar(
                select(func.count()).select_from(KnowledgeRelation).where(
                    KnowledgeRelation.status == "draft"
                )
            ) == 5
            assert await db.scalar(
                select(func.count()).select_from(KnowledgeRelation).where(
                    KnowledgeRelation.status == "active"
                )
            ) == 0
            assert await db.scalar(
                select(func.count()).select_from(RelationEvidence)
            ) == 5
            assert await db.scalar(
                select(func.count()).select_from(RelationEvidence).where(
                    RelationEvidence.status == "active"
                )
            ) == 5
            assert await db.scalar(
                select(func.count(func.distinct(RelationEvidence.relation_id))).where(
                    RelationEvidence.created_by_job_id == production_id
                )
            ) == 5
            assert await db.scalar(
                select(func.count()).select_from(RelationEvidence).where(
                    RelationEvidence.created_by_job_id == production_id,
                    RelationEvidence.document_revision_id
                    != seed.revision_ids_by_key["gold-v1"],
                )
            ) == 0
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityMergeCandidate)
            ) == 0
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityCandidate).where(
                    GraphEntityCandidate.job_id == production_id,
                    GraphEntityCandidate.status == "materialized",
                )
            ) == 6
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityCandidate).where(
                    GraphEntityCandidate.job_id == production_id,
                    GraphEntityCandidate.status.in_(("pending_review", "rejected")),
                )
            ) == 0
            assert await db.scalar(
                select(func.count()).select_from(GraphRelationCandidate).where(
                    GraphRelationCandidate.job_id == production_id,
                    GraphRelationCandidate.status == "materialized",
                )
            ) == 5
            assert await db.scalar(
                select(func.count()).select_from(GraphRelationCandidate).where(
                    GraphRelationCandidate.job_id == production_id,
                    GraphRelationCandidate.status.in_(("pending_review", "rejected")),
                )
            ) == 0
            duplicate_relations = (
                await db.execute(
                    select(KnowledgeRelation.relation_type_id)
                    .where(KnowledgeRelation.created_by_job_id == production_id)
                    .group_by(
                        KnowledgeRelation.relation_type_id,
                        KnowledgeRelation.source_entity_id,
                        KnowledgeRelation.target_entity_id,
                    )
                    .having(func.count() > 1)
                )
            ).first()
            assert duplicate_relations is None

        eval_job_ids = await create_eval_jobs(sessions, loaded=loaded, seed=seed)
        await run_eval_workers(
            sessions,
            library_id=seed.library_id,
            workers=2,
            provider_factory=_provider_factory("gold-v1"),
        )
        async with sessions() as db:
            eval_job = await db.get(GraphExtractionJob, eval_job_ids[0])
            assert eval_job.status == "succeeded"
            for model in (Entity, EntityMention, KnowledgeRelation, RelationEvidence):
                assert await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.created_by_job_id.in_(eval_job_ids))
                ) == 0

        async with sessions() as db, db.begin():
            department_type = await db.scalar(
                select(EntityType).where(
                    EntityType.library_id == seed.library_id,
                    EntityType.key == "department",
                )
            )
            process_type = await db.scalar(
                select(EntityType).where(
                    EntityType.library_id == seed.library_id,
                    EntityType.key == "process",
                )
            )
            relation_type = await db.scalar(
                select(RelationType).where(
                    RelationType.library_id == seed.library_id,
                    RelationType.key == "responsible_for",
                )
            )
            source = Entity(
                library_id=seed.library_id,
                ontology_version_id=department_type.ontology_version_id,
                entity_type_id=department_type.id,
                canonical_name="Sentinel Department",
                normalized_name="sentinel department",
                properties={},
                status="active",
                source_type="manual",
            )
            target = Entity(
                library_id=seed.library_id,
                ontology_version_id=process_type.ontology_version_id,
                entity_type_id=process_type.id,
                canonical_name="Sentinel Process",
                normalized_name="sentinel process",
                properties={},
                status="active",
                source_type="manual",
            )
            db.add_all([source, target])
            await db.flush()
            sentinel_relation = KnowledgeRelation(
                library_id=seed.library_id,
                ontology_version_id=department_type.ontology_version_id,
                relation_type_id=relation_type.id,
                source_entity_id=source.id,
                target_entity_id=target.id,
                properties={},
                status="active",
                review_status="approved",
                source_type="manual",
            )
            db.add(sentinel_relation)
            await db.flush()
            evidence_id = eval_uuid("gold-v1", "evidence", "gold-v1", "u1")
            db.add(
                RelationEvidence(
                    library_id=seed.library_id,
                    relation_id=sentinel_relation.id,
                    evidence_id=evidence_id,
                    document_id=seed.document_ids_by_key["gold-v1"],
                    document_revision_id=seed.revision_ids_by_key["gold-v1"],
                    chunk_id=eval_uuid("gold-v1", "chunk", "gold-v1", "u1"),
                    support_type="supports",
                    quote_text="人力资源部负责员工入职流程。",
                    evidence_text_snapshot="人力资源部负责员工入职流程。",
                    source_span={
                        "start": 0,
                        "end": len("人力资源部负责员工入职流程。"),
                    },
                    confidence=1.0,
                    status="active",
                )
            )
        async with sessions() as db:
            before = await _formal_checksum(db)

        async with sessions() as db, db.begin():
            library = await db.get(Library, seed.library_id)
            document = await db.get(Document, seed.document_ids_by_key["gold-v1"])
            revision = await db.get(
                DocumentRevision,
                seed.revision_ids_by_key["gold-v1"],
            )
            failed_job = await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="full_rerun",
                execution_mode="production",
                requested_by=None,
                idempotency_key="m6-gold-failed-rerun",
                rerun_of_job_id=production_id,
            )
            failed_job_id = failed_job.id
        await run_eval_workers(
            sessions,
            library_id=seed.library_id,
            workers=2,
            provider_factory=lambda _unit: AlwaysTimeoutProvider(),
        )
        async with sessions() as db:
            failed_job = await db.get(GraphExtractionJob, failed_job_id)
            assert failed_job.status == "failed"
            assert await _formal_checksum(db) == before
            for model in (Entity, EntityMention, KnowledgeRelation, RelationEvidence):
                assert await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.created_by_job_id == failed_job_id)
                ) == 0
    finally:
        await engine.dispose()


def test_m6_gold_v1_on_real_postgresql(monkeypatch):
    assert _DSN is not None
    database_name = f"vkt_m6_eval_gold_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
    database_url = asyncio.run(create_clean_eval_database(_DSN, database_name))
    try:
        upgrade_eval_database(database_url, repository_root=ROOT)
        asyncio.run(_exercise(database_url))
    finally:
        asyncio.run(drop_eval_database(_DSN, database_name))
