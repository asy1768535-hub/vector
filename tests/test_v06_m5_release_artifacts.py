from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.services.graph_retrieval_eval import (
    GraphRetrievalEvalPolicy,
    GraphRetrievalEvalReleaseEvidence,
    GraphRetrievalEvalResultArtifact,
    GraphRetrievalEvalThresholds,
    canonical_json_sha256,
    file_sha256,
    load_graph_retrieval_eval_policy,
    load_graph_retrieval_release_evidence,
    write_canonical_json_file,
)


def _thresholds():
    return GraphRetrievalEvalThresholds(
        seed_exact_accuracy=1.0,
        node_recall=1.0,
        relation_recall=1.0,
        relation_precision=1.0,
        evidence_coverage=1.0,
        determinism=1.0,
        scope_safety=1.0,
        property_privacy=1.0,
        max_property_leaks=0,
        max_membership_failures=0,
        max_p95_us={"one-hop-evidence-on": 100_000},
        max_scan_rows={"snapshot-items": 12_000},
    )


def _result_payload(*, run_id: str, phase: str, database_id: str, policy=False):
    started = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    return {
        "schema_version": "graph-retrieval-eval-result-v1",
        "run_id": run_id,
        "phase": phase,
        "status": "passed",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": (started + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "code_commit": "a" * 40,
        "implementation_tree_sha256": "b" * 64,
        "alembic_head": "0023",
        "database_id": database_id,
        "environment_fingerprint_sha256": "c" * 64,
        "dataset_id": "release-v1",
        "dataset_counts": {"entities": 40, "relations": 40, "evidence": 20, "cases": 48},
        "dataset_manifest_sha256": "d" * 64,
        "dataset_content_sha256": "e" * 64,
        "evaluation_config_sha256": "f" * 64,
        "contract_version": "v1",
        "normalization_version": "normalize_graph_name_v1",
        "response_canonicalization_version": "graph-retrieval-response-canonical-v1",
        "metrics": {
            "seed_exact_accuracy": {"numerator": 48, "denominator": 48, "value": 1.0},
            "node_recall": {"numerator": 80, "denominator": 80, "value": 1.0},
            "relation_recall": {"numerator": 60, "denominator": 60, "value": 1.0},
            "relation_precision": {"numerator": 60, "denominator": 60, "value": 1.0},
            "evidence_coverage": {"numerator": 20, "denominator": 20, "value": 1.0},
            "determinism": {"numerator": 96, "denominator": 96, "value": 1.0},
            "scope_safety": {"numerator": 8, "denominator": 8, "value": 1.0},
            "property_privacy": {"numerator": 36, "denominator": 36, "value": 1.0},
            "property_leak_count": 0,
            "publication_membership_failures": 0,
        },
        "performance": {
            "one-hop-evidence-on": {
                "sample_count": 30,
                "p50_us": 50_000,
                "p95_us": 80_000,
                "max_us": 90_000,
            }
        },
        "explain": {
            "snapshot-items": {
                "scan_rows_observed": 10_000,
                "plan_rows_observed": 20_000,
                "shared_hit_blocks": 100,
                "shared_read_blocks": 0,
                "temp_read_blocks": 0,
                "temp_written_blocks": 0,
                "plan_shape_sha256": "1" * 64,
            }
        },
        "candidate_thresholds": _thresholds().model_dump(mode="json") if phase == "calibration" else None,
        "case_counts": {"passed": 48, "failed": 0},
        "stable_error_code_counts": {},
        "case_response_hashes": [
            {"case_id": "case-001", "status_code": 200, "canonical_sha256": "2" * 64}
        ],
        "canonical_response_set_sha256": "3" * 64,
        "policy_id": "release_policy_v1" if policy else None,
        "policy_canonical_sha256": "4" * 64 if policy else None,
        "policy_file_sha256": "5" * 64 if policy else None,
        "database_cleanup_succeeded": True,
    }


def test_m5_result_schema_requires_policy_only_after_freeze_and_utc():
    calibration = GraphRetrievalEvalResultArtifact.model_validate(
        _result_payload(
            run_id="calibration-v1-01",
            phase="calibration",
            database_id="vkt_v06_m5_eval_calibration_01",
        )
    )
    assert calibration.policy_id is None

    post = GraphRetrievalEvalResultArtifact.model_validate(
        _result_payload(
            run_id="post-freeze-v1-01",
            phase="post-freeze",
            database_id="vkt_v06_m5_eval_post_01",
            policy=True,
        )
    )
    assert post.policy_id == "release_policy_v1"

    payload = _result_payload(
        run_id="bad-post",
        phase="post-freeze",
        database_id="vkt_v06_m5_eval_bad",
    )
    with pytest.raises(ValidationError, match="policy"):
        GraphRetrievalEvalResultArtifact.model_validate(payload)


def test_m5_policy_hard_quality_floors_cannot_be_lowered():
    payload = _thresholds().model_dump(mode="json")
    payload["relation_recall"] = 0.99
    with pytest.raises(ValidationError):
        GraphRetrievalEvalThresholds.model_validate(payload)


def test_m5_canonical_writer_refuses_overwrite(tmp_path):
    path = tmp_path / "artifact.json"
    write_canonical_json_file(path, {"schema_version": "test-v1", "value": 1})
    with pytest.raises(FileExistsError):
        write_canonical_json_file(path, {"schema_version": "test-v1", "value": 2})


def test_m5_policy_loader_binds_exact_calibration_and_approval_order(tmp_path):
    root = tmp_path
    result_path = root / "eval/graph_retrieval/results/calibration-v1-01.json"
    write_canonical_json_file(
        result_path,
        _result_payload(
            run_id="calibration-v1-01",
            phase="calibration",
            database_id="vkt_v06_m5_eval_calibration_01",
        ),
    )
    result_bytes = result_path.read_bytes()
    result_value = json.loads(result_bytes)
    policy = GraphRetrievalEvalPolicy(
        schema_version="graph-retrieval-release-policy-v1",
        policy_id="release_policy_v1",
        contract_version="v1",
        normalization_version="normalize_graph_name_v1",
        response_canonicalization_version="graph-retrieval-response-canonical-v1",
        implementation_tree_sha256="b" * 64,
        dataset_manifest_sha256="d" * 64,
        dataset_content_sha256="e" * 64,
        evaluation_config_sha256="f" * 64,
        environment_fingerprint_sha256="c" * 64,
        calibration={
            "path": "eval/graph_retrieval/results/calibration-v1-01.json",
            "canonical_sha256": canonical_json_sha256(result_value),
            "file_sha256": file_sha256(result_bytes),
        },
        thresholds=_thresholds(),
        properties_exposed=False,
        approved_by="release-owner",
        approved_at=datetime(2026, 7, 16, 2, tzinfo=timezone.utc),
        approval_reference="m5-gate-b-review-01",
    )
    policy_path = root / "eval/graph_retrieval/release_policy_v1.json"
    write_canonical_json_file(policy_path, policy.model_dump(mode="json"))

    loaded = load_graph_retrieval_eval_policy(
        repository_root=root,
        policy_path=policy_path,
    )
    assert loaded.policy.policy_id == "release_policy_v1"
    assert loaded.calibration.run_id == "calibration-v1-01"


def test_m5_release_evidence_schema_requires_exactly_three_distinct_runs():
    reference = {
        "path": "eval/graph_retrieval/results/post-freeze-v1-01.json",
        "canonical_sha256": "a" * 64,
        "file_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError, match="3 items"):
        GraphRetrievalEvalReleaseEvidence(
            schema_version="graph-retrieval-release-evidence-v1",
            evidence_id="release_evidence_v1",
            calibration={**reference, "path": "eval/graph_retrieval/results/calibration-v1-01.json"},
            policy={**reference, "path": "eval/graph_retrieval/release_policy_v1.json"},
            post_freeze_runs=[reference, reference],
        )


def test_m5_release_loader_rejects_missing_bundle(tmp_path):
    with pytest.raises(ValueError, match="release evidence"):
        load_graph_retrieval_release_evidence(
            repository_root=tmp_path,
            evidence_path=tmp_path / "eval/graph_retrieval/release_evidence_v1.json",
        )


def _file_reference(path, root):
    payload = path.read_bytes()
    value = json.loads(payload)
    return {
        "path": path.relative_to(root).as_posix(),
        "canonical_sha256": canonical_json_sha256(value),
        "file_sha256": file_sha256(payload),
    }


def _write_release_bundle(root, *, violating_p95=False):
    result_dir = root / "eval/graph_retrieval/results"
    calibration_path = result_dir / "calibration-v1-01.json"
    calibration_value = _result_payload(
        run_id="calibration-v1-01",
        phase="calibration",
        database_id="vkt_v06_m5_eval_calibration_01",
    )
    write_canonical_json_file(calibration_path, calibration_value)
    policy = GraphRetrievalEvalPolicy(
        schema_version="graph-retrieval-release-policy-v1",
        policy_id="release_policy_v1",
        contract_version="v1",
        normalization_version="normalize_graph_name_v1",
        response_canonicalization_version="graph-retrieval-response-canonical-v1",
        implementation_tree_sha256="b" * 64,
        dataset_manifest_sha256="d" * 64,
        dataset_content_sha256="e" * 64,
        evaluation_config_sha256="f" * 64,
        environment_fingerprint_sha256="c" * 64,
        calibration=_file_reference(calibration_path, root),
        thresholds=_thresholds(),
        properties_exposed=False,
        approved_by="release-owner",
        approved_at=datetime(2026, 7, 16, 2, tzinfo=timezone.utc),
        approval_reference="m5-gate-b-review-01",
    )
    policy_path = root / "eval/graph_retrieval/release_policy_v1.json"
    write_canonical_json_file(policy_path, policy.model_dump(mode="json"))
    policy_reference = _file_reference(policy_path, root)
    post_references = []
    for index in range(1, 4):
        value = _result_payload(
            run_id=f"post-freeze-v1-0{index}",
            phase="post-freeze",
            database_id=f"vkt_v06_m5_eval_post_0{index}",
            policy=True,
        )
        value["policy_canonical_sha256"] = policy_reference["canonical_sha256"]
        value["policy_file_sha256"] = policy_reference["file_sha256"]
        if violating_p95 and index == 3:
            value["performance"]["one-hop-evidence-on"]["p95_us"] = 100_001
            value["performance"]["one-hop-evidence-on"]["max_us"] = 100_001
        path = result_dir / f"post-freeze-v1-0{index}.json"
        write_canonical_json_file(path, value)
        post_references.append(_file_reference(path, root))
    evidence = GraphRetrievalEvalReleaseEvidence(
        schema_version="graph-retrieval-release-evidence-v1",
        evidence_id="release_evidence_v1",
        calibration=_file_reference(calibration_path, root),
        policy=policy_reference,
        post_freeze_runs=post_references,
    )
    evidence_path = root / "eval/graph_retrieval/release_evidence_v1.json"
    write_canonical_json_file(evidence_path, evidence.model_dump(mode="json"))
    return evidence_path


def test_m5_release_loader_verifies_complete_bundle_and_frozen_thresholds(tmp_path):
    evidence_path = _write_release_bundle(tmp_path)
    loaded = load_graph_retrieval_release_evidence(
        repository_root=tmp_path,
        evidence_path=evidence_path,
    )
    assert len(loaded.post_freeze_runs) == 3

    violating_root = tmp_path / "violating"
    violating_path = _write_release_bundle(violating_root, violating_p95=True)
    with pytest.raises(ValueError, match="p95 thresholds"):
        load_graph_retrieval_release_evidence(
            repository_root=violating_root,
            evidence_path=violating_path,
        )


def test_m5_policy_loader_rejects_incomplete_performance_scenario_set(tmp_path):
    calibration = _result_payload(
        run_id="calibration-v1-01",
        phase="calibration",
        database_id="vkt_v06_m5_eval_calibration_01",
    )
    calibration["performance"]["two-hop-evidence-on"] = {
        "sample_count": 30,
        "p50_us": 60_000,
        "p95_us": 90_000,
        "max_us": 95_000,
    }
    calibration["candidate_thresholds"]["max_p95_us"]["two-hop-evidence-on"] = 110_000
    calibration_path = tmp_path / "eval/graph_retrieval/results/calibration-v1-01.json"
    write_canonical_json_file(calibration_path, calibration)
    policy = GraphRetrievalEvalPolicy(
        schema_version="graph-retrieval-release-policy-v1",
        policy_id="release_policy_v1",
        contract_version="v1",
        normalization_version="normalize_graph_name_v1",
        response_canonicalization_version="graph-retrieval-response-canonical-v1",
        implementation_tree_sha256="b" * 64,
        dataset_manifest_sha256="d" * 64,
        dataset_content_sha256="e" * 64,
        evaluation_config_sha256="f" * 64,
        environment_fingerprint_sha256="c" * 64,
        calibration=_file_reference(calibration_path, tmp_path),
        thresholds=_thresholds(),
        properties_exposed=False,
        approved_by="release-owner",
        approved_at=datetime(2026, 7, 16, 2, tzinfo=timezone.utc),
        approval_reference="m5-gate-b-review-01",
    )
    policy_path = tmp_path / "eval/graph_retrieval/release_policy_v1.json"
    write_canonical_json_file(policy_path, policy.model_dump(mode="json"))
    with pytest.raises(ValueError, match="p95 scenarios"):
        load_graph_retrieval_eval_policy(
            repository_root=tmp_path,
            policy_path=policy_path,
        )
