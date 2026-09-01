from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    canonical_claim_shadow_replay_json,
    claim_shadow_replay_filename,
    scope_id_for,
)
from app.services.claim_shadow_replay import (
    ClaimShadowReplayLoadError,
    load_claim_shadow_replay_artifact,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(seed: str, revision_no: int) -> dict[str, object]:
    library = _hash(f"library:{seed}")
    document = _hash(f"document:{seed}")
    revision = _hash(f"revision:{seed}:{revision_no}")
    return {
        "scope_id": scope_id_for(
            library_id_sha256=library,
            document_id_sha256=document,
            revision_id_sha256=revision,
            revision_no=revision_no,
        ),
        "library_id_sha256": library,
        "document_id_sha256": document,
        "revision_id_sha256": revision,
        "revision_no": revision_no,
    }


def _artifact_payload(*, domain: str = "asset", empty: bool = False) -> dict[str, object]:
    scope_one = _scope(domain, 1)
    scope_two = _scope(domain, 2)
    scopes = [scope_one, scope_two]
    claims: list[dict[str, object]] = []
    occurrences: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    if not empty:
        for ordinal, scope in enumerate(scopes):
            claim_id = _hash(f"{domain}:claim:{ordinal}")
            occurrence_id = _hash(f"{domain}:occurrence:{ordinal}")
            claims.append(
                {
                    "scope_id": scope["scope_id"],
                    "claim_id_sha256": claim_id,
                    "content_fingerprint": _hash(f"same-content:{ordinal % 2}"),
                    "raw_predicate_sha256": _hash("surface relation"),
                    "source_mention_id_sha256": _hash(f"source:{ordinal}"),
                    "target_mention_id_sha256": _hash(f"target:{ordinal}"),
                    "direction": "unknown" if ordinal == 0 else "source_to_target",
                    "evidence_ref_count": 1,
                    "evidence_ref_ids_sha256": [_hash(f"evidence:{ordinal}")],
                    "qualifier_count": ordinal,
                    "has_effective_time": bool(ordinal),
                }
            )
            occurrences.append(
                {
                    "scope_id": scope["scope_id"],
                    "occurrence_id_sha256": occurrence_id,
                    "claim_id_sha256": claim_id,
                    "occurrence_fingerprint": _hash(f"occurrence:{ordinal}"),
                    "extractor_version": "shadow-builder-v1",
                    "parser_version": "raw-claim-v1",
                    "model_version": f"model-v{ordinal + 1}",
                    "prompt_version": "prompt-v1",
                    "config_version": "config-v1",
                }
            )
            decisions.append(
                {
                    "scope_id": scope["scope_id"],
                    "decision_id_sha256": _hash(f"decision:{ordinal}"),
                    "claim_id_sha256": claim_id,
                    "occurrence_id_sha256": occurrence_id,
                    "decision_fingerprint": _hash(f"decision-fingerprint:{ordinal}"),
                    "decision_kind": "mapping_candidate" if ordinal == 0 else "schema_extension_candidate",
                    "status": "pending",
                    "reason_code": "unknown_direction" if ordinal == 0 else "unknown_source_type",
                }
            )
    return {
        "artifact_id_sha256": _hash(f"{domain}:artifact"),
        "run_id_sha256": _hash(f"{domain}:run"),
        "provider_key_sha256": _hash("provider"),
        "model_key_sha256": _hash("model"),
        "config_sha256": _hash("config"),
        "prompt_sha256": _hash("prompt"),
        "artifact_producer_version": "artifact-v1",
        "schema_producer_version": "raw-claim-v1",
        "projection_producer_version": "decision-v1",
        "created_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
        "status": "success",
        "completed_stages": ["request", "provider", "parse", "write"],
        "scopes": scopes,
        "raw_claims": claims,
        "occurrences": occurrences,
        "decisions": decisions,
        "aggregate_counts": [
            {"key": "unknown_predicate", "count": 1},
            {"key": "unknown_direction", "count": 1},
            {"key": "shadow_write_success", "count": 2},
            {"key": "shadow_write_failure", "count": 0},
        ],
        "metrics": {
            "input_token_count": 100,
            "output_token_count": 30,
            "latency_ms": 250,
            "provider_call_count": 2,
            "raw_claim_count": len(claims),
            "occurrence_count": len(occurrences),
            "decision_count": len(decisions),
        },
        "failure": None,
    }


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_loader_computes_self_contained_metrics_without_domain_rules(domain: str) -> None:
    result = load_claim_shadow_replay_artifact(_artifact_payload(domain=domain))
    assert result.failure is None
    assert result.runtime_metrics is not None
    metrics = result.runtime_metrics
    assert metrics.raw_claim_count == 2
    assert metrics.unique_claim_count == 2
    assert metrics.claim_dedup_rate.value == 0
    assert metrics.unknown_predicate_retention.value == 0.5
    assert metrics.unknown_direction_retention.value == 0.5
    assert metrics.unknown_endpoint_retention.value is None
    assert metrics.unknown_endpoint_retention.unavailable_reason == "aggregate_count_unavailable"
    assert metrics.shadow_write_success_count.value == 2
    assert metrics.shadow_write_failure_count.value == 0
    assert metrics.input_token_total.value == 100
    assert metrics.output_token_total.value == 30
    assert metrics.latency_p50_ms.value == 250
    assert metrics.latency_p95_ms.value == 250
    assert [row.revision_no for row in metrics.scope_metrics] == [1, 2]
    assert metrics.canonical_metrics_status == "absent"


def test_metrics_are_order_independent_for_claim_occurrence_decision_sets() -> None:
    payload = _artifact_payload()
    result_one = load_claim_shadow_replay_artifact(payload).runtime_metrics
    assert result_one == load_claim_shadow_replay_artifact(payload).runtime_metrics
    payload["raw_claims"] = list(reversed(payload["raw_claims"]))
    payload["occurrences"] = list(reversed(payload["occurrences"]))
    payload["decisions"] = list(reversed(payload["decisions"]))
    result_two = load_claim_shadow_replay_artifact(payload).runtime_metrics
    assert result_one == result_two


def test_historical_v1_metrics_semantics_remain_accepted() -> None:
    payload = _artifact_payload()
    payload["metrics"] = {
        **payload["metrics"],
        "raw_claim_count": 0,
        "occurrence_count": 0,
    }
    result = load_claim_shadow_replay_artifact(payload)
    assert result.runtime_metrics is not None
    assert result.runtime_metrics.raw_claim_count == 2
    assert result.runtime_metrics.unique_claim_count == 2
    assert result.runtime_metrics.claim_dedup_rate.value == 0


def test_zero_claim_success_is_unavailable_not_zero() -> None:
    result = load_claim_shadow_replay_artifact(_artifact_payload(empty=True)).runtime_metrics
    assert result is not None
    assert result.raw_claim_count == 0
    assert result.unique_claim_count == 0
    assert result.claim_dedup_rate.value is None
    assert result.claim_dedup_rate.unavailable_reason == "no_claim_records"
    assert result.unknown_direction_retention.value is None
    assert result.unknown_direction_retention.unavailable_reason == "no_claim_records"


def test_no_provider_sample_does_not_report_zero_latency_or_tokens() -> None:
    payload = _artifact_payload(empty=True)
    payload["metrics"] = {
        "input_token_count": 0,
        "output_token_count": 0,
        "latency_ms": 0,
        "provider_call_count": 0,
        "raw_claim_count": 0,
        "occurrence_count": 0,
        "decision_count": 0,
    }
    result = load_claim_shadow_replay_artifact(payload).runtime_metrics
    assert result is not None
    assert result.latency_p50_ms.unavailable_reason == "no_provider_samples"
    assert result.input_token_total.unavailable_reason == "no_provider_samples"


def test_failed_artifact_returns_diagnostics_without_runtime_metrics() -> None:
    payload = _artifact_payload()
    payload["status"] = "failed"
    payload["metrics"] = None
    payload["failure"] = {
        "stage": "provider",
        "error_code": "provider_timeout",
        "finish_reason": "timeout",
        "response_sha256": None,
        "observed_claim_count": 0,
        "truncated": False,
    }
    result = load_claim_shadow_replay_artifact(payload)
    assert result.runtime_metrics is None
    assert result.failure is not None
    assert result.failure.error_code == "provider_timeout"


def test_loader_rejects_digest_tamper_unknown_field_cross_scope_and_noncanonical() -> None:
    artifact = ClaimShadowReplayArtifactV1.model_validate(_artifact_payload())
    payload = (canonical_claim_shadow_replay_json(artifact) + "\n").encode()
    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_digest_mismatch"):
        load_claim_shadow_replay_artifact(payload, expected_sha256=_hash("different"))

    unknown = json.loads(payload)
    unknown["raw_response"] = "must not be accepted"
    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_contract_invalid"):
        load_claim_shadow_replay_artifact(unknown)

    crossed = _artifact_payload()
    crossed["occurrences"] = [
        {**crossed["occurrences"][0], "scope_id": crossed["scopes"][1]["scope_id"]}
    ]
    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_contract_invalid"):
        load_claim_shadow_replay_artifact(crossed)

    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_not_canonical"):
        load_claim_shadow_replay_artifact(payload + b" ")


def test_path_loader_requires_immutable_filename_and_does_not_follow_artifact_paths(tmp_path: Path) -> None:
    artifact = ClaimShadowReplayArtifactV1.model_validate(_artifact_payload())
    path = tmp_path / claim_shadow_replay_filename(artifact)
    path.write_bytes((canonical_claim_shadow_replay_json(artifact) + "\n").encode())
    result = load_claim_shadow_replay_artifact(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert result.artifact.schema_version == "claim_shadow_replay_artifact_v1"
    assert not hasattr(result.artifact, "path")


def test_old_artifact_loader_regression_remains_separate() -> None:
    legacy = {
        "schema_version": "graph-discovery-eval-artifact-v2",
        "status": "failed",
        "completed_stages": ["provider"],
        "metrics": None,
    }
    assert json.loads(json.dumps(legacy))["schema_version"] == "graph-discovery-eval-artifact-v2"
