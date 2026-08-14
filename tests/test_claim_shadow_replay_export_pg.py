from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.models.raw_claim import GraphRawClaimOccurrence
from app.schemas.shadow_extraction import ShadowTelemetryV1
from app.schemas.claim_decision import (
    CLAIM_DECISION_ID_NAMESPACE,
    deterministic_decision_id,
)
from app.services.claim_decision_persistence import create_or_get_claim_decision
from app.services.claim_shadow_replay import load_claim_shadow_replay_artifact
from app.services.claim_shadow_replay_export import (
    ClaimShadowReplayExportError,
    ClaimShadowReplayExportRequest,
    export_claim_shadow_replay,
)
from app.services.raw_claim_persistence import create_or_get_raw_claim
from tests.test_claim_decision import _build_production
from tests.test_raw_claim_persistence_pg import _claim, _ids


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


async def _run_acceptance() -> None:
    engine = create_async_engine(
        _PG_DSN,
        isolation_level="REPEATABLE READ",
        pool_size=2,
        max_overflow=0,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = _ids()
    organization_id = uuid.uuid4()
    body = "PG raw claim evidence"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            trigger_names = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgname IN ('graph_raw_claims_immutable_guard', "
                            "'graph_raw_claim_occurrences_immutable_guard', "
                            "'graph_claim_decisions_immutable_guard')"
                        )
                    )
                ).all()
            }
            assert trigger_names == {
                "graph_raw_claims_immutable_guard",
                "graph_raw_claim_occurrences_immutable_guard",
                "graph_claim_decisions_immutable_guard",
            }

        async with sessions() as db:
            db.add(
                Organization(
                    id=organization_id,
                    slug="export-pg-" + uuid.uuid4().hex[:10],
                    name="Replay export acceptance",
                    deployment_profile="private",
                    status="active",
                )
            )
            await db.flush()
            db.add(
                Library(
                    id=ids["library"],
                    organization_id=organization_id,
                    slug="export-pg-" + uuid.uuid4().hex[:10],
                    name="Replay export acceptance",
                    embedding_model="fixture",
                    embedding_dim=3,
                    qdrant_collection="export-pg-unused",
                    lifecycle_mode="managed",
                    index_state="ready",
                )
            )
            await db.flush()
            document = Document(
                id=ids["document"],
                library_id=ids["library"],
                title="Replay export document",
                external_id="export-pg",
                content_hash=hashlib.sha256(body.encode()).hexdigest(),
                current_revision=1,
                status="ready",
            )
            db.add(document)
            await db.flush()
            db.add(
                DocumentRevision(
                    id=ids["revision"],
                    document_id=ids["document"],
                    library_id=ids["library"],
                    revision_no=1,
                    title="Replay export document",
                    content_hash=document.content_hash,
                    normalized_text=body,
                    parser_name="fixture",
                    parser_version="v1",
                    chunking_strategy="manual",
                    chunking_strategy_version="v1",
                    status="ready",
                )
            )
            ontology = OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="export-pg",
                version_no=1,
                status="active",
                origin="user",
                confirmed=True,
            )
            db.add(ontology)
            await db.flush()
            db.add(
                GraphExtractionJob(
                    id=ids["job"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    ontology_version_id=ids["ontology"],
                    trigger_type="eval",
                    execution_mode="eval",
                    status="succeeded",
                    input_fingerprint="d" * 64,
                    idempotency_key="export-job-" + uuid.uuid4().hex,
                    model_provider="fixture",
                    model_name="export-model",
                    prompt_version="v1",
                    extractor_version="v1",
                    output_parser_version="v1",
                    context_policy_version="v1",
                    extraction_policy_version="v1",
                    normalization_rule_version="v1",
                    confidence_policy_version="v1",
                    document_parser_version="v1",
                    chunking_strategy_version="v1",
                    model_config_snapshot={},
                    policy_config_snapshot={},
                    model_config_hash="a" * 64,
                    policy_config_hash="b" * 64,
                    ontology_snapshot={},
                    ontology_snapshot_hash="c" * 64,
                    prompt_content_hash="d" * 64,
                )
            )
            await db.flush()
            quote_hash = hashlib.sha256(body.encode()).hexdigest()
            db.add(
                EvidenceUnit(
                    id=ids["evidence"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_kind="chunk",
                    source_start=0,
                    source_end=len(body),
                    text_quote=body,
                    text_quote_hash=quote_hash,
                    status="active",
                )
            )
            await db.flush()
            db.add(
                Chunk(
                    id=ids["chunk"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_id=ids["evidence"],
                    seq=0,
                    chunk_kind="text",
                    text=body,
                    token_count=len(body),
                    source_start=0,
                    source_end=len(body),
                )
            )
            await db.flush()
            db.add(
                GraphExtractionUnit(
                    id=ids["unit"],
                    job_id=ids["job"],
                    library_id=ids["library"],
                    document_revision_id=ids["revision"],
                    ordinal=0,
                    center_chunk_id=ids["chunk"],
                    center_evidence_id=ids["evidence"],
                    unit_fingerprint="e" * 64,
                    status="succeeded",
                )
            )
            await db.commit()

        first_claim = _claim(ids=ids)
        second_ids = dict(ids)
        second_ids["claim"] = uuid.uuid4()
        second_ids["occurrence"] = uuid.uuid4()
        second_claim = _claim(ids=second_ids, model_name="export-rerun")
        async with sessions() as db:
            first = await create_or_get_raw_claim(db, first_claim)
            await db.commit()
            second = await create_or_get_raw_claim(db, second_claim)
            await db.commit()
            assert first.claim_created and first.occurrence_created
            assert not second.claim_created and second.occurrence_created
            decision_one = _build_production(
                first_claim,
                extraction_occurrence_id=first_claim.extraction_occurrence_id,
            )
            decision_two = _build_production(
                second_claim,
                extraction_occurrence_id=second_claim.extraction_occurrence_id,
            )
            decision_two_payload = decision_two.model_dump(mode="json")
            decision_two_payload.update(
                claim_id=str(first.claim.id),
                decision_fingerprint=None,
            )
            decision_two = type(decision_two).model_validate(decision_two_payload)
            decision_two_payload = decision_two.model_dump(mode="json")
            decision_two_payload["decision_id"] = str(
                deterministic_decision_id(
                    CLAIM_DECISION_ID_NAMESPACE,
                    decision_two.decision_fingerprint or "",
                )
            )
            decision_two = type(decision_two).model_validate(decision_two_payload)
            await create_or_get_claim_decision(db, decision_one)
            await db.commit()
            await create_or_get_claim_decision(db, decision_two)
            await db.commit()

        async with sessions() as db:
            async with db.begin():
                before_rows = (
                    await db.execute(
                        select(GraphRawClaimOccurrence.extraction_occurrence_id)
                    )
                ).all()
                bundle = await export_claim_shadow_replay(
                    db,
                    ClaimShadowReplayExportRequest(
                        library_id=ids["library"],
                        job_ids=(ids["job"],),
                    ),
                )
                after_rows = (
                    await db.execute(
                        select(GraphRawClaimOccurrence.extraction_occurrence_id)
                    )
                ).all()
                assert before_rows == after_rows
                assert len(bundle.raw_claims) == 2
                assert len(bundle.decisions) == 2
                assert {claim.claim_id for claim in bundle.raw_claims} == {first.claim.id}
                assert {
                    decision.extraction_occurrence_id for decision in bundle.decisions
                } == {
                    first_claim.extraction_occurrence_id,
                    second_claim.extraction_occurrence_id,
                }
                assert bundle.state_fingerprint_before == bundle.state_fingerprint_after
                artifact = bundle.assemble_artifact(
                    status="success",
                    artifact_id_sha256="1" * 64,
                    run_id_sha256="2" * 64,
                    provider_key_sha256="3" * 64,
                    model_key_sha256="4" * 64,
                    config_sha256="5" * 64,
                    prompt_sha256="6" * 64,
                    artifact_producer_version="m4d-test",
                    schema_producer_version="v2",
                    projection_producer_version="m3b",
                    created_at=datetime(2026, 8, 7, tzinfo=UTC),
                    completed_stages=("request", "provider", "parse", "build", "write"),
                    telemetry=ShadowTelemetryV1(
                        request_hash="7" * 64,
                        input_token_count=1,
                        output_token_count=1,
                        latency_ms=1,
                        claim_count=2,
                    ),
                )
                assert artifact.metrics is not None
                assert artifact.metrics.raw_claim_count == 2
                assert artifact.metrics.occurrence_count == 2
                assert len(artifact.raw_claims) == 1
                replay = load_claim_shadow_replay_artifact(
                    artifact.model_dump(mode="json")
                )
                assert replay.runtime_metrics is not None
                assert replay.runtime_metrics.claim_dedup_rate.value == 0.5
                with pytest.raises(
                    ClaimShadowReplayExportError,
                    match="job_scope_missing_or_cross_library",
                ):
                    await export_claim_shadow_replay(
                        db,
                        ClaimShadowReplayExportRequest(
                            library_id=ids["library"],
                            job_ids=(ids["job"],),
                            document_revision_ids=(uuid.uuid4(),),
                        ),
                    )
        read_committed_engine = create_async_engine(
            _PG_DSN,
            isolation_level="READ COMMITTED",
        )
        try:
            read_committed_sessions = async_sessionmaker(
                read_committed_engine,
                expire_on_commit=False,
            )
            async with read_committed_sessions() as db:
                async with db.begin():
                    with pytest.raises(
                        ClaimShadowReplayExportError,
                        match="repeatable_read_snapshot_required",
                    ):
                        await export_claim_shadow_replay(
                            db,
                            ClaimShadowReplayExportRequest(
                                library_id=ids["library"],
                                job_ids=(ids["job"],),
                            ),
                        )
        finally:
            await read_committed_engine.dispose()
    finally:
        await engine.dispose()


def test_claim_shadow_replay_export_pg_is_read_only_and_m4c_ready():
    asyncio.run(_run_acceptance())
