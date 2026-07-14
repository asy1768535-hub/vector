from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.graph_extraction_eval import (
    GraphEvalAttemptMetric,
    GraphEvalDocument,
    GraphEvalEntityPrediction,
    GraphEvalPolicy,
    GraphEvalRelationPrediction,
    assert_sanitized_eval_artifact,
    build_metric_report,
    canonical_graph_eval_hash,
    classify,
    entity_eval_key,
    gold_entity_keys,
    gold_relation_keys,
    prediction_entity_keys,
    prediction_relation_keys,
    rate,
    relation_eval_key,
)


ROOT = Path(__file__).resolve().parents[1]


def _gold() -> GraphEvalDocument:
    payload = json.loads(
        (ROOT / "eval/graph_extraction/gold_v1.json").read_text(encoding="utf-8")
    )
    return GraphEvalDocument.model_validate(payload)


def test_gold_v1_has_the_exact_master_plan_cardinality_and_evidence_counts():
    document = _gold()
    assert len(document.units) == 6
    assert len(document.gold_entities) == 6
    assert sum(len(row.evidence_unit_keys) for row in document.gold_entities) == 11
    assert len(document.gold_relations) == 5
    assert sum(len(row.evidence_unit_keys) for row in document.gold_relations) == 5
    assert document.gold_entities[0].aliases == ("HR",)


def test_dataset_types_reject_unknown_fields_and_non_local_references():
    payload = _gold().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GraphEvalDocument.model_validate(payload)

    payload.pop("unexpected")
    payload["gold_relations"][0]["source_gold_id"] = "missing"
    with pytest.raises(ValidationError, match="local Entity"):
        GraphEvalDocument.model_validate(payload)


def test_semantic_hash_is_stable_across_mapping_order_and_unicode():
    assert canonical_graph_eval_hash({"b": 2, "a": ["中文", True]}) == (
        "947c94c472d10003231b7aaf68ec0224eb37d9ea97d11f0f3c1e4901d0de7911"
    )
    assert canonical_graph_eval_hash({"a": ["中文", True], "b": 2}) == (
        "947c94c472d10003231b7aaf68ec0224eb37d9ea97d11f0f3c1e4901d0de7911"
    )


def test_entity_key_reuses_normalization_v1_and_aliases_are_not_predictions():
    document = _gold()
    keys = gold_entity_keys((document,))
    assert entity_eval_key("gold-v1", "department", "  人力资源部  ") in keys
    assert all(key[2] != "hr" for key in keys)


def test_relation_key_preserves_directed_order_and_sorts_undirected_endpoints():
    left = entity_eval_key("gold-v1", "person", "张三")
    right = entity_eval_key("gold-v1", "department", "人力资源部")
    directed = relation_eval_key(
        "gold-v1", left, "belongs_to", right, directed=True
    )
    reversed_directed = relation_eval_key(
        "gold-v1", right, "belongs_to", left, directed=True
    )
    assert directed != reversed_directed
    assert relation_eval_key(
        "gold-v1", left, "related_to", right, directed=False
    ) == relation_eval_key(
        "gold-v1", right, "related_to", left, directed=False
    )


def test_classification_uses_unique_exact_keys_and_reports_micro_counts():
    report = classify(["a", "b", "c"], ["a", "a", "b", "d"])
    assert report.true_positive == 2
    assert report.false_positive == 1
    assert report.false_negative == 1
    assert report.precision.value == pytest.approx(2 / 3)
    assert report.recall.value == pytest.approx(2 / 3)


def test_zero_denominator_is_null_and_never_looks_perfect():
    assert rate(0, 0).value is None
    report = classify([], [])
    assert report.precision.value is None
    assert report.recall.value is None


def test_prediction_rows_include_rejected_candidates_and_exact_endpoint_types():
    entities = prediction_entity_keys(
        (
            GraphEvalEntityPrediction("gold-v1", "person", "张三"),
            GraphEvalEntityPrediction("gold-v1", "department", "人力资源部"),
        )
    )
    relations = prediction_relation_keys(
        (
            GraphEvalRelationPrediction(
                document_key="gold-v1",
                source_entity_type_key="person",
                source_canonical_name="张三",
                relation_type_key="belongs_to",
                target_entity_type_key="department",
                target_canonical_name="人力资源部",
            ),
        )
    )
    assert len(entities) == 2
    assert relations[0][1] == entities[0]
    assert relations[0][3] == entities[1]


def test_metric_report_uses_frozen_attempt_schema_evidence_and_duplicate_denominators():
    document = _gold()
    gold_entities = gold_entity_keys((document,))
    gold_relations = gold_relation_keys((document,))
    predicted_entities = gold_entities + [gold_entities[0]]
    predicted_relations = gold_relations + [gold_relations[0]]
    metrics = build_metric_report(
        gold_entities=gold_entities,
        predicted_entities=predicted_entities,
        gold_relations=gold_relations,
        predicted_relations=predicted_relations,
        attempts=(
            GraphEvalAttemptMetric("succeeded", "valid"),
            GraphEvalAttemptMetric("succeeded", "invalid_json"),
            GraphEvalAttemptMetric("timeout", None),
        ),
        relation_schema_statuses=("valid", "valid", "warning"),
        evidence_statuses=("valid", "invalid", "ambiguous", "valid"),
        cross_revision_evidence_count=1,
        eval_formal_write_count=0,
    )
    assert metrics.entity.precision.value == 1.0
    assert metrics.relation.recall.value == 1.0
    assert metrics.json_parse_rate == rate(1, 3)
    assert metrics.schema_valid_rate == rate(2, 3)
    assert metrics.invalid_evidence_rate == rate(1, 4)
    assert metrics.ambiguous_evidence_count == 1
    assert metrics.candidate_duplicate_rate == rate(2, 13)
    assert metrics.cross_revision_evidence_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "secret"},
        {"nested": {"context_text": "payload"}},
        {"provider_request_ids": ["request-1"]},
        {"safe": "Bearer definitely-secret"},
        {"safe": float("nan")},
    ],
)
def test_artifact_privacy_scanner_rejects_sensitive_or_non_finite_values(payload):
    with pytest.raises(ValueError):
        assert_sanitized_eval_artifact(payload)


def test_artifact_privacy_scanner_accepts_only_sanitized_counts_hashes_and_codes():
    assert_sanitized_eval_artifact(
        {
            "run_id": "release-v1-run-1",
            "attempt_counts": {"succeeded": 100, "timeout": 1},
            "provider_request_id_sha256": "a" * 64,
            "stable_error_code_counts": {"provider_timeout": 1},
        }
    )


def test_eval_policy_requires_protection_floors_utc_and_scoped_calibration_path():
    base = {
        "schema_version": "graph-extraction-eval-policy-v1",
        "policy_id": "eval_policy_v1",
        "dataset_manifest_sha256": "a" * 64,
        "evaluation_config_hash": "b" * 64,
        "calibration_result_path": "eval/graph_extraction/results/calibration-1.json",
        "calibration_result_sha256": "c" * 64,
        "thresholds": {
            "entity_precision": 0.85,
            "entity_recall": 0.75,
            "relation_precision": 0.85,
            "relation_recall": 0.70,
        },
        "approved_by": "release-owner",
        "approved_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
        "approval_reference": "review-1",
    }
    assert GraphEvalPolicy.model_validate(base).policy_id == "eval_policy_v1"
    base["thresholds"]["relation_recall"] = 0.69
    with pytest.raises(ValidationError):
        GraphEvalPolicy.model_validate(base)
