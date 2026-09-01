"""Typed performance gates for graph extraction evaluation artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.services.graph_extraction_eval import (
    GraphEvalRunArtifact,
    assert_sanitized_eval_artifact,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphBenchmarkThresholds(_StrictModel):
    max_wall_time_ms: int = Field(default=15 * 60_000, gt=0)
    min_model_call_reduction: float = Field(default=0.70, ge=0, le=1)
    min_input_token_reduction: float = Field(default=0.60, ge=0, le=1)
    max_entity_f1_drop: float = Field(default=0.02, ge=0, le=1)
    max_relation_f1_drop: float = Field(default=0.02, ge=0, le=1)
    max_terminal_failure_rate: float = Field(default=0.005, ge=0, le=1)


class GraphBenchmarkComparison(_StrictModel):
    schema_version: str = "graph-extraction-benchmark-comparison-v1"
    baseline_run_id: str
    candidate_run_id: str
    wall_time_reduction: float | None
    model_call_reduction: float | None
    input_token_reduction: float | None
    output_token_reduction: float | None
    entity_f1_delta: float | None
    relation_f1_delta: float | None
    terminal_failure_rate: float | None
    gates: dict[str, bool]
    passed: bool


def load_graph_eval_run(path: Path) -> GraphEvalRunArtifact:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load graph Eval result: {path.name}") from exc
    artifact = GraphEvalRunArtifact.model_validate(payload)
    assert_sanitized_eval_artifact(artifact.model_dump(mode="json"))
    return artifact


def _reduction(baseline: int, candidate: int) -> float | None:
    if baseline == 0:
        return None
    return 1 - candidate / baseline


def _f1(run: GraphEvalRunArtifact, kind: str) -> float | None:
    classification = getattr(run.metrics, kind)
    precision = classification.precision.value
    recall = classification.recall.value
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def compare_graph_eval_runs(
    baseline: GraphEvalRunArtifact,
    candidate: GraphEvalRunArtifact,
    *,
    thresholds: GraphBenchmarkThresholds | None = None,
) -> GraphBenchmarkComparison:
    policy = thresholds or GraphBenchmarkThresholds()
    if baseline.performance is None or candidate.performance is None:
        raise ValueError("both Eval results require performance data")
    if baseline.dataset_content_sha256 != candidate.dataset_content_sha256:
        raise ValueError("benchmark results must use identical dataset content")

    wall_time_reduction = _reduction(
        baseline.performance.wall_time_ms,
        candidate.performance.wall_time_ms,
    )
    model_call_reduction = _reduction(
        baseline.performance.model_attempt_count,
        candidate.performance.model_attempt_count,
    )
    input_token_reduction = _reduction(
        baseline.performance.input_token_count,
        candidate.performance.input_token_count,
    )
    output_token_reduction = _reduction(
        baseline.performance.output_token_count,
        candidate.performance.output_token_count,
    )
    baseline_entity_f1 = _f1(baseline, "entity")
    candidate_entity_f1 = _f1(candidate, "entity")
    baseline_relation_f1 = _f1(baseline, "relation")
    candidate_relation_f1 = _f1(candidate, "relation")
    entity_f1_delta = (
        None
        if baseline_entity_f1 is None or candidate_entity_f1 is None
        else candidate_entity_f1 - baseline_entity_f1
    )
    relation_f1_delta = (
        None
        if baseline_relation_f1 is None or candidate_relation_f1 is None
        else candidate_relation_f1 - baseline_relation_f1
    )
    terminal_failure_rate = (
        None
        if candidate.performance.unit_count == 0
        else (
            candidate.performance.unit_count
            - candidate.performance.succeeded_unit_count
        )
        / candidate.performance.unit_count
    )
    evidence_valid = (
        candidate.metrics.invalid_evidence_rate.numerator == 0
        and candidate.metrics.ambiguous_evidence_count == 0
        and candidate.metrics.cross_revision_evidence_count == 0
    )
    schema_valid = (
        candidate.metrics.schema_valid_rate.denominator > 0
        and candidate.metrics.schema_valid_rate.numerator
        == candidate.metrics.schema_valid_rate.denominator
    )
    gates = {
        "wall_time": candidate.performance.wall_time_ms <= policy.max_wall_time_ms,
        "model_calls": model_call_reduction is not None
        and model_call_reduction >= policy.min_model_call_reduction,
        "input_tokens": input_token_reduction is not None
        and input_token_reduction >= policy.min_input_token_reduction,
        "entity_f1": entity_f1_delta is not None
        and entity_f1_delta >= -policy.max_entity_f1_drop,
        "relation_f1": relation_f1_delta is not None
        and relation_f1_delta >= -policy.max_relation_f1_drop,
        "evidence": evidence_valid,
        "schema": schema_valid,
        "terminal_failures": terminal_failure_rate is not None
        and terminal_failure_rate <= policy.max_terminal_failure_rate,
    }
    comparison = GraphBenchmarkComparison(
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        wall_time_reduction=wall_time_reduction,
        model_call_reduction=model_call_reduction,
        input_token_reduction=input_token_reduction,
        output_token_reduction=output_token_reduction,
        entity_f1_delta=entity_f1_delta,
        relation_f1_delta=relation_f1_delta,
        terminal_failure_rate=terminal_failure_rate,
        gates=gates,
        passed=all(gates.values()),
    )
    assert_sanitized_eval_artifact(comparison.model_dump(mode="json"))
    return comparison


def write_benchmark_comparison(
    comparison: GraphBenchmarkComparison,
    output_path: Path,
) -> Path:
    if output_path.exists():
        raise FileExistsError("benchmark comparison artifacts are immutable")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        comparison.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ) + "\n"
    output_path.write_text(payload, encoding="utf-8", newline="\n")
    return output_path
