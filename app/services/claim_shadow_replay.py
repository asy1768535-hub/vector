"""DB-free loader and structural/runtime metrics for Claim Shadow Replay v1.

This module never executes artifact content and has no provider, database, or
network dependency. Raw gold comparison is deliberately out of scope for
M4B1; this reports only facts contained in the self-contained artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    ReplayCountV1,
    ReplayFailureV1,
    canonical_claim_shadow_replay_json,
    canonical_claim_shadow_replay_v2_json,
    claim_shadow_replay_filename,
    validate_claim_shadow_replay_artifact_any,
)


class ClaimShadowReplayLoadError(ValueError):
    """Stable loader failure; never includes artifact content or exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ReplayMetricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayMetricValueV1(_ReplayMetricModel):
    value: int | float | None = None
    unavailable_reason: str | None = None

    @field_validator("unavailable_reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 128 or not value.replace("_", "").isalnum():
            raise ValueError("unavailable_reason must be a stable bounded code")
        return value

    @model_validator(mode="after")
    def validate_value(self) -> ReplayMetricValueV1:
        if self.value is None and self.unavailable_reason is None:
            raise ValueError("unavailable metric values require a reason")
        if self.value is not None:
            if isinstance(self.value, bool) or not math.isfinite(float(self.value)):
                raise ValueError("metric value must be finite")
            if self.unavailable_reason is not None:
                raise ValueError("available metric values cannot have an unavailable reason")
        return self


class ReplayScopeMetricsV1(_ReplayMetricModel):
    scope_id: str
    revision_no: StrictInt = Field(ge=1)
    claim_count: StrictInt = Field(ge=0)
    unique_claim_count: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=0)
    decision_count: StrictInt = Field(ge=0)
    direction_counts: tuple[ReplayCountV1, ...]
    decision_kind_counts: tuple[ReplayCountV1, ...]
    decision_reason_counts: tuple[ReplayCountV1, ...]
    extractor_version_counts: tuple[ReplayCountV1, ...]
    model_version_counts: tuple[ReplayCountV1, ...]
    prompt_version_counts: tuple[ReplayCountV1, ...]
    config_version_counts: tuple[ReplayCountV1, ...]


class ClaimShadowReplayRuntimeMetricsV1(_ReplayMetricModel):
    raw_claim_count: StrictInt = Field(ge=0)
    unique_claim_count: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=0)
    decision_count: StrictInt = Field(ge=0)
    claim_dedup_rate: ReplayMetricValueV1
    unknown_predicate_retention: ReplayMetricValueV1
    unknown_endpoint_retention: ReplayMetricValueV1
    unknown_direction_retention: ReplayMetricValueV1
    shadow_write_success_count: ReplayMetricValueV1
    shadow_write_failure_count: ReplayMetricValueV1
    input_token_total: ReplayMetricValueV1
    output_token_total: ReplayMetricValueV1
    latency_p50_ms: ReplayMetricValueV1
    latency_p95_ms: ReplayMetricValueV1
    scope_metrics: tuple[ReplayScopeMetricsV1, ...]
    canonical_metrics_status: Literal["absent"] = "absent"


class ClaimShadowReplayLoadResult(_ReplayMetricModel):
    artifact: ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2
    runtime_metrics: ClaimShadowReplayRuntimeMetricsV1 | None
    failure: ReplayFailureV1 | None
    canonical_metrics_status: Literal["absent"] = "absent"


def _parse_json_bytes(payload: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ClaimShadowReplayLoadError("artifact_json_invalid") from exc
    if not isinstance(decoded, dict):
        raise ClaimShadowReplayLoadError("artifact_root_not_object")
    return decoded


def _load_mapping(value: Mapping[str, Any]) -> ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2:
    try:
        return validate_claim_shadow_replay_artifact_any(value)
    except Exception as exc:
        raise ClaimShadowReplayLoadError("artifact_contract_invalid") from exc


def _load_bytes(
    payload: bytes,
    *,
    expected_sha256: str | None = None,
) -> ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2:
    if len(payload) > 2 * 1024 * 1024:
        raise ClaimShadowReplayLoadError("artifact_too_large")
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise ClaimShadowReplayLoadError("expected_sha256_invalid")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ClaimShadowReplayLoadError("artifact_digest_mismatch")
    artifact = _load_mapping(_parse_json_bytes(payload))
    canonicalizer = (
        canonical_claim_shadow_replay_v2_json
        if isinstance(artifact, ClaimShadowReplayArtifactV2)
        else canonical_claim_shadow_replay_json
    )
    canonical = (canonicalizer(artifact) + "\n").encode("utf-8")
    if payload != canonical:
        raise ClaimShadowReplayLoadError("artifact_not_canonical")
    return artifact


def load_claim_shadow_replay_artifact(
    source: Path
    | bytes
    | ClaimShadowReplayArtifactV2
    | Mapping[str, Any],
    *,
    expected_sha256: str | None = None,
) -> ClaimShadowReplayLoadResult:
    """Load and revalidate a caller-provided artifact, without side effects."""

    if isinstance(source, Path):
        if source.name == "" or not source.exists() or not source.is_file():
            raise ClaimShadowReplayLoadError("artifact_file_unavailable")
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise ClaimShadowReplayLoadError("artifact_file_unavailable") from exc
        artifact = _load_bytes(payload, expected_sha256=expected_sha256)
        if source.name != claim_shadow_replay_filename(artifact):
            raise ClaimShadowReplayLoadError("artifact_filename_mismatch")
    elif isinstance(source, bytes):
        artifact = _load_bytes(source, expected_sha256=expected_sha256)
    elif isinstance(source, ClaimShadowReplayArtifactV2):
        if expected_sha256 is not None:
            raise ClaimShadowReplayLoadError("expected_sha256_requires_bytes")
        artifact = _load_mapping(source.model_dump(mode="json"))
    elif isinstance(source, Mapping):
        if expected_sha256 is not None:
            raise ClaimShadowReplayLoadError("expected_sha256_requires_bytes")
        artifact = _load_mapping(source)
    else:
        raise ClaimShadowReplayLoadError("artifact_source_invalid")

    result_artifact = artifact
    if isinstance(artifact, ClaimShadowReplayArtifactV2):
        result_artifact = ClaimShadowReplayArtifactV2.model_validate(
            artifact.model_dump(mode="json")
        )
    runtime_metrics = _compute_runtime_metrics(result_artifact) if result_artifact.status == "success" else None
    return ClaimShadowReplayLoadResult(
        artifact=result_artifact,
        runtime_metrics=runtime_metrics,
        failure=result_artifact.failure,
    )


def _counts(rows: list[str]) -> tuple[ReplayCountV1, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row] = counts.get(row, 0) + 1
    return tuple(ReplayCountV1(key=key, count=counts[key]) for key in sorted(counts))


def _aggregate_counts(artifact: ClaimShadowReplayArtifactV1) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in artifact.aggregate_counts:
        if row.key in result:
            raise ClaimShadowReplayLoadError("aggregate_count_duplicate")
        result[row.key] = row.count
    return result


def _metric(value: int | float | None, reason: str | None = None) -> ReplayMetricValueV1:
    return ReplayMetricValueV1(value=value, unavailable_reason=reason)


def _ratio_count(counts: Mapping[str, int], key: str, denominator: int) -> ReplayMetricValueV1:
    if denominator == 0:
        return _metric(None, "no_claim_records")
    if key not in counts:
        return _metric(None, "aggregate_count_unavailable")
    if counts[key] > denominator:
        raise ClaimShadowReplayLoadError("aggregate_count_exceeds_claim_records")
    return _metric(counts[key] / denominator)


def _scope_metrics(artifact: ClaimShadowReplayArtifactV1) -> tuple[ReplayScopeMetricsV1, ...]:
    claims_by_scope: dict[str, list[Any]] = {scope.scope_id: [] for scope in artifact.scopes}
    occurrences_by_scope: dict[str, list[Any]] = {scope.scope_id: [] for scope in artifact.scopes}
    decisions_by_scope: dict[str, list[Any]] = {scope.scope_id: [] for scope in artifact.scopes}
    for claim in artifact.raw_claims:
        claims_by_scope[claim.scope_id].append(claim)
    for occurrence in artifact.occurrences:
        occurrences_by_scope[occurrence.scope_id].append(occurrence)
    for decision in artifact.decisions:
        decisions_by_scope[decision.scope_id].append(decision)

    result: list[ReplayScopeMetricsV1] = []
    scope_versions = {scope.scope_id: scope.revision_no for scope in artifact.scopes}
    for scope_id in sorted(scope_versions):
        claims = claims_by_scope[scope_id]
        occurrences = occurrences_by_scope[scope_id]
        decisions = decisions_by_scope[scope_id]
        unique_claims = len({claim.content_fingerprint for claim in claims})
        result.append(
            ReplayScopeMetricsV1(
                scope_id=scope_id,
                revision_no=scope_versions[scope_id],
                claim_count=len(claims),
                unique_claim_count=unique_claims,
                occurrence_count=len(occurrences),
                decision_count=len(decisions),
                direction_counts=_counts([claim.direction for claim in claims]),
                decision_kind_counts=_counts([decision.decision_kind for decision in decisions]),
                decision_reason_counts=_counts([decision.reason_code for decision in decisions]),
                extractor_version_counts=_counts([row.extractor_version for row in occurrences]),
                model_version_counts=_counts([row.model_version for row in occurrences]),
                prompt_version_counts=_counts([row.prompt_version for row in occurrences]),
                config_version_counts=_counts([row.config_version for row in occurrences]),
            )
        )
    return tuple(result)


def _compute_runtime_metrics(
    artifact: ClaimShadowReplayArtifactV1,
) -> ClaimShadowReplayRuntimeMetricsV1:
    if artifact.metrics is None:
        raise ClaimShadowReplayLoadError("success_metrics_missing")
    counts = _aggregate_counts(artifact)
    legacy_v1_count_semantics = isinstance(artifact, ClaimShadowReplayArtifactV1) and not isinstance(
        artifact, ClaimShadowReplayArtifactV2
    )
    # v1 predates observed-count semantics; keep its historical list-based count behavior.
    raw_claim_count = len(artifact.raw_claims) if legacy_v1_count_semantics else artifact.metrics.raw_claim_count
    unique_claim_count = len({(claim.scope_id, claim.content_fingerprint) for claim in artifact.raw_claims})
    provider_samples = artifact.metrics.provider_call_count
    if provider_samples:
        # M4A stores one run-level sample, so both percentiles equal that
        # sample. Per-unit percentile aggregation belongs to a later artifact.
        latency_p50 = _metric(float(artifact.metrics.latency_ms))
        latency_p95 = _metric(float(artifact.metrics.latency_ms))
        input_tokens = _metric(artifact.metrics.input_token_count)
        output_tokens = _metric(artifact.metrics.output_token_count)
    else:
        latency_p50 = _metric(None, "no_provider_samples")
        latency_p95 = _metric(None, "no_provider_samples")
        input_tokens = _metric(None, "no_provider_samples")
        output_tokens = _metric(None, "no_provider_samples")

    return ClaimShadowReplayRuntimeMetricsV1(
        raw_claim_count=raw_claim_count,
        unique_claim_count=unique_claim_count,
        occurrence_count=len(artifact.occurrences),
        decision_count=len(artifact.decisions),
        claim_dedup_rate=(
            _metric((raw_claim_count - unique_claim_count) / raw_claim_count)
            if raw_claim_count
            else _metric(None, "no_claim_records")
        ),
        unknown_predicate_retention=_ratio_count(counts, "unknown_predicate", raw_claim_count),
        unknown_endpoint_retention=_ratio_count(counts, "unknown_endpoint", raw_claim_count),
        unknown_direction_retention=_ratio_count(counts, "unknown_direction", raw_claim_count),
        shadow_write_success_count=(
            _metric(counts["shadow_write_success"])
            if "shadow_write_success" in counts
            else _metric(None, "aggregate_count_unavailable")
        ),
        shadow_write_failure_count=(
            _metric(counts["shadow_write_failure"])
            if "shadow_write_failure" in counts
            else _metric(None, "aggregate_count_unavailable")
        ),
        input_token_total=input_tokens,
        output_token_total=output_tokens,
        latency_p50_ms=latency_p50,
        latency_p95_ms=latency_p95,
        scope_metrics=_scope_metrics(artifact),
    )
