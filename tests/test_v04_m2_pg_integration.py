from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.services.graph_extraction_attempts import (
    AttemptCompletion,
    create_pending_attempt,
    finalize_attempt,
)
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
    from app.config import settings

    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


async def _seed_and_exercise(name: str) -> None:
    engine = create_async_engine(_database_url(name))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    block_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    job_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    claim_token = uuid.uuid4()
    now = datetime.now(timezone.utc)
    ontology_snapshot = {
        "entity_types": [{"key": "department"}],
        "relation_types": [{"key": "responsible_for"}],
    }

    try:
        async with session_factory() as db:
            library = Library(
                id=library_id,
                slug="m2-pg",
                name="M2 PostgreSQL",
                embedding_model="bge-m3",
                embedding_dim=1024,
                vector_distance="cosine",
                chunk_size=1000,
                chunk_overlap=120,
                retrieval_mode="dense",
                qdrant_collection="m2_pg",
            )
            document = Document(
                id=document_id,
                library_id=library_id,
                title="Policy",
                doc_metadata={"owner": "HR"},
                content_hash="1" * 64,
                current_revision=1,
                current_revision_id=None,
                latest_revision_id=None,
                status="ready",
            )
            revision = DocumentRevision(
                id=revision_id,
                document_id=document_id,
                library_id=library_id,
                revision_no=1,
                title="Policy",
                document_metadata={"owner": "HR"},
                content_hash="1" * 64,
                parser_name="test",
                parser_version="1",
                chunking_strategy="test",
                chunking_strategy_version="1",
                status="ready",
            )
            block = DocumentBlock(
                id=block_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=revision_id,
                seq=0,
                block_kind="paragraph",
                title_path=["Policy"],
                text="HR is responsible for onboarding.",
                parser_name="test",
                parser_version="1",
            )
            evidence = EvidenceUnit(
                id=evidence_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=revision_id,
                document_block_id=block_id,
                evidence_kind="chunk",
                text_quote="HR is responsible for onboarding.",
                status="active",
            )
            chunk = Chunk(
                id=chunk_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=revision_id,
                block_id=block_id,
                evidence_id=evidence_id,
                seq=0,
                chunk_kind="text",
                text="HR is responsible for onboarding.",
                token_count=7,
                title_path=["Policy"],
            )
            ontology = OntologyVersion(
                id=ontology_id,
                library_id=library_id,
                version_key="default",
                version_no=1,
                status="active",
            )
            job = GraphExtractionJob(
                id=job_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=revision_id,
                ontology_version_id=ontology_id,
                trigger_type="manual",
                execution_mode="production",
                status="processing",
                current_stage="building_context",
                input_fingerprint="2" * 64,
                idempotency_key="m2-pg-idempotency",
                retry_generation=0,
                model_provider="dashscope",
                model_name="qwen-plus",
                prompt_version="v1",
                extractor_version="v1",
                output_parser_version="v1",
                context_policy_version="context-v1",
                extraction_policy_version="v1",
                normalization_rule_version="normalization-v1",
                confidence_policy_version="v1",
                document_parser_version="1",
                chunking_strategy_version="1",
                model_config_snapshot={},
                policy_config_snapshot={},
                model_config_hash="3" * 64,
                policy_config_hash="4" * 64,
                ontology_snapshot=ontology_snapshot,
                ontology_snapshot_hash="5" * 64,
                prompt_content_hash="6" * 64,
            )
            unit = GraphExtractionUnit(
                id=unit_id,
                job_id=job_id,
                library_id=library_id,
                document_revision_id=revision_id,
                ordinal=0,
                center_chunk_id=chunk_id,
                center_evidence_id=evidence_id,
                unit_fingerprint="7" * 64,
                status="processing",
                model_attempt_count=0,
                retryable=False,
                worker_id="m2-pg-worker",
                claim_token=claim_token,
                claimed_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
            db.add(library)
            await db.flush()
            db.add(document)
            await db.flush()
            db.add_all([revision, ontology])
            await db.flush()
            document.current_revision_id = revision_id
            document.latest_revision_id = revision_id
            db.add(block)
            await db.flush()
            db.add(evidence)
            await db.flush()
            db.add(chunk)
            await db.flush()
            db.add_all(
                [
                    ChunkBlock(
                        chunk_id=chunk_id,
                        document_block_id=block_id,
                        document_revision_id=revision_id,
                        seq=0,
                    ),
                    ChunkEvidence(
                        chunk_id=chunk_id,
                        evidence_id=evidence_id,
                        document_revision_id=revision_id,
                        seq=0,
                    ),
                ]
            )
            await db.flush()
            db.add(job)
            await db.flush()
            db.add(unit)
            await db.commit()

        async with session_factory() as db:
            job = await db.get(GraphExtractionJob, job_id)
            unit = await db.get(GraphExtractionUnit, unit_id)
            snapshot = await build_context_snapshot(
                db,
                job=job,
                unit=unit,
                previous_chunks=1,
                next_chunks=1,
                max_context_chars=10_000,
            )
            snapshot_id = snapshot.id
            assert snapshot.context_mapping["c0"]["evidence_ids"] == [str(evidence_id)]
            await db.commit()

        async with session_factory() as db:
            first_attempt = await create_pending_attempt(
                db,
                unit_id=unit_id,
                context_snapshot_id=snapshot_id,
                claim_token=claim_token,
                request_payload_hash="8" * 64,
                max_attempts=3,
            )
            first_attempt_id = first_attempt.id
            assert first_attempt.attempt_no == 1

        completion = AttemptCompletion(
            request_status="succeeded",
            parse_status="valid",
            latency_ms=25,
            raw_response='{"entities":[],"relations":[]}',
            parsed_response={"entities": [], "relations": []},
            input_token_count=10,
            output_token_count=4,
            finish_reason="stop",
        )
        async with session_factory() as db:
            assert await finalize_attempt(
                db,
                attempt_id=first_attempt_id,
                claim_token=claim_token,
                completion=completion,
            )
        async with session_factory() as db:
            assert not await finalize_attempt(
                db,
                attempt_id=first_attempt_id,
                claim_token=claim_token,
                completion=completion,
            )

        async with session_factory() as db:
            second_attempt = await create_pending_attempt(
                db,
                unit_id=unit_id,
                context_snapshot_id=snapshot_id,
                claim_token=claim_token,
                request_payload_hash="9" * 64,
                max_attempts=3,
            )
            second_attempt_id = second_attempt.id
            assert second_attempt.attempt_no == 2

        async with session_factory() as db:
            await db.execute(
                update(GraphExtractionUnit)
                .where(GraphExtractionUnit.id == unit_id)
                .values(lease_expires_at=now - timedelta(seconds=1))
            )
            await db.commit()
        async with session_factory() as db:
            assert not await finalize_attempt(
                db,
                attempt_id=second_attempt_id,
                claim_token=claim_token,
                completion=completion,
            )
        async with session_factory() as db:
            attempts = (
                await db.execute(
                    select(ExtractionRawOutputAttempt)
                    .where(ExtractionRawOutputAttempt.extraction_unit_id == unit_id)
                    .order_by(ExtractionRawOutputAttempt.attempt_no)
                )
            ).scalars().all()
            assert [value.request_status for value in attempts] == ["succeeded", "pending"]
            unit = await db.get(GraphExtractionUnit, unit_id)
            assert unit.model_attempt_count == 2
    finally:
        await engine.dispose()


def test_m2_context_and_attempt_fencing_on_postgresql(monkeypatch):
    name = "vkt_v04_m2_" + uuid.uuid4().hex[:8]
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "0021")
        asyncio.run(_seed_and_exercise(name))
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
