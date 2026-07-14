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

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_extraction_jobs import (
    cancel_graph_extraction_job,
    create_graph_extraction_job,
    retry_graph_extraction_job,
)
from app.services.graph_extraction_materializer import (
    materialize_graph_extraction_job,
)
from app.services.graph_extraction_provider import (
    GraphExtractionProviderError,
    MockGraphExtractor,
)
from app.services.graph_extraction_purge import (
    purge_document_graph_extraction_payloads,
)
from app.services.graph_extraction_worker import (
    claim_graph_extraction_unit,
    process_graph_extraction_unit,
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
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _payload(person: str, team: str, quote: str) -> dict:
    return {
        "entities": [
            {
                "local_id": "person",
                "name": person,
                "entity_type_key": "person",
                "aliases": [],
                "properties": {},
                "external_mapping_hints": [],
                "confidence": 1.0,
                "evidence": [{"context_ref": "c0", "quote": quote}],
            },
            {
                "local_id": "team",
                "name": team,
                "entity_type_key": "team",
                "aliases": [],
                "properties": {},
                "external_mapping_hints": [],
                "confidence": 1.0,
                "evidence": [{"context_ref": "c0", "quote": quote}],
            },
        ],
        "relations": [
            {
                "source_local_id": "person",
                "relation_type_key": "member_of",
                "target_local_id": "team",
                "properties": {},
                "confidence": 1.0,
                "evidence": [{"context_ref": "c0", "quote": quote}],
            }
        ],
    }


class TimeoutProvider:
    async def extract(self, messages):  # noqa: ARG002
        raise GraphExtractionProviderError(
            "timeout",
            "mock timeout",
            latency_ms=1,
        )


async def _seed(name: str) -> dict[str, uuid.UUID]:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {
        key: uuid.uuid4()
        for key in (
            "library",
            "document",
            "revision",
            "ontology",
            "person_type",
            "team_type",
            "relation_type",
            "chunk_0",
            "chunk_1",
            "evidence_0",
            "evidence_1",
        )
    }
    try:
        async with sessions() as db, db.begin():
            library = Library(
                id=ids["library"],
                slug="m5-pg",
                name="M5 PostgreSQL",
                embedding_model="bge-m3",
                embedding_dim=1024,
                vector_distance="cosine",
                chunk_size=1000,
                chunk_overlap=120,
                retrieval_mode="dense",
                qdrant_collection="m5_pg",
                graph_extraction_enabled=True,
                external_llm_enabled=True,
                graph_extraction_allowed_security_levels=["internal"],
            )
            document = Document(
                id=ids["document"],
                library_id=ids["library"],
                title="Teams",
                doc_metadata={},
                content_hash="1" * 64,
                current_revision=1,
                security_level="internal",
                status="ready",
            )
            revision = DocumentRevision(
                id=ids["revision"],
                document_id=ids["document"],
                library_id=ids["library"],
                revision_no=1,
                title="Teams",
                document_metadata={},
                content_hash="1" * 64,
                normalized_text=(
                    "Alice belongs to Engineering.\nBob belongs to Sales."
                ),
                parser_name="test",
                parser_version="parser-v1",
                chunking_strategy="test",
                chunking_strategy_version="chunk-v1",
                security_level="internal",
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
            document.current_revision_id = revision.id
            document.latest_revision_id = revision.id

            person_type = EntityType(
                id=ids["person_type"],
                library_id=library.id,
                ontology_version_id=ontology.id,
                key="person",
                label="Person",
                status="active",
            )
            team_type = EntityType(
                id=ids["team_type"],
                library_id=library.id,
                ontology_version_id=ontology.id,
                key="team",
                label="Team",
                status="active",
            )
            relation_type = RelationType(
                id=ids["relation_type"],
                library_id=library.id,
                ontology_version_id=ontology.id,
                key="member_of",
                label="Member of",
                direction="directed",
                requires_evidence=True,
                default_review_policy="auto_active",
                status="active",
            )
            constraint = RelationTypeConstraint(
                library_id=library.id,
                ontology_version_id=ontology.id,
                relation_type_id=relation_type.id,
                source_entity_type_id=person_type.id,
                target_entity_type_id=team_type.id,
                cardinality="many_to_one",
                requires_review=False,
                status="active",
            )
            db.add_all([person_type, team_type, relation_type, constraint])

            texts = [
                "Alice belongs to Engineering.",
                "Bob belongs to Sales.",
            ]
            for index, text in enumerate(texts):
                evidence = EvidenceUnit(
                    id=ids[f"evidence_{index}"],
                    library_id=library.id,
                    document_id=document.id,
                    document_revision_id=revision.id,
                    evidence_kind="chunk",
                    text_quote=text,
                    text_quote_hash=str(index + 1) * 64,
                    security_level="internal",
                    status="active",
                )
                chunk = Chunk(
                    id=ids[f"chunk_{index}"],
                    document_id=document.id,
                    library_id=library.id,
                    document_revision_id=revision.id,
                    evidence_id=evidence.id,
                    seq=index,
                    chunk_kind="text",
                    text=text,
                    token_count=len(text),
                    title_path=["Teams"],
                )
                db.add_all([evidence, chunk])
    finally:
        await engine.dispose()
    return ids


async def _claim(sessions):
    async with sessions() as db:
        unit = await claim_graph_extraction_unit(
            db,
            worker_id="m5-pg-worker",
            lease_seconds=180,
            max_attempts=3,
        )
    assert unit is not None
    assert unit.claim_token is not None
    return unit.id, unit.claim_token


async def _exercise(name: str, ids: dict[str, uuid.UUID]) -> None:
    engine = create_async_engine(_database_url(name))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db, db.begin():
            library = await db.get(Library, ids["library"])
            document = await db.get(Document, ids["document"])
            revision = await db.get(DocumentRevision, ids["revision"])
            job = await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="manual",
                execution_mode="production",
                requested_by=None,
                idempotency_key=None,
            )
            job_id = job.id
            assert job.counts["total"] == 2

        unit_id, token = await _claim(sessions)
        first = await process_graph_extraction_unit(
            sessions,
            unit_id=unit_id,
            claim_token=token,
            provider=MockGraphExtractor(
                _payload(
                    "Alice",
                    "Engineering",
                    "Alice belongs to Engineering.",
                )
            ),
        )
        assert first.outcome == "succeeded"
        assert first.ready_for_materialization is False

        failed_unit_id, failed_token = await _claim(sessions)
        failed = await process_graph_extraction_unit(
            sessions,
            unit_id=failed_unit_id,
            claim_token=failed_token,
            provider=TimeoutProvider(),
        )
        assert failed.outcome == "failed"
        assert failed.error_code == "provider_timeout"

        async with sessions() as db, db.begin():
            library = await db.get(Library, ids["library"])
            job = await retry_graph_extraction_job(
                db,
                library=library,
                job_id=job_id,
                max_attempts=3,
            )
            assert job.id == job_id
            assert job.retry_generation == 1

        retry_unit_id, retry_token = await _claim(sessions)
        assert retry_unit_id == failed_unit_id
        retried = await process_graph_extraction_unit(
            sessions,
            unit_id=retry_unit_id,
            claim_token=retry_token,
            provider=MockGraphExtractor(
                _payload("Bob", "Sales", "Bob belongs to Sales.")
            ),
        )
        assert retried.outcome == "succeeded"
        assert retried.ready_for_materialization is True

        materialized = await materialize_graph_extraction_job(
            sessions,
            job_id=job_id,
        )
        assert materialized.entity_count == 4
        assert materialized.entity_mention_count == 4
        assert materialized.relation_count == 2
        assert materialized.relation_evidence_count == 2

        async with sessions() as db:
            job = await db.get(GraphExtractionJob, job_id)
            assert job.status == "succeeded"
            assert await db.scalar(select(func.count()).select_from(Entity)) == 4
            assert await db.scalar(
                select(func.count()).select_from(Entity).where(Entity.status == "active")
            ) == 0
            assert await db.scalar(select(func.count()).select_from(EntityMention)) == 4
            assert await db.scalar(
                select(func.count())
                .select_from(EntityMention)
                .where(EntityMention.status == "active")
            ) == 4
            assert await db.scalar(
                select(func.count()).select_from(KnowledgeRelation)
            ) == 2
            assert await db.scalar(
                select(func.count())
                .select_from(KnowledgeRelation)
                .where(KnowledgeRelation.status == "active")
            ) == 0
            assert await db.scalar(select(func.count()).select_from(RelationEvidence)) == 2

        async with sessions() as db, db.begin():
            library = await db.get(Library, ids["library"])
            document = await db.get(Document, ids["document"])
            revision = await db.get(DocumentRevision, ids["revision"])
            rerun = await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="full_rerun",
                execution_mode="production",
                requested_by=None,
                idempotency_key="m5-pg-full-rerun",
                rerun_of_job_id=job_id,
            )
            replay = await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="full_rerun",
                execution_mode="production",
                requested_by=None,
                idempotency_key="m5-pg-full-rerun",
                rerun_of_job_id=job_id,
            )
            assert rerun.id == replay.id
            assert rerun.input_fingerprint == job.input_fingerprint
            rerun_id = rerun.id

        claimed_rerun_id, _rerun_token = await _claim(sessions)
        async with sessions() as db:
            claimed_rerun = await db.get(GraphExtractionUnit, claimed_rerun_id)
            assert claimed_rerun.job_id == rerun_id
        async with sessions() as db, db.begin():
            library = await db.get(Library, ids["library"])
            cancelled = await cancel_graph_extraction_job(
                db,
                library=library,
                job_id=rerun_id,
            )
            assert cancelled.status == "cancelled"

        async with sessions() as db, db.begin():
            purged = await purge_document_graph_extraction_payloads(
                db,
                library_id=ids["library"],
                document_id=ids["document"],
            )
            assert purged.job_count == 2

        async with sessions() as db:
            original = await db.get(GraphExtractionJob, job_id)
            assert original.sensitive_payload_purged_at is not None
            contexts = (
                await db.execute(
                    select(ExtractionContextSnapshot).where(
                        ExtractionContextSnapshot.job_id == job_id
                    )
                )
            ).scalars().all()
            assert contexts
            assert all(row.context_text is None and row.context_json is None for row in contexts)
            attempts = (
                await db.execute(
                    select(ExtractionRawOutputAttempt)
                    .join(GraphExtractionUnit)
                    .where(GraphExtractionUnit.job_id == job_id)
                )
            ).scalars().all()
            assert len(attempts) == 3
            assert all(
                row.raw_response is None
                and row.parsed_response is None
                and row.parse_error is None
                for row in attempts
            )
            entity_candidates = (
                await db.execute(
                    select(GraphEntityCandidate).where(
                        GraphEntityCandidate.job_id == job_id
                    )
                )
            ).scalars().all()
            relation_candidates = (
                await db.execute(
                    select(GraphRelationCandidate).where(
                        GraphRelationCandidate.job_id == job_id
                    )
                )
            ).scalars().all()
            assert all(row.canonical_name is None for row in entity_candidates)
            assert all(row.proposed_properties is None for row in relation_candidates)
            assert await db.scalar(
                select(func.count()).select_from(GraphEntityOccurrence)
            ) == 4
            assert await db.scalar(
                select(func.count())
                .select_from(GraphEntityOccurrence)
                .where(GraphEntityOccurrence.raw_payload.is_not(None))
            ) == 0
            assert await db.scalar(
                select(func.count()).select_from(GraphRelationOccurrence)
            ) == 2
            assert await db.scalar(
                select(func.count())
                .select_from(GraphEntityCandidateEvidence)
                .where(GraphEntityCandidateEvidence.quote_text.is_not(None))
            ) == 0
            assert await db.scalar(
                select(func.count())
                .select_from(GraphRelationCandidateEvidence)
                .where(GraphRelationCandidateEvidence.quote_text.is_not(None))
            ) == 0
    finally:
        await engine.dispose()


def test_m5_operational_pipeline_on_real_postgresql(monkeypatch):
    database_name = f"vkt_m5_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(f'CREATE DATABASE "{database_name}"'))
    try:
        _configure_alembic(monkeypatch, database_name)
        monkeypatch.setattr(settings, "graph_extraction_enabled", True)
        monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        ids = asyncio.run(_seed(database_name))
        asyncio.run(_exercise(database_name, ids))
    finally:
        asyncio.run(
            _admin(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{database_name}'"
            )
        )
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{database_name}"'))
