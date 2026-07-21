from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.graph_extraction_eval import (
    GraphEvalReleaseEvidence,
    GraphEvalPolicy,
    assert_sanitized_eval_artifact,
    load_graph_eval_policy,
    load_graph_eval_release_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "eval/graph_extraction/eval_policy_v1.json"
CALIBRATION = (
    ROOT
    / "eval/graph_extraction/results/release-calibration-v1-20260715-01.json"
)
EVIDENCE = ROOT / "eval/graph_extraction/release_evidence_v1.json"


def _policy_payload() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def _temporary_policy_root(tmp_path: Path, payload: dict) -> tuple[Path, Path]:
    result_path = tmp_path / payload["calibration_result_path"]
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(CALIBRATION.read_bytes())
    policy_path = tmp_path / "eval/graph_extraction/eval_policy_v1.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return tmp_path, policy_path


def test_eval_policy_v1_binds_exact_selected_calibration_and_human_approval():
    loaded = load_graph_eval_policy(repository_root=ROOT, policy_path=POLICY)

    assert loaded.policy.thresholds.model_dump() == {
        "entity_precision": 0.85,
        "entity_recall": 0.75,
        "relation_precision": 0.85,
        "relation_recall": 0.70,
    }
    assert loaded.policy.approved_by == "Alice"
    assert loaded.policy.approved_at.isoformat() == "2026-07-15T10:30:00+00:00"
    assert loaded.policy.approval_reference == "m6-task7-approval-20260715-01"
    assert loaded.calibration.run_id == "release-calibration-v1-20260715-01"
    assert loaded.calibration.real_model_call_count == 120
    assert loaded.policy.dataset_manifest_sha256 == (
        loaded.calibration.dataset_manifest_sha256
    )
    assert loaded.policy.dataset_content_sha256 == (
        loaded.calibration.dataset_content_sha256
    )
    assert loaded.policy.evaluation_config_hash == (
        loaded.calibration.evaluation_config_hash
    )
    calibration_bytes = CALIBRATION.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    assert loaded.policy.calibration_result_sha256 == hashlib.sha256(calibration_bytes).hexdigest()
    assert_sanitized_eval_artifact(loaded.policy.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("threshold", "value"),
    [
        ("entity_precision", 0.849),
        ("entity_recall", 0.749),
        ("relation_precision", 0.849),
        ("relation_recall", 0.699),
    ],
)
def test_eval_policy_rejects_threshold_below_protection_floor(threshold, value):
    payload = _policy_payload()
    payload["thresholds"][threshold] = value
    with pytest.raises(ValidationError):
        GraphEvalPolicy.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dataset_manifest_sha256", "0" * 64, "manifest hash"),
        ("dataset_content_sha256", "0" * 64, "content hash"),
        ("evaluation_config_hash", "0" * 64, "config hash"),
        (
            "approved_at",
            "2026-07-15T02:00:00Z",
            "approval must follow",
        ),
    ],
)
def test_eval_policy_rejects_mismatched_or_premature_approval(
    tmp_path, field, value, message
):
    payload = _policy_payload()
    payload[field] = value
    root, policy_path = _temporary_policy_root(tmp_path, payload)
    with pytest.raises(ValueError, match=message):
        load_graph_eval_policy(repository_root=root, policy_path=policy_path)


def test_eval_policy_rejects_tampered_calibration_and_sensitive_metadata(tmp_path):
    payload = _policy_payload()
    payload["calibration_result_sha256"] = "0" * 64
    root, policy_path = _temporary_policy_root(tmp_path, payload)
    with pytest.raises(ValueError, match="calibration result SHA-256"):
        load_graph_eval_policy(repository_root=root, policy_path=policy_path)

    payload = _policy_payload()
    payload["approved_by"] = "sk-secret-like-policy-value"
    root, policy_path = _temporary_policy_root(tmp_path / "secret", payload)
    with pytest.raises(ValueError, match="secret-like"):
        load_graph_eval_policy(repository_root=root, policy_path=policy_path)


def test_eval_policy_schema_is_strict_and_requires_utc():
    payload = _policy_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        GraphEvalPolicy.model_validate(payload)

    payload = _policy_payload()
    payload["approved_at"] = "2026-07-15T18:30:00+08:00"
    with pytest.raises(ValidationError, match="UTC"):
        GraphEvalPolicy.model_validate(payload)


def _temporary_release_bundle(tmp_path: Path) -> tuple[Path, dict]:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    references = [
        payload["calibration"],
        payload["policy"],
        *payload["post_freeze_runs"],
    ]
    for reference in references:
        source = ROOT / reference["path"]
        target = tmp_path / reference["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    evidence_path = tmp_path / "eval/graph_extraction/release_evidence_v1.json"
    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return evidence_path, payload


def test_release_evidence_v1_binds_calibration_policy_and_three_real_runs():
    loaded = load_graph_eval_release_evidence(
        repository_root=ROOT,
        evidence_path=EVIDENCE,
    )

    assert loaded.calibration.run_id == "release-calibration-v1-20260715-01"
    assert [row.run_id for row in loaded.post_freeze_runs] == [
        "post-freeze-release-v1-20260715-01",
        "post-freeze-release-v1-20260715-02",
        "post-freeze-release-v1-20260715-03",
    ]
    assert len({row.database_name for row in loaded.post_freeze_runs}) == 3
    assert all(row.status == "passed" for row in loaded.post_freeze_runs)
    assert_sanitized_eval_artifact(loaded.evidence.model_dump(mode="json"))


def test_release_evidence_schema_rejects_duplicate_or_unscoped_references():
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    payload["post_freeze_runs"][2] = payload["post_freeze_runs"][1]
    with pytest.raises(ValidationError, match="distinct"):
        GraphEvalReleaseEvidence.model_validate(payload)

    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    payload["policy"]["path"] = "../eval_policy_v1.json"
    with pytest.raises(ValidationError, match="scoped JSON"):
        GraphEvalReleaseEvidence.model_validate(payload)


def test_release_evidence_rejects_tampered_bytes(tmp_path):
    evidence_path, payload = _temporary_release_bundle(tmp_path)
    run_path = tmp_path / payload["post_freeze_runs"][0]["path"]
    run_path.write_bytes(run_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_graph_eval_release_evidence(
            repository_root=tmp_path,
            evidence_path=evidence_path,
        )


def test_release_evidence_rejects_result_below_frozen_threshold(tmp_path):
    evidence_path, payload = _temporary_release_bundle(tmp_path)
    reference = payload["post_freeze_runs"][2]
    run_path = tmp_path / reference["path"]
    artifact = json.loads(run_path.read_text(encoding="utf-8"))
    artifact["metrics"]["relation"]["recall"] = {
        "denominator": 100,
        "numerator": 69,
        "value": 0.69,
    }
    run_path.write_text(
        json.dumps(artifact, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    reference["sha256"] = hashlib.sha256(run_path.read_bytes()).hexdigest()
    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="below a frozen threshold"):
        load_graph_eval_release_evidence(
            repository_root=tmp_path,
            evidence_path=evidence_path,
        )
