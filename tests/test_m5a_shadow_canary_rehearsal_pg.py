from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_publication import GraphPublication
from app.models.raw_claim import GraphRawClaim
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.schemas.evidence_locator import (
    EvidenceLocatorV1,
    ParserProvenanceV1,
    SourceLocatorV1,
    TextSpanV1,
)
from app.schemas.shadow_extraction import ShadowProviderResponseV1, ShadowTelemetryV1
from app.services import graph_claim_shadow_worker as shadow_worker
from app.services.claim_shadow_replay import load_claim_shadow_replay_artifact
from app.services.claim_shadow_replay_assembler import (
    assemble_claim_shadow_replay_artifact,
    build_claim_shadow_replay_report,
    write_claim_shadow_replay_artifact_v2,
)
from app.services.claim_shadow_replay_export import (
    ClaimShadowReplayExportError,
    ClaimShadowReplayExportRequest,
    export_claim_shadow_replay,
)
from app.services.graph_claim_shadow_worker import run_shadow_after_canonical
from app.services.graph_extraction_provider import GraphExtractionProviderError
from tests.test_claim_shadow_raw_scorer import _gold, _gold_fixture


_PG_DSN = __import__("os").environ.get("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakeProvider:
    def __init__(self, mode: str = "success"):
        self.mode = mode
        self.calls = 0

    async def extract(self, _messages):
        self.calls += 1
        if self.mode == "timeout":
            raise GraphExtractionProviderError("timeout", "fake timeout", latency_ms=1)
        if self.mode == "truncation":
            return ShadowProviderResponseV1(
                content='{"claims":[]}',
                finish_reason="length",
                input_token_count=3,
                output_token_count=2,
                latency_ms=1,
            )
        if self.mode == "parser":
            return ShadowProviderResponseV1(
                content="{malformed",
                finish_reason="stop",
                input_token_count=3,
                output_token_count=2,
                latency_ms=1,
            )
        return ShadowProviderResponseV1(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "source_mention": {
                                "local_id": "source",
                                "surface": "Source",
                                "entity_type_hint": None,
                                "evidence_ref": "c0",
                            },
                            "surface_raw_predicate": "unknown predicate",
                            "target_mention": {
                                "local_id": "target",
                                "surface": "Target",
                                "entity_type_hint": None,
                                "evidence_ref": "p1",
                            },
                            "surface_direction": "unknown",
                            "negation": {"value": False, "evidence_ref": "c0"},
                            "modality": {"value": "alleged", "evidence_ref": "p1"},
                            "qualifiers": [
                                {
                                    "key": "basis",
                                    "value": "synthetic",
                                    "evidence_ref": "p1",
                                }
                            ],
                            "valid_time": {
                                "start": "2026-01-01",
                                "end": "2026-12-31",
                                "evidence_ref": "p1",
                            },
                            "effective_time": {
                                "start": "2026-01-01T00:00:00Z",
                                "evidence_ref": "p1",
                            },
                            "evidence_ref_keys": ["c0", "p1"],
                        }
                    ]
                },
                separators=(",", ":"),
            ),
            finish_reason="stop",
            input_token_count=12,
            output_token_count=28,
            latency_ms=1,
        )


async def _seed(engine, sessions):
    ids = {
        name: uuid.uuid4()
        for name in (
            "organization",
            "library",
            "document",
            "revision",
            "ontology",
            "job",
            "unit",
            "snapshot",
            "evidence",
            "evidence_previous",
            "chunk",
            "chunk_previous",
        )
    }
    center_text = "Source supports target."
    previous_text = "Previous context."
    body = previous_text + "\n" + center_text
    center_start = len(previous_text) + 1

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(
            Organization(
                id=ids["organization"],
                slug="m5a-" + uuid.uuid4().hex[:10],
                name="M5A rehearsal",
                deployment_profile="private",
                status="active",
            )
        )
        await db.flush()
        db.add(
            Library(
                id=ids["library"],
                organization_id=ids["organization"],
                slug="m5a-" + uuid.uuid4().hex[:10],
                name="M5A isolated library",
                embedding_model="fixture",
                embedding_dim=3,
                qdrant_collection="m5a-unused-" + uuid.uuid4().hex[:8],
                lifecycle_mode="managed",
                index_state="ready",
                graph_extraction_enabled=True,
                external_llm_enabled=True,
                claim_graph_shadow_policy="inherit",
            )
        )
        await db.flush()
        content_hash = _hash(body)
        db.add(
            Document(
                id=ids["document"],
                library_id=ids["library"],
                title="M5A document",
                external_id="m5a-" + uuid.uuid4().hex[:8],
                content_hash=content_hash,
                current_revision=1,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            DocumentRevision(
                id=ids["revision"],
                document_id=ids["document"],
                library_id=ids["library"],
                revision_no=1,
                title="M5A document",
                content_hash=content_hash,
                normalized_text=body,
                parser_name="fixture",
                parser_version="v1",
                chunking_strategy="manual",
                chunking_strategy_version="v1",
                status="ready",
            )
        )
        db.add(
            OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="m5a",
                version_no=1,
                status="active",
                origin="user",
                confirmed=True,
            )
        )
        await db.flush()
        db.add(
            GraphExtractionJob(
                id=ids["job"],
                library_id=ids["library"],
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                ontology_version_id=ids["ontology"],
                trigger_type="eval",
                execution_mode="production",
                status="succeeded",
                input_fingerprint="a" * 64,
                idempotency_key="m5a-" + uuid.uuid4().hex,
                model_provider="fixture",
                model_name="m5a-fake",
                prompt_version="canonical-v1",
                extractor_version="canonical-v1",
                output_parser_version="canonical-v1",
                context_policy_version="v1",
                extraction_policy_version="v1",
                normalization_rule_version="v1",
                confidence_policy_version="v1",
                document_parser_version="v1",
                chunking_strategy_version="v1",
                model_config_snapshot={},
                policy_config_snapshot={},
                model_config_hash="b" * 64,
                policy_config_hash="c" * 64,
                ontology_snapshot={},
                ontology_snapshot_hash="d" * 64,
                prompt_content_hash="e" * 64,
            )
        )
        await db.flush()
        evidence_values = (
            ("evidence", center_text, center_start, ids["chunk"]),
            ("evidence_previous", previous_text, 0, ids["chunk_previous"]),
        )
        for evidence_key, quote, start, chunk_id in evidence_values:
            evidence_id = ids[evidence_key]
            db.add(
                EvidenceUnit(
                    id=evidence_id,
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_kind="chunk",
                    source_start=start,
                    source_end=start + len(quote),
                    text_quote=quote,
                    text_quote_hash=_hash(quote),
                    status="active",
                )
            )
            locator = EvidenceLocatorV1(
                document_id=ids["document"],
                document_revision_id=ids["revision"],
                revision_no=1,
                normalized_content_hash=content_hash,
                unit_id=chunk_id,
                unit_kind="chunk",
                ordinal=0,
                parser=ParserProvenanceV1(name="fixture", version="v1"),
                source=SourceLocatorV1(
                    kind="text",
                    text=TextSpanV1(start=start, end=start + len(quote)),
                ),
                unit_text_sha256=_hash(quote),
                quote_sha256=_hash(quote),
                provenance_status="verified",
            )
            db.add(
                Chunk(
                    id=chunk_id,
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_id=evidence_id,
                    seq=0,
                    chunk_kind="text",
                    text=quote,
                    token_count=3,
                    source_start=start,
                    source_end=start + len(quote),
                    chunk_metadata={"evidence_locator_v1": locator.model_dump(mode="json")},
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
                unit_fingerprint="f" * 64,
                status="succeeded",
            )
        )
        await db.flush()
        context_json = {
            "chunks": [
                {"context_ref": "c0", "text": center_text},
                {"context_ref": "p1", "text": previous_text},
            ]
        }
        db.add(
            ExtractionContextSnapshot(
                id=ids["snapshot"],
                job_id=ids["job"],
                extraction_unit_id=ids["unit"],
                center_chunk_id=ids["chunk"],
                center_evidence_id=ids["evidence"],
                previous_chunk_ids=[str(ids["chunk_previous"])],
                next_chunk_ids=[],
                block_ids=[],
                title_path_source="none",
                ontology_snapshot_hash="d" * 64,
                context_mapping={
                    "c0": {
                        "chunk_id": str(ids["chunk"]),
                        "primary_evidence_id": str(ids["evidence"]),
                        "evidence_ids": [str(ids["evidence"])],
                    },
                    "p1": {
                        "chunk_id": str(ids["chunk_previous"]),
                        "primary_evidence_id": str(ids["evidence_previous"]),
                        "evidence_ids": [str(ids["evidence_previous"])],
                    },
                },
                context_policy_version="v1",
                context_hash="e" * 64,
                context_char_count=len(json.dumps(context_json)),
                context_json=context_json,
                context_text=json.dumps(context_json),
            )
        )
        await db.commit()
    return ids


async def _canonical_baseline(db: AsyncSession, ids: dict[str, uuid.UUID]):
    job = await db.get(GraphExtractionJob, ids["job"])
    entity_count = await db.scalar(
        select(func.count()).select_from(GraphEntityCandidate).where(
            GraphEntityCandidate.job_id == ids["job"]
        )
    )
    relation_count = await db.scalar(
        select(func.count()).select_from(GraphRelationCandidate).where(
            GraphRelationCandidate.job_id == ids["job"]
        )
    )
    publication_count = await db.scalar(
        select(func.count()).select_from(GraphPublication).where(
            GraphPublication.library_id == ids["library"]
        )
    )
    return (
        job.status,
        job.current_stage,
        dict(job.counts or {}),
        entity_count,
        relation_count,
        publication_count,
    )


async def _set_rollout(sessions, ids, *, global_enabled: bool, policy: str):
    settings.graph_claim_shadow_enabled = global_enabled
    async with sessions() as db:
        library = await db.get(Library, ids["library"])
        library.claim_graph_shadow_policy = policy
        await db.commit()


def _failed_artifact(code: str, stage: str, completed_stages: tuple[str, ...]):
    artifact = assemble_claim_shadow_replay_artifact(
        status="failed",
        artifact_id_sha256=_hash("m5a-failed-artifact:" + code),
        run_id_sha256=_hash("m5a-failed-run:" + code),
        provider_key_sha256=_hash("m5a-provider"),
        model_key_sha256=_hash("m5a-model"),
        config_sha256=_hash("m5a-config"),
        prompt_sha256=_hash("m5a-prompt"),
        artifact_producer_version="m5a-rehearsal",
        schema_producer_version="claim-shadow-replay-v2",
        projection_producer_version="claim-decision-v1",
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
        completed_stages=completed_stages,
        raw_claims=(),
        decisions=(),
        failure={"stage": stage, "error_code": code},
    )
    loaded = load_claim_shadow_replay_artifact(artifact.model_dump(mode="json"))
    report = build_claim_shadow_replay_report(artifact)
    assert loaded.runtime_metrics is None
    assert artifact.metrics is None
    assert report.structural_raw_metrics.metrics is None
    assert report.failure is not None
    assert report.failure.error_code == code
    return artifact


@pytest.mark.asyncio
async def test_m5a_fake_provider_canary_rehearsal(tmp_path: Path, monkeypatch):
    old_global = settings.graph_claim_shadow_enabled
    old_retries = settings.graph_extraction_provider_max_retries
    engine = create_async_engine(
        _PG_DSN,
        isolation_level="REPEATABLE READ",
        pool_size=4,
        max_overflow=0,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        ids = await _seed(engine, sessions)
        prepared = SimpleNamespace(
            job_id=ids["job"],
            unit_id=ids["unit"],
            library_id=ids["library"],
            context_snapshot_id=ids["snapshot"],
        )
        settings.graph_extraction_provider_max_retries = 0

        matrix = (
            (False, "inherit", "skipped", 0),
            (False, "enabled", "succeeded", 1),
            (True, "inherit", "succeeded", 1),
            (True, "disabled", "skipped", 0),
            (True, "enabled", "succeeded", 1),
        )
        for global_enabled, policy, expected_status, expected_calls in matrix:
            await _set_rollout(
                sessions,
                ids,
                global_enabled=global_enabled,
                policy=policy,
            )
            provider = _FakeProvider()
            async with sessions() as db:
                baseline = await _canonical_baseline(db, ids)
            result = await run_shadow_after_canonical(
                sessions,
                prepared=prepared,
                provider=provider,
            )
            assert result.status == expected_status
            assert provider.calls == expected_calls
            async with sessions() as db:
                assert await _canonical_baseline(db, ids) == baseline

        await _set_rollout(sessions, ids, global_enabled=True, policy="enabled")
        failure_runs = (
            ("truncation", "truncated_response"),
            ("timeout", "timeout"),
            ("parser", "malformed_response"),
        )
        for mode, reason in failure_runs:
            provider = _FakeProvider(mode)
            async with sessions() as db:
                baseline = await _canonical_baseline(db, ids)
                before_claims = await db.scalar(
                    select(func.count()).select_from(GraphRawClaim).where(
                        GraphRawClaim.library_id == ids["library"]
                    )
                )
            result = await run_shadow_after_canonical(
                sessions,
                prepared=prepared,
                provider=provider,
            )
            assert result.status == "failed"
            assert result.reason == reason
            assert provider.calls == 1
            async with sessions() as db:
                assert await _canonical_baseline(db, ids) == baseline
                assert await db.scalar(
                    select(func.count()).select_from(GraphRawClaim).where(
                        GraphRawClaim.library_id == ids["library"]
                    )
                ) == before_claims
            _failed_artifact(
                reason,
                "provider" if reason == "timeout" else "parse",
                ("request",) if reason == "timeout" else ("request", "provider"),
            )

        async def fail_decision(*_args, **_kwargs):
            raise RuntimeError("synthetic decision DB failure")

        monkeypatch.setattr(shadow_worker, "create_or_get_claim_decision", fail_decision)
        async with sessions() as db:
            baseline = await _canonical_baseline(db, ids)
        decision_failure = await run_shadow_after_canonical(
            sessions,
            prepared=prepared,
            provider=_FakeProvider(),
        )
        assert decision_failure.status == "succeeded"
        assert decision_failure.reason == "decision_persistence_failed"
        async with sessions() as db:
            assert await _canonical_baseline(db, ids) == baseline

        monkeypatch.undo()
        async with sessions() as db:
            async with db.begin():
                bundle = await export_claim_shadow_replay(
                    db,
                    ClaimShadowReplayExportRequest(
                        library_id=ids["library"],
                        job_ids=(ids["job"],),
                    ),
                )
                artifact = bundle.assemble_artifact(
                    status="success",
                    artifact_id_sha256="1" * 64,
                    run_id_sha256="2" * 64,
                    provider_key_sha256="3" * 64,
                    model_key_sha256="4" * 64,
                    config_sha256="5" * 64,
                    prompt_sha256="6" * 64,
                    artifact_producer_version="m5a-rehearsal",
                    schema_producer_version="claim-shadow-replay-v2",
                    projection_producer_version="claim-decision-v1",
                    created_at=datetime(2026, 8, 7, tzinfo=UTC),
                    completed_stages=("request", "provider", "parse", "build", "write"),
                    telemetry=ShadowTelemetryV1(
                        request_hash="7" * 64,
                        response_hash="8" * 64,
                        input_token_count=12,
                        output_token_count=28,
                        latency_ms=1,
                        finish_reason="stop",
                        claim_count=len(bundle.raw_claims),
                    ),
                    aggregate_counts={
                        "unknown_predicate": 1,
                        "unknown_endpoint": 1,
                        "unknown_direction": 1,
                        "shadow_write_success": 1,
                    },
                )
                assert artifact.provider_key_sha256 == "3" * 64
                assert artifact.model_key_sha256 == "4" * 64
                assert artifact.prompt_sha256 == "6" * 64
                assert len(artifact.raw_claims) == 1
                assert len(artifact.occurrences) == 1
                assert len(artifact.decisions) == 4
                replay = load_claim_shadow_replay_artifact(
                    artifact.model_dump(mode="json")
                )
                assert replay.runtime_metrics is not None
                assert replay.runtime_metrics.claim_dedup_rate.value == 0
                assert replay.runtime_metrics.unknown_predicate_retention.value == 1
                assert replay.runtime_metrics.unknown_direction_retention.value == 1
                gold = _gold_fixture(
                    [_gold(artifact.raw_claims[0], unknown_predicate=True)]
                )
                report = build_claim_shadow_replay_report(artifact, raw_gold=gold)
                assert report.optional_raw_gold_metrics.metrics is not None
                assert report.canonical_metrics.metrics is None
                output_path = write_claim_shadow_replay_artifact_v2(tmp_path, artifact)
                payload = output_path.read_text(encoding="utf-8")
                assert "Source supports target." not in payload
                assert "Previous context." not in payload
                assert "raw_file_sha256" not in payload

                with pytest.raises(ClaimShadowReplayExportError):
                    await export_claim_shadow_replay(
                        db,
                        ClaimShadowReplayExportRequest(
                            library_id=ids["library"],
                            job_ids=(ids["job"],),
                            document_revision_ids=(uuid.uuid4(),),
                        ),
                    )
        _failed_artifact("export_failed", "write", ("request", "provider", "parse", "build"))
    finally:
        settings.graph_claim_shadow_enabled = old_global
        settings.graph_extraction_provider_max_retries = old_retries
        await engine.dispose()
