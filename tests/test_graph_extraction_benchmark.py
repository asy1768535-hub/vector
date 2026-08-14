from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.graph_extraction_benchmark import (
    GraphBenchmarkThresholds,
    compare_graph_eval_runs,
    load_graph_eval_run,
)
from app.services.graph_extraction_eval import (
    GraphEvalAttemptPerformance,
    GraphEvalClassification,
    GraphEvalDatasetCounts,
    GraphEvalMetricReport,
    GraphEvalPerformanceReport,
    GraphEvalRate,
    GraphEvalRunArtifact,
    build_performance_report,
)


ROOT = Path(__file__).resolve().parents[1]


def _rate(value: float = 1.0) -> GraphEvalRate:
    numerator = round(value * 100)
    return GraphEvalRate(numerator=numerator, denominator=100, value=numerator / 100)


def _classification(precision: float = 1.0, recall: float = 1.0):
    return GraphEvalClassification(
        true_positive=1,
        false_positive=0,
        false_negative=0,
        precision=_rate(precision),
        recall=_rate(recall),
    )


def _artifact(
    run_id: str,
    *,
    wall_time_ms: int,
    model_attempts: int,
    input_tokens: int,
    entity_score: float = 1.0,
    relation_score: float = 1.0,
) -> GraphEvalRunArtifact:
    started_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
    finished_at = started_at + timedelta(milliseconds=wall_time_ms)
    metrics = GraphEvalMetricReport(
        entity=_classification(entity_score, entity_score),
        relation=_classification(relation_score, relation_score),
        json_parse_rate=_rate(),
        schema_valid_rate=_rate(),
        invalid_evidence_rate=GraphEvalRate(
            numerator=0, denominator=10, value=0.0
        ),
        ambiguous_evidence_count=0,
        candidate_duplicate_rate=GraphEvalRate(
            numerator=0, denominator=10, value=0.0
        ),
        cross_revision_evidence_count=0,
        eval_formal_write_count=0,
    )
    performance = GraphEvalPerformanceReport(
        wall_time_ms=wall_time_ms,
        unit_count=208,
        succeeded_unit_count=208,
        units_per_minute=208 / (wall_time_ms / 60_000),
        model_attempt_count=model_attempts,
        retry_count=0,
        truncated_attempt_count=0,
        input_token_count=input_tokens,
        output_token_count=100_000,
        token_usage_attempt_count=model_attempts,
        token_usage_coverage=GraphEvalRate(
            numerator=model_attempts,
            denominator=model_attempts,
            value=1.0,
        ),
        provider_latency_ms={
            "count": model_attempts,
            "min_ms": 1,
            "p50_ms": 2,
            "p95_ms": 3,
            "max_ms": 4,
            "mean_ms": 2.5,
        },
    )
    return GraphEvalRunArtifact(
        schema_version="graph-extraction-eval-result-v1",
        run_id=run_id,
        phase="mock",
        status="passed",
        real_provider=False,
        started_at=started_at,
        finished_at=finished_at,
        code_commit="a" * 40,
        alembic_head="0022",
        database_name="vkt_m6_eval_benchmark_1",
        dataset_id="benchmark-v1",
        dataset_counts=GraphEvalDatasetCounts(
            documents=6, units=208, entities=1, relations=1
        ),
        dataset_manifest_sha256="b" * 64,
        dataset_content_sha256="c" * 64,
        evaluation_config_hash="d" * 64,
        model_provider="mock",
        model_name="mock",
        component_versions={"prompt_version": "v1"},
        job_ids=(),
        job_status_counts={"succeeded": 6},
        unit_status_counts={"succeeded": 208},
        attempt_status_counts={"succeeded": model_attempts},
        model_attempt_count=model_attempts,
        real_model_call_count=0,
        provider_request_id_count=0,
        provider_request_id_sha256=None,
        metrics=metrics,
        performance=performance,
        stable_error_code_counts={},
    )


def test_performance_report_summarizes_tokens_latency_and_retries():
    started_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
    report = build_performance_report(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=2),
        unit_count=4,
        succeeded_unit_count=3,
        attempted_unit_count=3,
        attempts=(
            GraphEvalAttemptPerformance(100, 10, 20, "stop"),
            GraphEvalAttemptPerformance(200, 30, 40, "length"),
            GraphEvalAttemptPerformance(400, None, None, None),
            GraphEvalAttemptPerformance(800, 50, 60, "stop"),
        ),
    )

    assert report.wall_time_ms == 120_000
    assert report.units_per_minute == pytest.approx(1.5)
    assert report.model_attempt_count == 4
    assert report.retry_count == 1
    assert report.truncated_attempt_count == 1
    assert report.input_token_count == 90
    assert report.output_token_count == 120
    assert report.token_usage_coverage.value == pytest.approx(0.75)
    assert report.provider_latency_ms.model_dump() == {
        "count": 4,
        "min_ms": 100,
        "p50_ms": 200,
        "p95_ms": 800,
        "max_ms": 800,
        "mean_ms": 375.0,
    }


def test_old_eval_artifact_without_performance_remains_loadable(tmp_path: Path):
    payload = json.loads(
        (ROOT / "eval/graph_extraction/results/development-smoke-v1-20260715-01.json")
        .read_text(encoding="utf-8")
    )
    payload.pop("performance", None)
    path = tmp_path / "old-result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_graph_eval_run(path).performance is None


def test_comparison_enforces_speed_token_quality_and_evidence_gates():
    baseline = _artifact(
        "baseline", wall_time_ms=2_700_000, model_attempts=208, input_tokens=3_271_424
    )
    candidate = _artifact(
        "candidate",
        wall_time_ms=840_000,
        model_attempts=60,
        input_tokens=1_200_000,
        entity_score=0.99,
        relation_score=0.99,
    )

    comparison = compare_graph_eval_runs(
        baseline,
        candidate,
        thresholds=GraphBenchmarkThresholds(),
    )

    assert comparison.model_call_reduction == pytest.approx(1 - 60 / 208)
    assert comparison.input_token_reduction == pytest.approx(
        1 - 1_200_000 / 3_271_424
    )
    assert comparison.gates == {
        "wall_time": True,
        "model_calls": True,
        "input_tokens": True,
        "entity_f1": True,
        "relation_f1": True,
        "evidence": True,
        "schema": True,
        "terminal_failures": True,
    }
    assert comparison.passed is True


def test_comparison_requires_performance_data():
    candidate = _artifact(
        "candidate", wall_time_ms=1, model_attempts=1, input_tokens=1
    )
    candidate = candidate.model_copy(update={"performance": None})
    with pytest.raises(ValueError, match="performance"):
        compare_graph_eval_runs(candidate, candidate)
