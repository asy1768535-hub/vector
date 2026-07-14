from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.graph_candidate_aggregation import (
    CandidateReplayError,
    canonical_graph_value_hash_v1,
    recompute_job_candidate_aggregates,
    stage_unit_candidate_occurrences,
)
from app.services.graph_candidate_routing import apply_job_candidate_routes
from app.services.graph_candidate_validation import validate_job_candidates
from app.services.graph_extraction_context import build_context_snapshot


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


async def _connect(url: URL):
    import asyncpg

    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )


async def _admin(sql: str) -> None:
    connection = await _connect(_admin_url())
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _configure_alembic(monkeypatch, name: str) -> None:
    from app.config import settings

    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _policy_snapshot() -> dict:
    return {
        "entity_materialization_threshold": 0.85,
        "relation_draft_threshold": 0.85,
        "confidence_weights": {
            "model": 0.25,
            "evidence": 0.35,
            "schema": 0.25,
            "normalization": 0.15,
        },
        "evidence_score_map": {"direct_statement": 1.0, "table_cell": 0.95},
        "schema_score_map": {
            "valid": 1.0,
            "warning": 0.6,
            "boundary_unclear": 0.4,
            "invalid": 0.0,
        },
        "normalization_score_map": {
            "exact_normalized_match": 1.0,
            "exact_alias_match": 0.95,
            "new_entity": 0.9,
            "ambiguous": 0.0,
        },
        "evidence_group_policy": "all_claims_valid",
    }


async def _seed_database(name: str) -> dict[str, object]:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {key: uuid.uuid4() for key in (
        "library",
        "document",
        "revision",
        "ontology",
        "department_type",
        "process_type",
        "relation_type",
        "formal_department",
        "job",
        "unit_0",
        "unit_1",
        "unit_rollback",
    )}
    snapshot = {
        "ontology_version_id": str(ids["ontology"]),
        "entity_types": [
            {
                "id": str(ids["department_type"]),
                "key": "department",
                "properties_schema": {
                    "properties": {"owner": {"type": "string"}}
                },
                "active_attribute_definitions": [],
            },
            {
                "id": str(ids["process_type"]),
                "key": "process",
                "properties_schema": None,
                "active_attribute_definitions": [],
            },
        ],
        "relation_types": [
            {
                "id": str(ids["relation_type"]),
                "key": "responsible_for",
                "direction": "directed",
                "requires_evidence": True,
                "default_review_policy": "auto_active",
                "properties_schema": None,
                "active_attribute_definitions": [],
            }
        ],
        "relation_constraints": [
            {
                "relation_type_id": str(ids["relation_type"]),
                "source_entity_type_id": str(ids["department_type"]),
                "target_entity_type_id": str(ids["process_type"]),
                "cardinality": "one_to_many",
                "requires_review": False,
            }
        ],
    }
    ids["snapshot"] = snapshot
    try:
        async with sessions() as db:
            library = Library(
                id=ids["library"],
                slug="m4-pg",
                name="M4 PostgreSQL",
                embedding_model="bge-m3",
                embedding_dim=1024,
                vector_distance="cosine",
                chunk_size=1000,
                chunk_overlap=120,
                retrieval_mode="dense",
                qdrant_collection="m4_pg",
            )
            document = Document(
                id=ids["document"],
                library_id=ids["library"],
                title="Onboarding",
                doc_metadata={},
                content_hash="1" * 64,
                current_revision=1,
                status="ready",
            )
            revision = DocumentRevision(
                id=ids["revision"],
                document_id=ids["document"],
                library_id=ids["library"],
                revision_no=1,
                title="Onboarding",
                document_metadata={},
                content_hash="1" * 64,
                parser_name="test",
                parser_version="1",
                chunking_strategy="test",
                chunking_strategy_version="1",
                status="ready",
            )
            ontology = OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="default",
                version_no=1,
                status="active",
            )
            db.add(library)
            await db.flush()
            db.add(document)
            await db.flush()
            db.add_all([revision, ontology])
            await db.flush()
            document.current_revision_id = ids["revision"]
            document.latest_revision_id = ids["revision"]

            department_type = EntityType(
                id=ids["department_type"],
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                key="department",
                label="Department",
                properties_schema={"properties": {"owner": {"type": "string"}}},
                status="active",
            )
            process_type = EntityType(
                id=ids["process_type"],
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                key="process",
                label="Process",
                status="active",
            )
            relation_type = RelationType(
                id=ids["relation_type"],
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                key="responsible_for",
                label="Responsible For",
                direction="directed",
                requires_evidence=True,
                default_review_policy="auto_active",
                status="active",
            )
            constraint = RelationTypeConstraint(
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                relation_type_id=ids["relation_type"],
                source_entity_type_id=ids["department_type"],
                target_entity_type_id=ids["process_type"],
                cardinality="one_to_many",
                requires_review=False,
                status="active",
            )
            db.add_all([department_type, process_type, relation_type, constraint])
            await db.flush()

            formal_department = Entity(
                id=ids["formal_department"],
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                entity_type_id=ids["department_type"],
                canonical_name="People Operations",
                normalized_name="people operations",
                properties={},
                status="active",
                source_type="manual",
            )
            alias = EntityAlias(
                library_id=ids["library"],
                entity_id=ids["formal_department"],
                alias="Human Resources",
                normalized_alias="human resources",
                source_type="manual",
                status="active",
            )
            db.add(formal_department)
            await db.flush()
            db.add(alias)

            contexts = (
                (0, ids["unit_0"], "Human Resources owns onboarding."),
                (1, ids["unit_1"], "HUMAN RESOURCES owns onboarding."),
                (2, ids["unit_rollback"], "Temporary process is documented."),
            )
            context_rows = []
            for ordinal, unit_id, text in contexts:
                block_id = uuid.uuid4()
                evidence_id = uuid.uuid4()
                chunk_id = uuid.uuid4()
                ids[f"evidence_{ordinal}"] = evidence_id
                ids[f"chunk_{ordinal}"] = chunk_id
                block = DocumentBlock(
                    id=block_id,
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    seq=ordinal,
                    block_kind="paragraph",
                    title_path=["Onboarding"],
                    text=text,
                    parser_name="test",
                    parser_version="1",
                )
                evidence = EvidenceUnit(
                    id=evidence_id,
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    document_block_id=block_id,
                    evidence_kind="chunk",
                    text_quote=text,
                    status="active",
                )
                chunk = Chunk(
                    id=chunk_id,
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    block_id=block_id,
                    evidence_id=evidence_id,
                    seq=ordinal,
                    chunk_kind="text",
                    text=text,
                    token_count=5,
                    title_path=["Onboarding"],
                )
                db.add(block)
                await db.flush()
                db.add(evidence)
                await db.flush()
                db.add(chunk)
                context_rows.append((ordinal, unit_id, chunk_id, evidence_id))
            await db.flush()

            job = GraphExtractionJob(
                id=ids["job"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                ontology_version_id=ids["ontology"],
                trigger_type="manual",
                execution_mode="production",
                status="processing",
                current_stage="aggregating",
                input_fingerprint="2" * 64,
                idempotency_key="m4-pg-idempotency",
                retry_generation=0,
                model_provider="dashscope",
                model_name="qwen-plus",
                prompt_version="v1",
                extractor_version="v1",
                output_parser_version="v1",
                context_policy_version="v1",
                extraction_policy_version="v1",
                normalization_rule_version="normalization_v1",
                confidence_policy_version="v1",
                document_parser_version="1",
                chunking_strategy_version="1",
                model_config_snapshot={},
                policy_config_snapshot=_policy_snapshot(),
                model_config_hash="3" * 64,
                policy_config_hash="4" * 64,
                ontology_snapshot=snapshot,
                ontology_snapshot_hash=canonical_graph_value_hash_v1(snapshot),
                prompt_content_hash="5" * 64,
            )
            db.add(job)
            await db.flush()
            for ordinal, unit_id, chunk_id, evidence_id in context_rows:
                db.add(
                    GraphExtractionUnit(
                        id=unit_id,
                        job_id=ids["job"],
                        library_id=ids["library"],
                        document_revision_id=ids["revision"],
                        ordinal=ordinal,
                        center_chunk_id=chunk_id,
                        center_evidence_id=evidence_id,
                        unit_fingerprint=canonical_graph_value_hash_v1(
                            {"job": str(ids["job"]), "ordinal": ordinal}
                        ),
                        status="queued",
                        model_attempt_count=0,
                        retryable=False,
                    )
                )
            await db.commit()

        async with sessions() as db:
            job = await db.get(GraphExtractionJob, ids["job"])
            for ordinal, unit_id, _, _ in context_rows:
                unit = await db.get(GraphExtractionUnit, unit_id)
                snapshot_row = await build_context_snapshot(
                    db,
                    job=job,
                    unit=unit,
                    previous_chunks=0,
                    next_chunks=0,
                    max_context_chars=10_000,
                )
                ids[f"snapshot_{ordinal}"] = snapshot_row.id
            await db.commit()
        return ids
    finally:
        await engine.dispose()


def _payload(name: str, owner: str, quote: str, confidence: float) -> GraphExtractionPayload:
    return GraphExtractionPayload.model_validate(
        {
            "entities": [
                {
                    "local_id": "department",
                    "name": name,
                    "entity_type_key": "department",
                    "aliases": ["HR" if owner == "Alice" else "People Team"],
                    "properties": {"owner": owner},
                    "external_mapping_hints": [],
                    "confidence": confidence,
                    "evidence": [{"context_ref": "c0", "quote": quote}],
                },
                {
                    "local_id": "process",
                    "name": "Onboarding",
                    "entity_type_key": "process",
                    "aliases": [],
                    "properties": {},
                    "external_mapping_hints": [],
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": quote}],
                },
            ],
            "relations": [
                {
                    "source_local_id": "department",
                    "relation_type_key": "responsible_for",
                    "target_local_id": "process",
                    "properties": {},
                    "confidence": confidence,
                    "evidence": [{"context_ref": "c0", "quote": quote}],
                }
            ],
        }
    )


async def _stage_one(name: str, ids: dict[str, object], ordinal: int, payload) -> None:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db, db.begin():
            job = await db.get(GraphExtractionJob, ids["job"])
            unit = await db.get(GraphExtractionUnit, ids[f"unit_{ordinal}"])
            snapshot = await db.get(ExtractionContextSnapshot, ids[f"snapshot_{ordinal}"])
            await stage_unit_candidate_occurrences(
                db, job=job, unit=unit, snapshot=snapshot, payload=payload
            )
    finally:
        await engine.dispose()


async def _exercise_m4(name: str, ids: dict[str, object]) -> None:
    first_quote = "Human Resources owns onboarding."
    second_quote = "HUMAN RESOURCES owns onboarding."
    first = _payload(" Human Resources ", "Alice", first_quote, 0.8)
    second = _payload("HUMAN   RESOURCES", "Bob", second_quote, 0.95)
    await asyncio.gather(
        _stage_one(name, ids, 1, second),
        _stage_one(name, ids, 0, first),
    )

    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db, db.begin():
            job = await db.get(GraphExtractionJob, ids["job"])
            aggregate = await recompute_job_candidate_aggregates(db, job=job)
            validation = await validate_job_candidates(db, job=job)
            routing = await apply_job_candidate_routes(db, job=job)
            assert aggregate.entity_candidate_count == 2
            assert aggregate.relation_candidate_count == 1
            assert aggregate.property_conflict_count == 1
            assert validation.rejected_count == 0
            assert routing.validated_count == 1
            assert routing.pending_review_count == 2

        async with sessions() as db:
            entities = list(
                (
                    await db.execute(
                        select(GraphEntityCandidate).order_by(
                            GraphEntityCandidate.entity_type_key
                        )
                    )
                )
                .scalars()
                .all()
            )
            department = next(row for row in entities if row.entity_type_key == "department")
            process = next(row for row in entities if row.entity_type_key == "process")
            relation = (
                await db.execute(select(GraphRelationCandidate))
            ).scalars().one()
            assert department.canonical_name == "Human Resources"
            assert department.proposed_properties == {"owner": "Alice"}
            assert department.model_confidence == 0.95
            assert department.matched_entity_id == ids["formal_department"]
            assert department.normalization_method == "exact_alias_match"
            assert department.status == "pending_review"
            assert process.status == "validated"
            assert relation.evidence_support_mode == "evidence_group"
            assert relation.status == "pending_review"
            assert relation.review_reason == "evidence_group"
            assert 0 <= relation.final_confidence <= 1
            assert await db.scalar(select(func.count()).select_from(GraphEntityOccurrence)) == 4
            assert await db.scalar(select(func.count()).select_from(GraphRelationOccurrence)) == 2
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityCandidateEvidence)
            ) == 4
            assert await db.scalar(
                select(func.count()).select_from(GraphRelationCandidateEvidence)
            ) == 2
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "property_conflict"
                )
            ) == 1

        await _stage_one(name, ids, 0, first)
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(GraphEntityCandidate)) == 2
            assert await db.scalar(select(func.count()).select_from(GraphRelationCandidate)) == 1
            assert await db.scalar(select(func.count()).select_from(GraphEntityOccurrence)) == 4

        changed_relation = _payload(" Human Resources ", "Alice", first_quote, 0.8)
        changed_relation.relations[0].properties = {"note": "changed"}
        with pytest.raises(CandidateReplayError) as exc_info:
            await _stage_one(name, ids, 0, changed_relation)
        assert exc_info.value.code == "relation_occurrence_replay_mismatch"
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(GraphRelationCandidate)) == 1
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "endpoint_mismatch"
                )
            ) == 0

        mismatch_payload = _payload(" Human Resources ", "Alice", first_quote, 0.8)
        mismatch_payload.relations[0].target_local_id = "department"
        await _stage_one(name, ids, 0, mismatch_payload)
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(GraphRelationCandidate)) == 1
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "endpoint_mismatch"
                )
            ) == 1

        rollback_payload = GraphExtractionPayload.model_validate(
            {
                "entities": [
                    {
                        "local_id": "temporary",
                        "name": "Temporary Process",
                        "entity_type_key": "process",
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "context_ref": "c0",
                                "quote": "Temporary process is documented.",
                            }
                        ],
                    }
                ],
                "relations": [],
            }
        )
        async with sessions() as db:
            job = await db.get(GraphExtractionJob, ids["job"])
            unit = await db.get(GraphExtractionUnit, ids["unit_rollback"])
            snapshot = await db.get(ExtractionContextSnapshot, ids["snapshot_2"])
            await stage_unit_candidate_occurrences(
                db, job=job, unit=unit, snapshot=snapshot, payload=rollback_payload
            )
            await db.rollback()
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(GraphEntityCandidate)) == 2
            assert await db.scalar(select(func.count()).select_from(KnowledgeRelation)) == 0
            assert await db.scalar(select(func.count()).select_from(RelationEvidence)) == 0
            assert await db.scalar(select(func.count()).select_from(Entity)) == 1
            statuses = (
                await db.execute(
                    select(GraphEntityCandidate.status).union_all(
                        select(GraphRelationCandidate.status)
                    )
                )
            ).scalars().all()
            assert set(statuses) <= {"validated", "pending_review", "rejected"}

        second_entity_id = uuid.uuid4()
        async with sessions() as db, db.begin():
            db.add(
                Entity(
                    id=second_entity_id,
                    library_id=ids["library"],
                    ontology_version_id=ids["ontology"],
                    entity_type_id=ids["department_type"],
                    canonical_name="HR Shared Services",
                    normalized_name="hr shared services",
                    properties={},
                    status="active",
                    source_type="manual",
                )
            )
            await db.flush()
            db.add(
                EntityAlias(
                    library_id=ids["library"],
                    entity_id=second_entity_id,
                    alias="Human Resources",
                    normalized_alias="human resources",
                    source_type="manual",
                    status="active",
                )
            )
        async with sessions() as db, db.begin():
            job = await db.get(GraphExtractionJob, ids["job"])
            await validate_job_candidates(db, job=job)
        async with sessions() as db:
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityMergeCandidate).where(
                    GraphEntityMergeCandidate.status == "pending_review"
                )
            ) == 2
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "entity_merge_ambiguity",
                    GraphExtractionConflict.status == "open",
                )
            ) == 1

        async with sessions() as db, db.begin():
            second_alias = (
                await db.execute(
                    select(EntityAlias).where(EntityAlias.entity_id == second_entity_id)
                )
            ).scalars().one()
            second_alias.status = "disabled"
            job = await db.get(GraphExtractionJob, ids["job"])
            await validate_job_candidates(db, job=job)
        async with sessions() as db:
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityMergeCandidate).where(
                    GraphEntityMergeCandidate.status == "superseded"
                )
            ) == 2
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "entity_merge_ambiguity",
                    GraphExtractionConflict.status == "superseded",
                )
            ) == 1

        async with sessions() as db, db.begin():
            second_alias = (
                await db.execute(
                    select(EntityAlias).where(EntityAlias.entity_id == second_entity_id)
                )
            ).scalars().one()
            second_alias.status = "active"
            job = await db.get(GraphExtractionJob, ids["job"])
            await validate_job_candidates(db, job=job)
        async with sessions() as db:
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityMergeCandidate).where(
                    GraphEntityMergeCandidate.status == "pending_review"
                )
            ) == 2
            assert await db.scalar(
                select(func.count()).select_from(GraphExtractionConflict).where(
                    GraphExtractionConflict.conflict_type == "entity_merge_ambiguity",
                    GraphExtractionConflict.status == "open",
                )
            ) == 1
    finally:
        await engine.dispose()


def test_m4_candidate_pipeline_on_real_postgresql(monkeypatch):
    name = "vkt_v04_m4_" + uuid.uuid4().hex[:8]
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "0022")
        ids = asyncio.run(_seed_database(name))
        asyncio.run(_exercise_m4(name, ids))
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
