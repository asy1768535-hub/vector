from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    canonical_claim_shadow_replay_json,
    claim_shadow_replay_filename,
    scope_id_for,
    write_claim_shadow_replay_artifact,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(seed: str = "ordinary") -> dict[str, object]:
    library = _hash(f"library:{seed}")
    document = _hash(f"document:{seed}")
    revision = _hash(f"revision:{seed}")
    scope_id = scope_id_for(
        library_id_sha256=library,
        document_id_sha256=document,
        revision_id_sha256=revision,
        revision_no=1,
    )
    return {
        "scope_id": scope_id,
        "library_id_sha256": library,
        "document_id_sha256": document,
        "revision_id_sha256": revision,
        "revision_no": 1,
    }


def _artifact(*, status: str = "success", scope_seed: str = "ordinary", **updates: object) -> dict[str, object]:
    scope = _scope(scope_seed)
    claim_id = _hash("claim-1")
    occurrence_id = _hash("occurrence-1")
    decision_id = _hash("decision-1")
    raw_claim = {
        "scope_id": scope["scope_id"],
        "claim_id_sha256": claim_id,
        "content_fingerprint": _hash("content"),
        "raw_predicate_sha256": _hash("is alleged to support"),
        "source_mention_id_sha256": _hash("source-local"),
        "target_mention_id_sha256": _hash("target-local"),
        "direction": "unknown",
        "evidence_ref_count": 2,
        "evidence_ref_ids_sha256": [_hash("c0"), _hash("p1")],
        "qualifier_count": 1,
        "has_effective_time": True,
    }
    occurrence = {
        "scope_id": scope["scope_id"],
        "occurrence_id_sha256": occurrence_id,
        "claim_id_sha256": claim_id,
        "occurrence_fingerprint": _hash("occurrence"),
        "extractor_version": "shadow-builder-v1",
        "parser_version": "raw-claim-v1",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "config_version": "config-v1",
    }
    decision = {
        "scope_id": scope["scope_id"],
        "decision_id_sha256": decision_id,
        "claim_id_sha256": claim_id,
        "occurrence_id_sha256": occurrence_id,
        "decision_fingerprint": _hash("decision"),
        "decision_kind": "mapping_candidate",
        "status": "pending",
        "reason_code": "unknown_direction",
    }
    result: dict[str, object] = {
        "artifact_id_sha256": _hash("artifact"),
        "run_id_sha256": _hash("run"),
        "provider_key_sha256": _hash("provider"),
        "model_key_sha256": _hash("model"),
        "config_sha256": _hash("config"),
        "prompt_sha256": _hash("prompt"),
        "artifact_producer_version": "artifact-v1",
        "schema_producer_version": "schema-v1",
        "projection_producer_version": "projection-v1",
        "created_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
        "status": status,
        "completed_stages": ["request", "provider", "parse"],
        "scopes": [scope],
        "raw_claims": [raw_claim],
        "occurrences": [occurrence],
        "decisions": [decision],
        "aggregate_counts": [{"key": "unknown_direction", "count": 1}],
        "metrics": {
            "input_token_count": 20,
            "output_token_count": 15,
            "latency_ms": 50,
            "provider_call_count": 1,
            "raw_claim_count": 1,
            "occurrence_count": 1,
            "decision_count": 1,
        },
        "failure": None,
    }
    if status == "failed":
        result["metrics"] = None
        result["failure"] = {
            "stage": "provider",
            "error_code": "provider_timeout",
            "finish_reason": "timeout",
            "response_sha256": None,
            "observed_claim_count": 0,
            "truncated": False,
        }
    result.update(updates)
    return result


def test_success_contract_and_sensitive_surface_is_not_present() -> None:
    artifact = ClaimShadowReplayArtifactV1.model_validate(_artifact())
    serialized = canonical_claim_shadow_replay_json(artifact)
    decoded = json.loads(serialized)
    assert decoded["status"] == "success"
    assert decoded["raw_claims"][0]["direction"] == "unknown"
    forbidden = (
        "is alleged to support",
        "source-local",
        "target-local",
        "quote",
        "storage_path",
        "object_key",
        "password",
        "postgres",
        "dsn",
    )
    assert all(marker not in serialized for marker in forbidden)


def test_failed_artifact_is_real_failure_with_null_metrics() -> None:
    artifact = ClaimShadowReplayArtifactV1.model_validate(_artifact(status="failed"))
    assert artifact.status == "failed"
    assert artifact.metrics is None
    assert artifact.failure is not None
    assert artifact.failure.error_code == "provider_timeout"


def test_deterministic_serialization_is_order_independent() -> None:
    first = canonical_claim_shadow_replay_json(_artifact())
    changed = _artifact()
    changed["aggregate_counts"] = [{"count": 1, "key": "unknown_direction"}]
    assert first == canonical_claim_shadow_replay_json(changed)


def test_cross_scope_references_are_rejected() -> None:
    payload = _artifact()
    other = _scope("medical")
    payload["scopes"] = [payload["scopes"][0], other]
    payload["raw_claims"] = [{**payload["raw_claims"][0], "scope_id": other["scope_id"]}]
    with pytest.raises(ValidationError, match="occurrence crosses claim revision scope"):
        ClaimShadowReplayArtifactV1.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_sha256", "prompt text"),
        ("artifact_producer_version", "C:\\secrets\\run"),
        ("completed_stages", ["provider", "raw response"]),
    ],
)
def test_unredacted_or_unbounded_metadata_is_rejected(field: str, value: object) -> None:
    payload = _artifact()
    payload[field] = value
    with pytest.raises(ValidationError):
        ClaimShadowReplayArtifactV1.model_validate(payload)


def test_unknown_fields_and_nan_are_rejected() -> None:
    payload = _artifact()
    payload["response"] = "raw model response"
    with pytest.raises(ValidationError):
        ClaimShadowReplayArtifactV1.model_validate(payload)
    payload = _artifact()
    payload["metrics"] = {**payload["metrics"], "latency_ms": float("nan")}
    with pytest.raises(ValidationError):
        ClaimShadowReplayArtifactV1.model_validate(payload)


def test_truncation_metadata_contains_no_error_text() -> None:
    payload = _artifact(status="failed")
    payload["failure"] = {
        "stage": "parse",
        "error_code": "response_truncated",
        "finish_reason": "length",
        "response_sha256": _hash("response"),
        "observed_claim_count": 2,
        "truncated": True,
    }
    artifact = ClaimShadowReplayArtifactV1.model_validate(payload)
    serialized = canonical_claim_shadow_replay_json(artifact)
    assert "Traceback" not in serialized
    assert "response text" not in serialized
    assert artifact.failure is not None and artifact.failure.truncated is True


def test_filename_is_immutable_and_exclusive(tmp_path: Path) -> None:
    artifact = ClaimShadowReplayArtifactV1.model_validate(_artifact())
    path = write_claim_shadow_replay_artifact(tmp_path, artifact)
    assert path.name == claim_shadow_replay_filename(artifact)
    assert path.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(FileExistsError):
        write_claim_shadow_replay_artifact(tmp_path, artifact)


def test_old_graph_discovery_artifact_shape_remains_loadable() -> None:
    legacy = {
        "schema_version": "graph-discovery-eval-artifact-v2",
        "status": "failed",
        "completed_stages": ["provider"],
        "metrics": None,
    }
    assert json.loads(json.dumps(legacy, sort_keys=True))["schema_version"] == "graph-discovery-eval-artifact-v2"
