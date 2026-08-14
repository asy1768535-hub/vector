from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.claim_decision import ClaimDecisionProjectionV1, MappingCandidateProposalV1
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV2,
    canonical_claim_shadow_replay_v2_json,
    redact_raw_claim_v2,
)
from app.schemas.evidence_locator import SourceLocatorV1, TextSpanV1, sha256_text
from app.schemas.raw_claim import EvidenceLocatorClaimRefV1, RawClaimV1, TimeIntervalV1
from app.schemas.shadow_extraction import ShadowTelemetryV1
from app.services.claim_decision_builder import build_claim_decision_projection
from app.services.claim_shadow_replay_assembler import (
    ClaimShadowReplayAssemblyError,
    assemble_claim_shadow_replay_artifact,
    build_claim_shadow_replay_report,
    write_claim_shadow_replay_artifact_v2,
)
from app.services.claim_shadow_replay import load_claim_shadow_replay_artifact


NAMESPACE = uuid.UUID("e1a4b8ef-4fb4-4d8b-9ee4-8a15b3c59a41")
LIBRARY_ID = uuid.uuid5(NAMESPACE, "library")
DOCUMENT_ID = uuid.uuid5(NAMESPACE, "document")
REVISION_ID = uuid.uuid5(NAMESPACE, "revision")
EVIDENCE_ID = uuid.uuid5(NAMESPACE, "evidence")
UNIT_ID = uuid.uuid5(NAMESPACE, "unit")
HASH = hashlib.sha256(b"fixture").hexdigest()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _claim(
    *,
    claim_seed: str,
    occurrence_seed: str,
    job_seed: str,
    unit_seed: str,
    scope_seed: str = "default",
) -> RawClaimV1:
    document_id = DOCUMENT_ID if scope_seed == "default" else uuid.uuid5(NAMESPACE, f"document:{scope_seed}")
    revision_id = REVISION_ID if scope_seed == "default" else uuid.uuid5(NAMESPACE, f"revision:{scope_seed}")
    evidence_id = EVIDENCE_ID if scope_seed == "default" else uuid.uuid5(NAMESPACE, f"evidence:{scope_seed}")
    unit_id = UNIT_ID if scope_seed == "default" else uuid.uuid5(NAMESPACE, f"unit:{scope_seed}")
    job_id = uuid.uuid5(NAMESPACE, f"job:{job_seed}")
    extraction_unit_id = uuid.uuid5(NAMESPACE, f"extraction-unit:{unit_seed}")
    text = "Alpha supports Beta"
    locator = EvidenceLocatorClaimRefV1(
        locator_version="v1",
        document_id=document_id,
        document_revision_id=revision_id,
        revision_no=1,
        unit_id=unit_id,
        unit_kind="chunk",
        ordinal=0,
        quote_sha256=sha256_text(text),
        unit_text_sha256=sha256_text(text),
        source=SourceLocatorV1(kind="text", file_name="fixture.txt", text=TextSpanV1(start=0, end=len(text))),
    )
    return RawClaimV1.model_validate(
        {
            "claim_id": uuid.uuid5(NAMESPACE, f"claim:{claim_seed}"),
            "library_id": LIBRARY_ID,
            "document_id": document_id,
            "document_revision_id": revision_id,
            "revision_no": 1,
            "job_id": job_id,
            "extraction_unit_id": extraction_unit_id,
            "source_mention": {
                "local_id": "source",
                "surface": "Alpha",
                "entity_type_hint": "entity",
                "evidence_ref": "e0",
            },
            "raw_predicate": "supports",
            "target_mention": {
                "local_id": "target",
                "surface": "Beta",
                "entity_type_hint": "entity",
                "evidence_ref": "e0",
            },
            "surface_direction": "unknown",
            "negation": {"value": False, "evidence_ref": "e0"},
            "modality": {"value": "asserted", "evidence_ref": "e0"},
            "qualifiers": (),
            "valid_time": None,
            "effective_time": None,
            "evidence_refs": [
                {
                    "ref_id": "e0",
                    "evidence_id": evidence_id,
                    "library_id": LIBRARY_ID,
                    "document_id": document_id,
                    "document_revision_id": revision_id,
                    "revision_no": 1,
                    "job_id": job_id,
                    "extraction_unit_id": extraction_unit_id,
                    "unit_id": unit_id,
                    "chunk_id": unit_id,
                    "block_id": None,
                    "quote_sha256": sha256_text(text),
                    "unit_text_sha256": sha256_text(text),
                    "source_span": {"start": 0, "end": len(text)},
                    "locator": locator,
                }
            ],
            "extractor_version": "shadow-extractor-v1",
            "prompt_version": "shadow-prompt-v1",
            "model_provider": "fixture-provider",
            "model_name": "fixture-model",
            "model_config_hash": HASH,
            "prompt_content_hash": HASH,
            "parser_version": "shadow-parser-v1",
            "normalization_rule_version": "shadow-normalization-v1",
            "ontology_snapshot_hash": None,
            "extraction_occurrence_id": uuid.uuid5(NAMESPACE, f"occurrence:{occurrence_seed}"),
        }
    )


def _decision(claim: RawClaimV1):
    proposal = MappingCandidateProposalV1(
        raw_predicate=claim.raw_predicate,
        source_mention=claim.source_mention,
        target_mention=claim.target_mention,
        surface_direction=claim.surface_direction,
        evidence_ref_ids=("e0",),
        suggested_canonical_key=None,
    )
    return build_claim_decision_projection(
        claim,
        decision_kind="mapping_candidate",
        reason_code="unknown_direction",
        proposal=proposal,
        decision_version=1,
        created_by_kind="system",
        producer_key="m4c-fixture",
        producer_version="m4c-fixture-v1",
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        id_namespace=NAMESPACE,
        extraction_occurrence_id=claim.extraction_occurrence_id,
    )


def _assemble(claims: tuple[RawClaimV1, ...], decisions: tuple[object, ...] = ()):
    return assemble_claim_shadow_replay_artifact(
        status="success",
        artifact_id_sha256=_hash("artifact"),
        run_id_sha256=_hash("run"),
        provider_key_sha256=_hash("provider"),
        model_key_sha256=_hash("model"),
        config_sha256=_hash("config"),
        prompt_sha256=_hash("prompt"),
        artifact_producer_version="assembler-v1",
        schema_producer_version="raw-claim-v1",
        projection_producer_version="decision-v1",
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        completed_stages=("write", "parse", "request", "build", "provider"),
        raw_claims=claims,
        decisions=decisions,
        telemetry=ShadowTelemetryV1(
            request_hash=_hash("request"),
            response_hash=_hash("response"),
            input_token_count=10,
            output_token_count=5,
            latency_ms=25,
            finish_reason="stop",
            claim_count=len(claims),
        ),
        aggregate_counts={"shadow_write_success": len(claims)},
    )


def test_same_content_dedup_is_input_order_independent_and_remaps_links() -> None:
    low = _claim(claim_seed="a", occurrence_seed="a", job_seed="a", unit_seed="a")
    high = _claim(claim_seed="b", occurrence_seed="b", job_seed="b", unit_seed="b")
    decisions = (_decision(low), _decision(high))
    first = _assemble((high, low), decisions)
    second = _assemble((low, high), tuple(reversed(decisions)))

    assert first.raw_claims == second.raw_claims
    assert len(first.raw_claims) == 1
    assert len(first.occurrences) == 2
    assert len(first.decisions) == 2
    assert {row.occurrence_fingerprint for row in first.occurrences} == {
        low.extraction_occurrence_fingerprint,
        high.extraction_occurrence_fingerprint,
    }
    representative_id = first.raw_claims[0].claim_id_sha256
    assert {row.claim_id_sha256 for row in first.occurrences} == {representative_id}
    assert {row.claim_id_sha256 for row in first.decisions} == {representative_id}
    assert {row.decision_fingerprint for row in first.decisions} == {
        _decision(low).decision_fingerprint,
        _decision(high).decision_fingerprint,
    }
    assert first.occurrences == second.occurrences
    assert first.decisions == second.decisions
    first_report = build_claim_shadow_replay_report(first)
    second_report = build_claim_shadow_replay_report(second)
    first_metrics = first_report.structural_raw_metrics.metrics
    second_metrics = second_report.structural_raw_metrics.metrics
    assert first_metrics is not None and second_metrics is not None
    assert (first_metrics.raw_claim_count, first_metrics.unique_claim_count, first_metrics.occurrence_count) == (2, 1, 2)
    assert first_metrics.claim_dedup_rate.value == 0.5
    assert first_metrics == second_metrics

    with_direction_count = ClaimShadowReplayArtifactV2.model_validate(
        first.model_dump(mode="json")
        | {
            "aggregate_counts": [
                {"key": "unknown_direction", "count": 2},
            ]
        }
    )
    direction_metrics = build_claim_shadow_replay_report(with_direction_count).structural_raw_metrics.metrics
    assert direction_metrics is not None
    assert direction_metrics.unknown_direction_retention.value == 1

    invalid_metrics = first.model_dump(mode="json")
    invalid_metrics["metrics"]["raw_claim_count"] = 0
    with pytest.raises(ValidationError, match="raw_claim_count"):
        ClaimShadowReplayArtifactV2.model_validate(invalid_metrics)


def test_decision_scope_and_dangling_claims_fail_closed() -> None:
    claim = _claim(claim_seed="scope", occurrence_seed="scope", job_seed="scope", unit_seed="scope")
    decision = _decision(claim)
    with pytest.raises(ClaimShadowReplayAssemblyError, match="decision_contract_invalid"):
        _assemble((claim,), (decision.model_copy(update={"claim_id": uuid.uuid4()}),))

    cross_scope = decision.model_copy(update={"revision_no": 2})
    with pytest.raises(ClaimShadowReplayAssemblyError, match="decision_contract_invalid"):
        _assemble((claim,), (cross_scope,))


@pytest.mark.parametrize("forge_kind", ["model_copy", "model_construct"])
def test_decision_redaction_revalidates_forged_projection(forge_kind: str) -> None:
    claim = _claim(claim_seed="stale-decision", occurrence_seed="stale-decision", job_seed="stale-decision", unit_seed="stale-decision")
    decision = _decision(claim)
    if forge_kind == "model_copy":
        forged = decision.model_copy(update={"decision_fingerprint": _hash("stale-decision")})
    else:
        payload = decision.model_dump(mode="json")
        payload["decision_fingerprint"] = _hash("stale-decision")
        forged = ClaimDecisionProjectionV1.model_construct(**payload)

    with pytest.raises(ClaimShadowReplayAssemblyError, match="decision_contract_invalid"):
        _assemble((claim,), (forged,))


def test_multiple_revision_scopes_remain_separate() -> None:
    first = _claim(claim_seed="scope-one", occurrence_seed="scope-one", job_seed="scope-one", unit_seed="scope-one")
    second = _claim(
        claim_seed="scope-two",
        occurrence_seed="scope-two",
        job_seed="scope-two",
        unit_seed="scope-two",
        scope_seed="second",
    )
    artifact = _assemble((first, second))
    assert len(artifact.scopes) == 2
    assert len(artifact.raw_claims) == 2
    assert {row.scope_id for row in artifact.raw_claims} == {scope.scope_id for scope in artifact.scopes}


def test_success_and_failed_stage_contracts_are_enforced() -> None:
    claim = _claim(claim_seed="stage", occurrence_seed="stage", job_seed="stage", unit_seed="stage")
    with pytest.raises(ClaimShadowReplayAssemblyError, match="success_stages_incomplete"):
        assemble_claim_shadow_replay_artifact(
            status="success",
            artifact_id_sha256=_hash("artifact"),
            run_id_sha256=_hash("run"),
            provider_key_sha256=_hash("provider"),
            model_key_sha256=_hash("model"),
            config_sha256=_hash("config"),
            prompt_sha256=_hash("prompt"),
            artifact_producer_version="assembler-v1",
            schema_producer_version="raw-claim-v1",
            projection_producer_version="decision-v1",
            created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
            completed_stages=("request", "provider"),
            raw_claims=(),
            telemetry=ShadowTelemetryV1(
                request_hash=_hash("request"),
                input_token_count=1,
                output_token_count=1,
                latency_ms=1,
            ),
        )
    with pytest.raises(ClaimShadowReplayAssemblyError, match="failed_record_stages_incomplete"):
        assemble_claim_shadow_replay_artifact(
            status="failed",
            artifact_id_sha256=_hash("artifact"),
            run_id_sha256=_hash("run"),
            provider_key_sha256=_hash("provider"),
            model_key_sha256=_hash("model"),
            config_sha256=_hash("config"),
            prompt_sha256=_hash("prompt"),
            artifact_producer_version="assembler-v1",
            schema_producer_version="raw-claim-v1",
            projection_producer_version="decision-v1",
            created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
            completed_stages=("request", "provider"),
            raw_claims=(claim,),
            failure={
                "stage": "parse",
                "error_code": "response_malformed",
                "finish_reason": None,
                "response_sha256": _hash("response"),
                "observed_claim_count": 1,
                "truncated": False,
            },
        )


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_report_keeps_structural_raw_gold_and_canonical_sections_separate(domain: str) -> None:
    claim = _claim(claim_seed=domain, occurrence_seed=domain, job_seed=domain, unit_seed=domain)
    artifact = _assemble((claim,))
    redacted = redact_raw_claim_v2(claim)
    gold = {
        "fixture_id_sha256": _hash("gold-fixture"),
        "annotations": [
            {
                "gold_claim_id_sha256": _hash("gold-claim"),
                "claim": redacted.model_dump(mode="json"),
                "unknown_predicate": False,
                "unknown_source_endpoint": False,
                "unknown_target_endpoint": False,
            }
        ],
    }
    report = build_claim_shadow_replay_report(
        artifact,
        raw_gold=gold,
        canonical_metrics={"strict": {"recall": 0.75}},
    )
    assert report.structural_raw_metrics.metrics is not None
    assert report.optional_raw_gold_metrics.metrics is not None
    assert report.canonical_metrics.metrics == {"strict": {"recall": 0.75}}
    assert report.canonical_metrics.metrics != report.structural_raw_metrics.metrics.model_dump(mode="json")

    serialized = canonical_claim_shadow_replay_v2_json(artifact)
    for marker in ("Alpha supports Beta", "supports", str(LIBRARY_ID), str(REVISION_ID)):
        assert marker not in serialized


def test_assembly_revalidates_raw_claim_before_artifact_ingestion() -> None:
    claim = _claim(
        claim_seed="forged-time",
        occurrence_seed="forged-time",
        job_seed="forged-time",
        unit_seed="forged-time",
    )
    forged_interval = TimeIntervalV1.model_construct(
        start="2026-01-01",
        end=None,
        evidence_ref="e0",
    )
    forged_claim = claim.model_copy(update={"valid_time": forged_interval})

    with pytest.raises(ClaimShadowReplayAssemblyError, match="raw_claim_contract_invalid"):
        _assemble((forged_claim,))


def test_failed_report_and_missing_raw_gold_keep_unavailable_reasons() -> None:
    failed = assemble_claim_shadow_replay_artifact(
        status="failed",
        artifact_id_sha256=_hash("failed-artifact"),
        run_id_sha256=_hash("failed-run"),
        provider_key_sha256=_hash("provider"),
        model_key_sha256=_hash("model"),
        config_sha256=_hash("config"),
        prompt_sha256=_hash("prompt"),
        artifact_producer_version="assembler-v1",
        schema_producer_version="raw-claim-v1",
        projection_producer_version="decision-v1",
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        completed_stages=("request",),
        raw_claims=(),
        failure={
            "stage": "provider",
            "error_code": "provider_timeout",
            "finish_reason": "timeout",
            "response_sha256": None,
            "observed_claim_count": 0,
            "truncated": False,
        },
    )
    report = build_claim_shadow_replay_report(failed)
    assert report.structural_raw_metrics.unavailable_reason == "failed_artifact_not_scored"
    assert report.optional_raw_gold_metrics.unavailable_reason == "raw_gold_not_provided"
    assert report.failure is not None and report.failure.error_code == "provider_timeout"
    assert report.failure.response_sha256 is None
    assert report.failure.stage == "provider"


def test_v2_writer_is_exclusive_and_removes_partial_file_on_write_error(tmp_path, monkeypatch) -> None:
    claim = _claim(claim_seed="write", occurrence_seed="write", job_seed="write", unit_seed="write")
    artifact = _assemble((claim,))
    path = write_claim_shadow_replay_artifact_v2(tmp_path, artifact)
    assert path.exists()
    loaded = load_claim_shadow_replay_artifact(path)
    assert loaded.artifact == artifact
    with pytest.raises(FileExistsError):
        write_claim_shadow_replay_artifact_v2(tmp_path, artifact)

    output = tmp_path / "failure"
    monkeypatch.setattr("app.services.claim_shadow_replay_assembler.os.fsync", lambda _: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ClaimShadowReplayAssemblyError, match="artifact_write_failed"):
        write_claim_shadow_replay_artifact_v2(output, artifact.model_copy(update={"run_id_sha256": _hash("failed-write")}))
    assert list(output.glob("*.json")) == []
