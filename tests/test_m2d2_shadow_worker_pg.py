from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.models.claim_decision import GraphClaimDecision
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.config import settings
from app.schemas.evidence_locator import (
    EvidenceLocatorV1,
    ParserProvenanceV1,
    SourceLocatorV1,
    TextSpanV1,
)
from app.schemas.shadow_extraction import ShadowProviderResponseV1
from app.services.graph_claim_shadow_worker import (
    ShadowRunSummary,
    record_shadow_statistics,
    run_shadow_after_canonical,
)
from app.services.graph_extraction_batch_eval import claim_eval_graph_extraction_batch
from app.services.graph_extraction_worker import claim_graph_extraction_unit


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _run_acceptance() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {name: uuid.uuid4() for name in (
        "organization", "library", "document", "revision", "ontology", "job",
        "unit", "snapshot", "evidence", "chunk", "evidence_neighbor", "chunk_neighbor",
    )}
    center_text = "Source supports target."
    neighbor_text = "Prior source supports target."
    body = neighbor_text + "\n" + center_text
    center_start = len(neighbor_text) + 1
    content_hash = _hash(body)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(Organization(
                id=ids["organization"],
                slug="m2d2-shadow-" + uuid.uuid4().hex[:8],
                name="M2D2 shadow PG",
                deployment_profile="private",
                status="active",
            ))
            await db.flush()
            await db.commit()
            db.add(Library(
                id=ids["library"],
                organization_id=ids["organization"],
                slug="m2d2-shadow-" + uuid.uuid4().hex[:8],
                name="M2D2 shadow",
                embedding_model="fixture",
                embedding_dim=3,
                qdrant_collection="m2d2-shadow-unused",
                lifecycle_mode="managed",
                index_state="ready",
                graph_extraction_enabled=True,
                external_llm_enabled=True,
                claim_graph_shadow_policy="enabled",
            ))
            await db.flush()
            db.add(Document(
                id=ids["document"],
                library_id=ids["library"],
                title="M2D2",
                external_id="m2d2-shadow-" + uuid.uuid4().hex[:8],
                content_hash=content_hash,
                current_revision=1,
                status="ready",
            ))
            db.add(DocumentRevision(
                id=ids["revision"],
                document_id=ids["document"],
                library_id=ids["library"],
                revision_no=1,
                title="M2D2",
                content_hash=content_hash,
                normalized_text=body,
                parser_name="fixture",
                parser_version="v1",
                chunking_strategy="manual",
                chunking_strategy_version="v1",
                status="ready",
            ))
            db.add(OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="m2d2-" + uuid.uuid4().hex[:8],
                version_no=1,
                status="active",
                origin="user",
                confirmed=True,
            ))
            await db.flush()
            db.add(GraphExtractionJob(
                id=ids["job"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                ontology_version_id=ids["ontology"],
                trigger_type="eval",
                execution_mode="production",
                status="succeeded",
                input_fingerprint="a" * 64,
                idempotency_key="m2d2-shadow-" + uuid.uuid4().hex,
                model_provider="fixture",
                model_name="fixture-model",
                prompt_version="canonical-v1",
                extractor_version="canonical-v1",
                output_parser_version="canonical-v1",
                context_policy_version="v1",
                extraction_policy_version="v1",
                normalization_rule_version="v1",
                confidence_policy_version="v1",
                document_parser_version="v1",
                chunking_strategy_version="v1",
                model_config_snapshot={
                    "context_window_tokens": 32_768,
                    "max_output_tokens": None,
                },
                policy_config_snapshot={},
                model_config_hash="b" * 64,
                policy_config_hash="c" * 64,
                ontology_snapshot={},
                ontology_snapshot_hash="d" * 64,
                prompt_content_hash="e" * 64,
            ))
            await db.flush()
            db.add(EvidenceUnit(
                id=ids["evidence"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                evidence_kind="chunk",
                source_start=center_start,
                source_end=center_start + len(center_text),
                text_quote=center_text,
                text_quote_hash=_hash(center_text),
                status="active",
            ))
            db.add(EvidenceUnit(
                id=ids["evidence_neighbor"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                evidence_kind="chunk",
                source_start=0,
                source_end=len(neighbor_text),
                text_quote=neighbor_text,
                text_quote_hash=_hash(neighbor_text),
                status="active",
            ))
            locator = EvidenceLocatorV1(
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                revision_no=1,
                normalized_content_hash=content_hash,
                unit_id=ids["chunk"],
                unit_kind="chunk",
                ordinal=0,
                parser=ParserProvenanceV1(name="fixture", version="v1"),
                source=SourceLocatorV1(
                    kind="text",
                    text=TextSpanV1(start=center_start, end=center_start + len(center_text)),
                ),
                unit_text_sha256=_hash(center_text),
                quote_sha256=_hash(center_text),
                provenance_status="verified",
            )
            db.add(Chunk(
                id=ids["chunk"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                evidence_id=ids["evidence"],
                seq=0,
                chunk_kind="text",
                text=center_text,
                token_count=4,
                source_start=center_start,
                source_end=center_start + len(center_text),
                chunk_metadata={"evidence_locator_v1": locator.model_dump(mode="json")},
            ))
            neighbor_locator = EvidenceLocatorV1(
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                revision_no=1,
                normalized_content_hash=content_hash,
                unit_id=ids["chunk_neighbor"],
                unit_kind="chunk",
                ordinal=0,
                parser=ParserProvenanceV1(name="fixture", version="v1"),
                source=SourceLocatorV1(
                    kind="text",
                    text=TextSpanV1(start=0, end=len(neighbor_text)),
                ),
                unit_text_sha256=_hash(neighbor_text),
                quote_sha256=_hash(neighbor_text),
                provenance_status="verified",
            )
            db.add(Chunk(
                id=ids["chunk_neighbor"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                evidence_id=ids["evidence_neighbor"],
                seq=0,
                chunk_kind="text",
                text=neighbor_text,
                token_count=5,
                source_start=0,
                source_end=len(neighbor_text),
                chunk_metadata={"evidence_locator_v1": neighbor_locator.model_dump(mode="json")},
            ))
            db.add(GraphExtractionUnit(
                id=ids["unit"],
                job_id=ids["job"],
                library_id=ids["library"],
                document_revision_id=ids["revision"],
                ordinal=0,
                center_chunk_id=ids["chunk"],
                center_evidence_id=ids["evidence"],
                unit_fingerprint="f" * 64,
                status="succeeded",
            ))
            await db.flush()
            snapshot_json = {"chunks": [
                {"context_ref": "c0", "text": center_text},
                {"context_ref": "p1", "text": neighbor_text},
            ]}
            db.add(ExtractionContextSnapshot(
                id=ids["snapshot"],
                job_id=ids["job"],
                extraction_unit_id=ids["unit"],
                center_chunk_id=ids["chunk"],
                center_evidence_id=ids["evidence"],
                previous_chunk_ids=[],
                next_chunk_ids=[],
                block_ids=[],
                title_path_source="none",
                ontology_snapshot_hash="d" * 64,
                context_mapping={
                    "c0": {
                        "chunk_id": str(ids["chunk"]),
                        "primary_evidence_id": str(ids["evidence"]),
                        "evidence_ids": [
                            str(ids["evidence"]), str(ids["evidence_neighbor"])
                        ],
                        "role": "current",
                    },
                    "p1": {
                        "chunk_id": str(ids["chunk_neighbor"]),
                        "primary_evidence_id": str(ids["evidence_neighbor"]),
                        "evidence_ids": [str(ids["evidence_neighbor"])],
                        "role": "neighbor",
                    },
                },
                context_policy_version="v1",
                context_hash="e" * 64,
                context_char_count=len(json.dumps(snapshot_json)),
                context_json=snapshot_json,
                context_text=json.dumps(snapshot_json),
            ))
            await db.commit()

        async with sessions() as db:
            entity_candidates_before = len(
                (
                    await db.execute(
                        select(GraphEntityCandidate).where(
                            GraphEntityCandidate.job_id == ids["job"]
                        )
                    )
                ).scalars().all()
            )
            relation_candidates_before = len(
                (
                    await db.execute(
                        select(GraphRelationCandidate).where(
                            GraphRelationCandidate.job_id == ids["job"]
                        )
                    )
                ).scalars().all()
            )

        class FakeProvider:
            def __init__(self):
                self.output_budget = None

            def with_output_budget(self, output_budget):
                self.output_budget = output_budget
                return self

            async def extract(self, _messages):
                return ShadowProviderResponseV1(
                    content=(
                        '{"claims":[{"source_mention":{"local_id":"s",'
                        '"surface":"Source","evidence_ref":"c0"},'
                        '"surface_raw_predicate":"supports",'
                        '"target_mention":{"local_id":"t",'
                        '"surface":"Prior source","evidence_ref":"p1"},'
                        '"surface_direction":"source_to_target",'
                        '"negation":{"value":false,"evidence_ref":"c0"},'
                        '"modality":{"value":"asserted","evidence_ref":"c0"},'
                        '"qualifiers":[],"valid_time":null,"effective_time":null,'
                        '"evidence_ref_keys":["c0","p1"]}]}'
                    ),
                    finish_reason="stop",
                    input_token_count=5,
                    output_token_count=12,
                    latency_ms=1,
                )

        prepared = SimpleNamespace(
            job_id=ids["job"],
            unit_id=ids["unit"],
            library_id=ids["library"],
            context_snapshot_id=ids["snapshot"],
        )
        provider = FakeProvider()
        result = await run_shadow_after_canonical(
            sessions,
            prepared=prepared,
            provider=provider,
        )
        assert result.status == "succeeded"
        assert provider.output_budget == 8_000
        async with sessions() as db:
            claim_rows = (
                await db.execute(
                    select(GraphRawClaim).where(GraphRawClaim.library_id == ids["library"])
                )
            ).scalars().all()
            assert len(claim_rows) == 1
            assert claim_rows[0].raw_predicate == "supports"
            assert len(claim_rows[0].evidence_refs) == 2
            occurrence_rows = (
                await db.execute(
                    select(GraphRawClaimOccurrence).where(
                        GraphRawClaimOccurrence.claim_id == claim_rows[0].id
                    )
                )
            ).scalars().all()
            assert len(occurrence_rows) == 1
            assert {item["ref_id"] for item in occurrence_rows[0].evidence_refs} == {"c0", "p1"}
            decision_rows = (
                await db.execute(
                    select(GraphClaimDecision).where(
                        GraphClaimDecision.claim_id == claim_rows[0].id
                    )
                )
            ).scalars().all()
            assert len(decision_rows) == 2
            assert {row.reason_code for row in decision_rows} == {
                "unknown_source_type",
                "unknown_target_type",
            }
            job = await db.get(GraphExtractionJob, ids["job"])
            shadow_stats = job.statistics["claim_shadow"]
            assert shadow_stats["attempted"] == 1
            assert shadow_stats["succeeded"] == 1
            assert shadow_stats["failed"] == 0
            assert shadow_stats["skipped"] == 0
            assert shadow_stats["claim_core_created"] == 1
            assert shadow_stats["claim_core_reused"] == 0
            assert shadow_stats["occurrence_created"] == 1
            assert shadow_stats["occurrence_reused"] == 0
            assert shadow_stats["decision_created"] == 2
            assert shadow_stats["decision_reused"] == 0
            assert len(
                (
                    await db.execute(
                        select(GraphEntityCandidate).where(
                            GraphEntityCandidate.job_id == ids["job"]
                        )
                    )
                ).scalars().all()
            ) == entity_candidates_before
            assert len(
                (
                    await db.execute(
                        select(GraphRelationCandidate).where(
                            GraphRelationCandidate.job_id == ids["job"]
                        )
                    )
                ).scalars().all()
            ) == relation_candidates_before

        retry = await run_shadow_after_canonical(
            sessions,
            prepared=prepared,
            provider=FakeProvider(),
        )
        assert retry.status == "succeeded"
        async with sessions() as db:
            decision_rows = (
                await db.execute(
                    select(GraphClaimDecision).where(
                        GraphClaimDecision.claim_id == claim_rows[0].id
                    )
                )
            ).scalars().all()
            assert len(decision_rows) == 2
            job = await db.get(GraphExtractionJob, ids["job"])
            shadow_stats = job.statistics["claim_shadow"]
            assert shadow_stats["attempted"] == 2
            assert shadow_stats["succeeded"] == 2
            assert shadow_stats["claim_core_created"] == 1
            assert shadow_stats["claim_core_reused"] == 1
            assert shadow_stats["occurrence_created"] == 1
            assert shadow_stats["occurrence_reused"] == 1
            assert shadow_stats["decision_created"] == 2
            assert shadow_stats["decision_reused"] == 2

        class SchemaTypeProvider:
            async def extract(self, _messages):
                return ShadowProviderResponseV1(
                    content=(
                        '{"claims":[{"source_mention":"Sensitive Asset Name",'
                        '"surface_raw_predicate":"supports",'
                        '"target_mention":{"local_id":"t","surface":"Prior source",'
                        '"evidence_ref":"p1"},'
                        '"surface_direction":"source_to_target",'
                        '"negation":{"value":false,"evidence_ref":"c0"},'
                        '"modality":{"value":"asserted","evidence_ref":"c0"},'
                        '"qualifiers":[],"valid_time":null,"effective_time":null,'
                        '"evidence_ref_keys":["c0","p1"]}]}'
                    ),
                    finish_reason="stop",
                    input_token_count=404,
                    output_token_count=971,
                    latency_ms=9_170,
                )

        malformed = await run_shadow_after_canonical(
            sessions,
            prepared=prepared,
            provider=SchemaTypeProvider(),
        )
        assert malformed.status == "failed"
        assert malformed.reason == "malformed_response"
        assert malformed.parse_category == "schema_type"
        assert malformed.validation_error_count == 1
        async with sessions() as db:
            job = await db.get(GraphExtractionJob, ids["job"])
            shadow_stats = job.statistics["claim_shadow"]
            assert shadow_stats["attempted"] == 3
            assert shadow_stats["succeeded"] == 2
            assert shadow_stats["failed"] == 1
            assert shadow_stats["validation_error_count"] == 1
            assert shadow_stats["validation_error_path_type_counts"] == {
                "claims.0.source_mention|model_type": 1
            }
            assert "Sensitive Asset Name" not in json.dumps(shadow_stats)
            assert len(
                (
                    await db.execute(
                        select(GraphRawClaim).where(
                            GraphRawClaim.library_id == ids["library"]
                        )
                    )
                ).scalars().all()
            ) == 1
    finally:
        await engine.dispose()


def test_m2d2_shadow_worker_pg_persists_with_fake_provider():
    asyncio.run(_run_acceptance())


async def _run_batch_routing_matrix() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            organization_id = uuid.uuid4()
            db.add(Organization(
                id=organization_id,
                slug="m2d2-batch-" + uuid.uuid4().hex[:8],
                name="M2D2 batch matrix",
                deployment_profile="private",
                status="active",
            ))
            await db.flush()
            await db.commit()

        cases = (
            ("global_false_inherit", False, "inherit", True, True, True),
            ("global_false_enabled", False, "enabled", True, True, False),
            ("global_true_inherit", True, "inherit", True, True, False),
            ("global_true_disabled", True, "disabled", True, True, True),
            ("graph_disabled", True, "inherit", False, True, True),
            ("llm_disabled", True, "inherit", True, False, True),
        )
        original_global = settings.graph_claim_shadow_enabled
        try:
            for name, global_enabled, policy, graph_enabled, llm_enabled, expected in cases:
                ids = {key: uuid.uuid4() for key in (
                    "library", "document", "revision", "ontology", "job", "unit",
                    "evidence", "chunk",
                )}
                body = f"{name} source text"
                digest = _hash(body)
                async with sessions() as db:
                    db.add(Library(
                        id=ids["library"],
                        organization_id=organization_id,
                        slug="m2d2-batch-" + uuid.uuid4().hex[:8],
                        name=name,
                        embedding_model="fixture",
                        embedding_dim=3,
                        qdrant_collection="m2d2-batch-unused-" + uuid.uuid4().hex[:8],
                        lifecycle_mode="managed",
                        index_state="ready",
                        graph_extraction_enabled=graph_enabled,
                        external_llm_enabled=llm_enabled,
                        claim_graph_shadow_policy=policy,
                    ))
                    await db.flush()
                    db.add(Document(
                        id=ids["document"], library_id=ids["library"],
                        title=name, external_id=name + "-" + uuid.uuid4().hex[:8],
                        content_hash=digest, current_revision=1, status="ready",
                    ))
                    db.add(DocumentRevision(
                        id=ids["revision"], document_id=ids["document"],
                        library_id=ids["library"], revision_no=1, title=name,
                        content_hash=digest, normalized_text=body,
                        parser_name="fixture", parser_version="v1",
                        chunking_strategy="manual", chunking_strategy_version="v1",
                        status="ready",
                    ))
                    db.add(OntologyVersion(
                        id=ids["ontology"], library_id=ids["library"],
                        version_key=name + "-" + uuid.uuid4().hex[:8], version_no=1,
                        status="active", origin="user", confirmed=True,
                    ))
                    await db.flush()
                    db.add(GraphExtractionJob(
                        id=ids["job"], library_id=ids["library"],
                        document_id=ids["document"], document_revision_id=ids["revision"],
                        ontology_version_id=ids["ontology"], trigger_type="eval",
                        execution_mode="eval", status="queued",
                        input_fingerprint=digest, idempotency_key=name + "-" + uuid.uuid4().hex,
                        model_provider="fixture", model_name="fixture-model",
                        prompt_version="v1", extractor_version="v1",
                        output_parser_version="v1", context_policy_version="v1",
                        extraction_policy_version="v1", normalization_rule_version="v1",
                        confidence_policy_version="v1", document_parser_version="v1",
                        chunking_strategy_version="v1", model_config_snapshot={"batch_size": 2},
                        policy_config_snapshot={}, model_config_hash="a" * 64,
                        policy_config_hash="b" * 64, ontology_snapshot={},
                        ontology_snapshot_hash="c" * 64, prompt_content_hash="d" * 64,
                    ))
                    await db.flush()
                    db.add(EvidenceUnit(
                        id=ids["evidence"], library_id=ids["library"],
                        document_id=ids["document"], document_revision_id=ids["revision"],
                        evidence_kind="chunk", text_quote=body, text_quote_hash=digest,
                        source_start=0, source_end=len(body), status="active",
                    ))
                    db.add(Chunk(
                        id=ids["chunk"], library_id=ids["library"],
                        document_id=ids["document"], document_revision_id=ids["revision"],
                        evidence_id=ids["evidence"], seq=0, chunk_kind="text",
                        text=body, token_count=3, source_start=0, source_end=len(body),
                    ))
                    db.add(GraphExtractionUnit(
                        id=ids["unit"], job_id=ids["job"], library_id=ids["library"],
                        document_revision_id=ids["revision"], ordinal=0,
                        center_chunk_id=ids["chunk"], center_evidence_id=ids["evidence"],
                        unit_fingerprint="e" * 64, status="queued",
                    ))
                    await db.commit()

                settings.graph_claim_shadow_enabled = global_enabled
                async with sessions() as db:
                    claimed = await claim_eval_graph_extraction_batch(
                        db,
                        worker_id="m2d2-batch-test",
                        batch_size=2,
                        lease_seconds=60,
                        production_only=False,
                        max_attempts=3,
                    )
                assert bool(claimed) is expected, name
                async with sessions() as db:
                    single_claim = await claim_graph_extraction_unit(
                        db,
                        worker_id="m2d2-single-test",
                        lease_seconds=60,
                        max_attempts=3,
                    )
                if expected:
                    assert single_claim is None, name
                else:
                    assert single_claim is not None, name
                async with sessions() as db:
                    job = await db.get(GraphExtractionJob, ids["job"])
                    if job is not None:
                        job.status = "failed"
                        await db.commit()
        finally:
            settings.graph_claim_shadow_enabled = original_global
    finally:
        await engine.dispose()


def test_m2d2_shadow_worker_pg_batch_claimant_matrix():
    asyncio.run(_run_batch_routing_matrix())


async def _run_concurrent_statistics() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            job = (
                await db.execute(select(GraphExtractionJob).order_by(GraphExtractionJob.created_at))
            ).scalars().first()
            assert job is not None
            job.statistics = {}
            job_id = job.id
            await db.commit()

        await asyncio.gather(
            record_shadow_statistics(
                sessions,
                job_id=job_id,
                summary=ShadowRunSummary(
                    status="succeeded", claim_core_created=1, occurrence_created=1,
                    reason="accepted",
                ),
                attempted=1,
            ),
            record_shadow_statistics(
                sessions,
                job_id=job_id,
                summary=ShadowRunSummary(status="failed", reason="provider_error"),
                attempted=1,
            ),
        )
        async with sessions() as db:
            job = await db.get(GraphExtractionJob, job_id)
            shadow = job.statistics["claim_shadow"]
            assert shadow["attempted"] == 2
            assert shadow["succeeded"] == 1
            assert shadow["failed"] == 1
            assert shadow["claim_core_created"] == 1
            assert shadow["occurrence_created"] == 1
            assert shadow["reason_counts"] == {"accepted": 1, "provider_error": 1}
    finally:
        await engine.dispose()


def test_m2d2_shadow_worker_pg_statistics_row_lock():
    asyncio.run(_run_concurrent_statistics())
