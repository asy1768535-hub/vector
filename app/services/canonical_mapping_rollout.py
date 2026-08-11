"""M5 gate-only rollout orchestration for isolated canonical canaries.

This module has no production caller and no database, provider, candidate,
materializer, or publication dependency. Callers supply bounded async stubs;
formal rollout and publication handoff remain unauthorized.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt
from pydantic import ValidationInfo, field_validator, model_validator

from app.schemas.canonical_mapping import (
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    MappingOutcome,
    canonical_mapping_input_json,
    canonical_mapping_json,
    canonical_mapping_json_value,
)
from app.services.canonical_mapping_comparison import (
    CanonicalComparisonMetricV1,
    CanonicalMappingComparisonCaseV1,
    CanonicalMappingMetricsV1,
    RawExtractionComparisonSectionV1,
    compare_canonical_mappings,
)
from app.services.canonical_mapping_quarantine import (
    CanonicalMappingBridgeConfigV1,
    CanonicalMappingBridgeEffect,
    CanonicalMappingBridgeReason,
    project_canonical_mapping_to_candidate_sidecar,
)
from app.services.claim_shadow_replay_assembler import (
    ClaimShadowReplayReportV1,
    build_claim_shadow_replay_report,
)


MAX_CANARY_CASES = 3
MAX_CANARY_TIMEOUT_SECONDS = 5.0
MAPPER_CLEANUP_GRACE_SECONDS = 0.01
MAX_LEAK_SENTINELS = 32
MAX_LEAK_SENTINEL_BYTES = 128 * 1024
MAX_LEAK_SENTINEL_TOTAL_BYTES = 256 * 1024
CANARY_CASE_KEYS = ("asset", "legal", "medical")
CanaryCaseKey = Literal["asset", "legal", "medical"]

class _RolloutModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )


class CanonicalMappingRolloutError(ValueError):
    """Stable M5 failure without source payload or exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CanonicalMappingCanaryFailure(str, Enum):
    CALL_LIMIT_REACHED = "call_limit_reached"
    MAPPER_TIMEOUT = "mapper_timeout"
    MAPPER_FAILED = "mapper_failed"
    MALFORMED_OUTPUT = "malformed_mapper_output"
    RESULT_AUTHORITY_INVALID = "result_authority_invalid"
    PERSISTENCE_FAILED = "persistence_failed"
    BRIDGE_VALIDATION_FAILED = "bridge_validation_failed"


class CanonicalMappingRolloutAuthorizationV1(_RolloutModel):
    schema_version: Literal["canonical_mapping_rollout_authorization_v1"] = (
        "canonical_mapping_rollout_authorization_v1"
    )
    global_execution_enabled: StrictBool = False
    authorized_library_ids: tuple[UUID, ...] = ()
    read_visibility_enabled: StrictBool = False
    bridge_visibility_enabled: StrictBool = False
    authorization_version: StrictInt = Field(default=1, ge=1)

    @field_validator("authorized_library_ids", mode="before")
    @classmethod
    def validate_library_ids_ingress(cls, value: Any, info: ValidationInfo) -> Any:
        if info.mode == "python":
            if type(value) is not tuple or any(type(item) is not UUID for item in value):
                raise ValueError("authorized library ids must be an exact UUID tuple")
        elif type(value) is not list or any(
            type(item) is not str or _canonical_uuid_string(item) is None for item in value
        ):
            raise ValueError("authorized library ids JSON must be canonical UUID strings")
        return value

    @field_validator("authorized_library_ids")
    @classmethod
    def validate_library_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("authorized library ids must be unique")
        return tuple(sorted(value, key=str))

    @model_validator(mode="after")
    def validate_visibility(self) -> CanonicalMappingRolloutAuthorizationV1:
        if self.bridge_visibility_enabled and not self.read_visibility_enabled:
            raise ValueError("bridge visibility requires read visibility")
        return self

    def execution_enabled_for(self, library_id: UUID) -> bool:
        return self.global_execution_enabled and library_id in self.authorized_library_ids


class CanonicalMappingCanaryLimitsV1(_RolloutModel):
    timeout_seconds: StrictFloat = Field(default=1.0, gt=0, le=MAX_CANARY_TIMEOUT_SECONDS)
    max_mapper_calls: StrictInt = Field(default=MAX_CANARY_CASES, ge=1, le=MAX_CANARY_CASES)


class CanonicalMappingCanaryCaseV1(_RolloutModel):
    case_key: CanaryCaseKey
    input: Any
    expected: Any = None


class CanonicalMappingCanaryRequestV1(_RolloutModel):
    case_key: CanaryCaseKey
    input: Any


class CanonicalMappingCanaryMapperOutputV1(_RolloutModel):
    result: Any
    latency_ms: StrictInt = Field(ge=0, le=300_000)
    cost_microunits: StrictInt = Field(ge=0, le=1_000_000_000)


class CanonicalMappingCanaryOutcomeV1(_RolloutModel):
    case_key: CanaryCaseKey
    execution_status: Literal["not_run", "succeeded", "failed"]
    failure_reason: CanonicalMappingCanaryFailure | None = None
    mapping_outcome: MappingOutcome | None = None
    mapping_result_id: UUID | None = None
    mapping_result_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    bridge_effect: CanonicalMappingBridgeEffect | None = None
    bridge_reason: CanonicalMappingBridgeReason | None = None
    latency_ms: StrictInt = Field(default=0, ge=0)
    cost_microunits: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> CanonicalMappingCanaryOutcomeV1:
        if self.execution_status == "succeeded":
            if (
                self.failure_reason is not None
                or self.mapping_outcome is None
                or self.mapping_result_id is None
                or self.mapping_result_fingerprint is None
                or self.bridge_effect is None
            ):
                raise ValueError("successful canary outcome is incomplete")
        elif self.execution_status == "failed":
            if self.failure_reason is None or self.mapping_outcome is not None:
                raise ValueError("failed canary outcome must carry only a stable failure")
        elif any(
            value is not None
            for value in (
                self.failure_reason,
                self.mapping_outcome,
                self.mapping_result_id,
                self.mapping_result_fingerprint,
                self.bridge_effect,
                self.bridge_reason,
            )
        ):
            raise ValueError("not-run canary outcome cannot carry mapping state")
        return self


class CanonicalMappingCanaryOperationalMetricsV1(_RolloutModel):
    mapper_call_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    persistence_call_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    mapped_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    ambiguous_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    blocked_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    rejected_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    unknown_retained_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    failure_count: StrictInt = Field(ge=0, le=MAX_CANARY_CASES)
    total_latency_ms: StrictInt = Field(ge=0)
    total_cost_microunits: StrictInt = Field(ge=0)
    endpoint_validity: CanonicalComparisonMetricV1
    direction_validity: CanonicalComparisonMetricV1
    evidence_validity: CanonicalComparisonMetricV1


class CanonicalMappingCanaryReportV1(_RolloutModel):
    schema_version: Literal["canonical_mapping_canary_report_v1"] = (
        "canonical_mapping_canary_report_v1"
    )
    library_id: UUID
    authorization_version: StrictInt = Field(ge=1)
    status: Literal["disabled", "succeeded", "failed"]
    read_visibility_enabled: StrictBool
    bridge_visibility_enabled: StrictBool
    publication_handoff: Literal["not_authorized"] = "not_authorized"
    outcomes: tuple[CanonicalMappingCanaryOutcomeV1, ...]
    raw_extraction_metrics: RawExtractionComparisonSectionV1
    canonical_mapping_metrics: CanonicalMappingMetricsV1 | None = None
    canonical_metrics_unavailable_reason: Literal[
        "mapping_rollout_disabled",
        "mapping_read_visibility_disabled",
        "canary_execution_failed",
        "canonical_gold_not_provided",
    ] | None = None
    replay_report: ClaimShadowReplayReportV1 | None = None
    replay_report_unavailable_reason: Literal[
        "mapping_rollout_disabled",
        "mapping_read_visibility_disabled",
        "replay_artifact_not_provided",
    ] | None = None
    operational_metrics: CanonicalMappingCanaryOperationalMetricsV1

    @model_validator(mode="after")
    def validate_report(self) -> CanonicalMappingCanaryReportV1:
        if len(self.outcomes) != MAX_CANARY_CASES:
            raise ValueError("canary report requires exactly three outcomes")
        if tuple(row.case_key for row in self.outcomes) != CANARY_CASE_KEYS:
            raise ValueError("canary outcomes must contain the exact deterministic suite")
        if (self.canonical_mapping_metrics is None) == (
            self.canonical_metrics_unavailable_reason is None
        ):
            raise ValueError("canonical metrics must be available or explicitly unavailable")
        if (self.replay_report is None) == (self.replay_report_unavailable_reason is None):
            raise ValueError("replay report must be available or explicitly unavailable")
        if self.status == "succeeded" and self.operational_metrics.failure_count:
            raise ValueError("successful canary report cannot contain system failures")
        return self


MapperCallable = Callable[
    [CanonicalMappingCanaryRequestV1], Awaitable[CanonicalMappingCanaryMapperOutputV1]
]
PersistenceCallable = Callable[[CanonicalMappingInputV1, CanonicalMappingV1], Awaitable[None]]


def rollback_canonical_mapping_rollout(
    authorization: CanonicalMappingRolloutAuthorizationV1,
) -> CanonicalMappingRolloutAuthorizationV1:
    validated = _validated_authorization(authorization)
    return CanonicalMappingRolloutAuthorizationV1(
        authorization_version=validated.authorization_version,
    )


def canonical_mapping_canary_report_json(
    report: CanonicalMappingCanaryReportV1,
    *,
    forbidden_values: Sequence[str] = (),
) -> str:
    if not isinstance(report, CanonicalMappingCanaryReportV1):
        raise CanonicalMappingRolloutError("report_not_typed")
    sentinels = _validated_leak_sentinels(forbidden_values)
    try:
        report_payload = report.model_dump(mode="json")
    except Exception as exc:
        raise CanonicalMappingRolloutError("report_contract_invalid") from exc
    if _contains_forbidden_value(report_payload, sentinels):
        raise CanonicalMappingRolloutError("artifact_leak_detected")
    try:
        payload = canonical_mapping_json_value(report_payload)
        rebuilt = CanonicalMappingCanaryReportV1.model_validate_json(payload)
        encoded = canonical_mapping_json_value(rebuilt.model_dump(mode="json"))
    except Exception as exc:
        raise CanonicalMappingRolloutError("report_contract_invalid") from exc
    if _contains_forbidden_value(rebuilt.model_dump(mode="json"), sentinels):
        raise CanonicalMappingRolloutError("artifact_leak_detected")
    return encoded


def canonical_mapping_canary_report_sha256(report: CanonicalMappingCanaryReportV1) -> str:
    return hashlib.sha256(canonical_mapping_canary_report_json(report).encode("utf-8")).hexdigest()


async def run_canonical_mapping_canary_suite(
    *,
    authorization: CanonicalMappingRolloutAuthorizationV1,
    library_id: UUID,
    cases: Sequence[CanonicalMappingCanaryCaseV1],
    mapper: MapperCallable,
    persistence: PersistenceCallable | None = None,
    limits: CanonicalMappingCanaryLimitsV1 = CanonicalMappingCanaryLimitsV1(),
    replay_artifact: Any = None,
    raw_gold: Any = None,
) -> CanonicalMappingCanaryReportV1:
    """Run one bounded no-write synthetic suite without authorizing publication writes.

    Supported mappers may defer one cancellation but must terminate on the second;
    an untrusted mapper that suppresses every cancellation requires a process
    boundary outside this gate.
    """

    auth = _validated_authorization(authorization)
    bounded_limits = _validated_limits(limits)
    if not isinstance(library_id, UUID):
        raise CanonicalMappingRolloutError("library_scope_invalid")
    typed_cases = _validated_cases(cases, library_id=library_id)

    if not auth.execution_enabled_for(library_id):
        return _disabled_report(auth, library_id, typed_cases)
    if not callable(mapper) or (persistence is not None and not callable(persistence)):
        raise CanonicalMappingRolloutError("canary_callable_invalid")

    outcomes: list[CanonicalMappingCanaryOutcomeV1] = []
    comparison_cases: list[CanonicalMappingComparisonCaseV1] = []
    successful_results: list[CanonicalMappingV1] = []
    mapper_calls = 0
    persistence_calls = 0

    for case, input_value, expected in typed_cases:
        if mapper_calls >= bounded_limits.max_mapper_calls:
            outcomes.append(_failed_outcome(case.case_key, CanonicalMappingCanaryFailure.CALL_LIMIT_REACHED))
            continue
        mapper_calls += 1
        task = asyncio.create_task(
            _invoke_mapper(
                mapper,
                CanonicalMappingCanaryRequestV1(case_key=case.case_key, input=input_value),
            )
        )
        done, _ = await asyncio.wait({task}, timeout=bounded_limits.timeout_seconds)
        if task not in done:
            await _cancel_and_drain_mapper_task(task)
            outcomes.append(_failed_outcome(case.case_key, CanonicalMappingCanaryFailure.MAPPER_TIMEOUT))
            continue
        try:
            output = task.result()
        except Exception:
            outcomes.append(_failed_outcome(case.case_key, CanonicalMappingCanaryFailure.MAPPER_FAILED))
            continue
        try:
            output = _validated_mapper_output(output)
        except CanonicalMappingRolloutError:
            outcomes.append(_failed_outcome(case.case_key, CanonicalMappingCanaryFailure.MALFORMED_OUTPUT))
            continue
        try:
            result = _validated_result(input_value, output.result)
        except CanonicalMappingRolloutError:
            outcomes.append(
                _failed_outcome(
                    case.case_key,
                    CanonicalMappingCanaryFailure.RESULT_AUTHORITY_INVALID,
                    latency_ms=output.latency_ms,
                    cost_microunits=output.cost_microunits,
                )
            )
            continue
        if persistence is not None:
            persistence_calls += 1
            try:
                await persistence(input_value, result)
            except Exception:
                outcomes.append(
                    _failed_outcome(
                        case.case_key,
                        CanonicalMappingCanaryFailure.PERSISTENCE_FAILED,
                        latency_ms=output.latency_ms,
                        cost_microunits=output.cost_microunits,
                    )
                )
                continue
        try:
            bridge = project_canonical_mapping_to_candidate_sidecar(
                input_value,
                result,
                config=CanonicalMappingBridgeConfigV1(enabled=auth.bridge_visibility_enabled),
            )
        except Exception:
            outcomes.append(
                _failed_outcome(
                    case.case_key,
                    CanonicalMappingCanaryFailure.BRIDGE_VALIDATION_FAILED,
                    latency_ms=output.latency_ms,
                    cost_microunits=output.cost_microunits,
                )
            )
            continue
        outcomes.append(
            CanonicalMappingCanaryOutcomeV1(
                case_key=case.case_key,
                execution_status="succeeded",
                mapping_outcome=result.outcome,
                mapping_result_id=result.mapping_result_id,
                mapping_result_fingerprint=result.mapping_result_fingerprint,
                bridge_effect=bridge.effect,
                bridge_reason=bridge.reason_code,
                latency_ms=output.latency_ms,
                cost_microunits=output.cost_microunits,
            )
        )
        successful_results.append(result)
        comparison_cases.append(
            CanonicalMappingComparisonCaseV1(
                input=input_value,
                expected=expected,
                predicted=result,
            )
        )

    outcomes.sort(key=lambda row: row.case_key)
    failed = any(row.execution_status == "failed" for row in outcomes)
    raw_only = compare_canonical_mappings(
        [],
        replay_artifact=replay_artifact if auth.read_visibility_enabled else None,
        raw_gold=raw_gold if auth.read_visibility_enabled else None,
    )
    canonical_metrics: CanonicalMappingMetricsV1 | None = None
    canonical_unavailable: str | None = None
    replay_report: ClaimShadowReplayReportV1 | None = None
    replay_unavailable: str | None = None
    if not auth.read_visibility_enabled:
        canonical_unavailable = "mapping_read_visibility_disabled"
        replay_unavailable = "mapping_read_visibility_disabled"
    elif failed:
        canonical_unavailable = "canary_execution_failed"
        if replay_artifact is None:
            replay_unavailable = "replay_artifact_not_provided"
        else:
            replay_report = build_claim_shadow_replay_report(replay_artifact, raw_gold=raw_gold)
    elif not any(expected is not None and expected.outcome == "mapped" for _, _, expected in typed_cases):
        canonical_unavailable = "canonical_gold_not_provided"
        if replay_artifact is None:
            replay_unavailable = "replay_artifact_not_provided"
        else:
            replay_report = build_claim_shadow_replay_report(replay_artifact, raw_gold=raw_gold)
    else:
        comparison = compare_canonical_mappings(
            comparison_cases,
            replay_artifact=replay_artifact,
            raw_gold=raw_gold,
        )
        canonical_metrics = comparison.canonical_mapping_metrics
        if replay_artifact is None:
            replay_unavailable = "replay_artifact_not_provided"
        else:
            replay_report = comparison.to_claim_shadow_replay_report(
                replay_artifact,
                raw_gold=raw_gold,
            )

    return CanonicalMappingCanaryReportV1(
        library_id=library_id,
        authorization_version=auth.authorization_version,
        status="failed" if failed else "succeeded",
        read_visibility_enabled=auth.read_visibility_enabled,
        bridge_visibility_enabled=auth.bridge_visibility_enabled,
        outcomes=tuple(outcomes),
        raw_extraction_metrics=raw_only.raw_extraction_metrics,
        canonical_mapping_metrics=canonical_metrics,
        canonical_metrics_unavailable_reason=canonical_unavailable,
        replay_report=replay_report,
        replay_report_unavailable_reason=replay_unavailable,
        operational_metrics=_operational_metrics(
            outcomes,
            successful_results,
            mapper_calls=mapper_calls,
            persistence_calls=persistence_calls,
        ),
    )


def _validated_authorization(
    value: CanonicalMappingRolloutAuthorizationV1,
) -> CanonicalMappingRolloutAuthorizationV1:
    if not isinstance(value, CanonicalMappingRolloutAuthorizationV1):
        raise CanonicalMappingRolloutError("authorization_not_typed")
    if type(value.authorized_library_ids) is not tuple or any(
        type(item) is not UUID for item in value.authorized_library_ids
    ):
        raise CanonicalMappingRolloutError("authorization_invalid")
    try:
        return CanonicalMappingRolloutAuthorizationV1.model_validate_json(
            canonical_mapping_json_value(value.model_dump(mode="json"))
        )
    except Exception as exc:
        raise CanonicalMappingRolloutError("authorization_invalid") from exc


def _validated_limits(value: CanonicalMappingCanaryLimitsV1) -> CanonicalMappingCanaryLimitsV1:
    if not isinstance(value, CanonicalMappingCanaryLimitsV1):
        raise CanonicalMappingRolloutError("limits_not_typed")
    try:
        return CanonicalMappingCanaryLimitsV1.model_validate_json(
            canonical_mapping_json_value(value.model_dump(mode="json"))
        )
    except Exception as exc:
        raise CanonicalMappingRolloutError("limits_invalid") from exc


def _validated_cases(
    cases: Sequence[CanonicalMappingCanaryCaseV1],
    *,
    library_id: UUID,
) -> tuple[tuple[CanonicalMappingCanaryCaseV1, CanonicalMappingInputV1, CanonicalMappingV1 | None], ...]:
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
        raise CanonicalMappingRolloutError("cases_input_invalid")
    if len(cases) != MAX_CANARY_CASES:
        raise CanonicalMappingRolloutError("canary_case_count_invalid")
    rows = []
    keys: set[str] = set()
    occurrences: set[UUID] = set()
    for case in cases:
        if not isinstance(case, CanonicalMappingCanaryCaseV1):
            raise CanonicalMappingRolloutError("case_not_typed")
        if case.case_key not in CANARY_CASE_KEYS:
            raise CanonicalMappingRolloutError("canary_case_identity_invalid")
        try:
            rebuilt_case = CanonicalMappingCanaryCaseV1.model_validate(
                {"case_key": case.case_key, "input": case.input, "expected": case.expected}
            )
            canonical_mapping_input_json(rebuilt_case.input)
            input_value = rebuilt_case.input
            expected = (
                None
                if rebuilt_case.expected is None
                else CanonicalMappingV1.model_validate_json(
                    canonical_mapping_json(input_value, rebuilt_case.expected)
                )
            )
        except Exception as exc:
            raise CanonicalMappingRolloutError("case_authority_invalid") from exc
        if input_value.library_id != library_id:
            raise CanonicalMappingRolloutError("canary_scope_leakage")
        if rebuilt_case.case_key in keys or input_value.extraction_occurrence_id in occurrences:
            raise CanonicalMappingRolloutError("canary_case_duplicate")
        keys.add(rebuilt_case.case_key)
        occurrences.add(input_value.extraction_occurrence_id)
        rows.append((rebuilt_case, input_value, expected))
    if keys != set(CANARY_CASE_KEYS):
        raise CanonicalMappingRolloutError("canary_case_identity_invalid")
    return tuple(sorted(rows, key=lambda row: row[0].case_key))


def _validated_mapper_output(value: Any) -> CanonicalMappingCanaryMapperOutputV1:
    if not isinstance(value, CanonicalMappingCanaryMapperOutputV1):
        raise CanonicalMappingRolloutError("mapper_output_not_typed")
    try:
        return CanonicalMappingCanaryMapperOutputV1.model_validate(
            {
                "result": value.result,
                "latency_ms": value.latency_ms,
                "cost_microunits": value.cost_microunits,
            }
        )
    except Exception as exc:
        raise CanonicalMappingRolloutError("mapper_output_invalid") from exc


def _validated_result(
    input_value: CanonicalMappingInputV1,
    result: Any,
) -> CanonicalMappingV1:
    if not isinstance(result, CanonicalMappingV1):
        raise CanonicalMappingRolloutError("mapping_result_not_typed")
    try:
        return CanonicalMappingV1.model_validate_json(canonical_mapping_json(input_value, result))
    except Exception as exc:
        raise CanonicalMappingRolloutError("mapping_result_invalid") from exc


async def _invoke_mapper(
    mapper: MapperCallable,
    request: CanonicalMappingCanaryRequestV1,
) -> CanonicalMappingCanaryMapperOutputV1:
    return await mapper(request)


def _consume_mapper_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _cancel_and_drain_mapper_task(task: asyncio.Task[Any]) -> None:
    """Bound cancellation for the supported synthetic mapper contract."""

    for _ in range(2):
        if task.done():
            break
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=MAPPER_CLEANUP_GRACE_SECONDS)
        if task in done:
            break
    if not task.done():
        task.add_done_callback(_consume_mapper_task)
        raise CanonicalMappingRolloutError("mapper_cleanup_failed")
    _consume_mapper_task(task)


def _failed_outcome(
    case_key: CanaryCaseKey,
    reason: CanonicalMappingCanaryFailure,
    *,
    latency_ms: int = 0,
    cost_microunits: int = 0,
) -> CanonicalMappingCanaryOutcomeV1:
    return CanonicalMappingCanaryOutcomeV1(
        case_key=case_key,
        execution_status="failed",
        failure_reason=reason,
        latency_ms=latency_ms,
        cost_microunits=cost_microunits,
    )


def _metric(valid: int, total: int, *, reason: str) -> CanonicalComparisonMetricV1:
    if total == 0:
        return CanonicalComparisonMetricV1(unavailable_reason=reason)
    return CanonicalComparisonMetricV1(value=float(valid / total))


def _operational_metrics(
    outcomes: Sequence[CanonicalMappingCanaryOutcomeV1],
    results: Sequence[CanonicalMappingV1],
    *,
    mapper_calls: int,
    persistence_calls: int,
) -> CanonicalMappingCanaryOperationalMetricsV1:
    counts = {outcome: sum(result.outcome == outcome for result in results) for outcome in ("mapped", "ambiguous", "blocked", "rejected")}
    mapped = tuple(result for result in results if result.outcome == "mapped")
    endpoint_valid = sum(
        result.canonical_source_endpoint is not None and result.canonical_target_endpoint is not None
        for result in mapped
    )
    direction_valid = sum(result.canonical_direction is not None for result in mapped)
    evidence_valid = sum(
        bool(result.mapping_evidence_ref_ids)
        and len(result.mapping_evidence_ref_ids) == len(result.evidence_bindings)
        for result in mapped
    )
    return CanonicalMappingCanaryOperationalMetricsV1(
        mapper_call_count=mapper_calls,
        persistence_call_count=persistence_calls,
        mapped_count=counts["mapped"],
        ambiguous_count=counts["ambiguous"],
        blocked_count=counts["blocked"],
        rejected_count=counts["rejected"],
        unknown_retained_count=sum(
            result.reason_code is not None and result.reason_code.startswith("unknown_")
            for result in results
        ),
        failure_count=sum(row.execution_status == "failed" for row in outcomes),
        total_latency_ms=sum(row.latency_ms for row in outcomes),
        total_cost_microunits=sum(row.cost_microunits for row in outcomes),
        endpoint_validity=_metric(
            endpoint_valid,
            len(mapped),
            reason="no_predicted_canonical_mappings",
        ),
        direction_validity=_metric(
            direction_valid,
            len(mapped),
            reason="no_predicted_canonical_mappings",
        ),
        evidence_validity=_metric(
            evidence_valid,
            len(mapped),
            reason="no_predicted_canonical_mappings",
        ),
    )


def _disabled_report(
    authorization: CanonicalMappingRolloutAuthorizationV1,
    library_id: UUID,
    cases: Sequence[tuple[CanonicalMappingCanaryCaseV1, CanonicalMappingInputV1, CanonicalMappingV1 | None]],
) -> CanonicalMappingCanaryReportV1:
    raw = compare_canonical_mappings([]).raw_extraction_metrics
    outcomes = tuple(
        CanonicalMappingCanaryOutcomeV1(case_key=case.case_key, execution_status="not_run")
        for case, _, _ in cases
    )
    return CanonicalMappingCanaryReportV1(
        library_id=library_id,
        authorization_version=authorization.authorization_version,
        status="disabled",
        read_visibility_enabled=False,
        bridge_visibility_enabled=False,
        outcomes=outcomes,
        raw_extraction_metrics=raw,
        canonical_metrics_unavailable_reason="mapping_rollout_disabled",
        replay_report_unavailable_reason="mapping_rollout_disabled",
        operational_metrics=_operational_metrics(
            outcomes,
            (),
            mapper_calls=0,
            persistence_calls=0,
        ),
    )


def _canonical_uuid_string(value: str) -> str | None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def _validated_leak_sentinels(values: Sequence[str]) -> tuple[tuple[str, Any | None], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise CanonicalMappingRolloutError("leak_sentinels_invalid")
    if len(values) > MAX_LEAK_SENTINELS:
        raise CanonicalMappingRolloutError("leak_sentinels_invalid")
    sentinels = []
    total_bytes = 0
    for value in values:
        if type(value) is not str or not value:
            raise CanonicalMappingRolloutError("leak_sentinels_invalid")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise CanonicalMappingRolloutError("leak_sentinels_invalid") from exc
        if size > MAX_LEAK_SENTINEL_BYTES:
            raise CanonicalMappingRolloutError("leak_sentinels_invalid")
        total_bytes += size
        if total_bytes > MAX_LEAK_SENTINEL_TOTAL_BYTES:
            raise CanonicalMappingRolloutError("leak_sentinels_invalid")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        sentinels.append((value, decoded if isinstance(decoded, (dict, list)) else None))
    return tuple(sentinels)


def _contains_forbidden_value(
    payload: Any,
    sentinels: Sequence[tuple[str, Any | None]],
) -> bool:
    if isinstance(payload, Mapping):
        if any(sentinel in key for key in payload if isinstance(key, str) for sentinel, _ in sentinels):
            return True
        children = payload.values()
    elif isinstance(payload, list):
        children = payload
    else:
        if isinstance(payload, str) and any(sentinel in payload for sentinel, _ in sentinels):
            return True
        return any(structured is not None and payload == structured for _, structured in sentinels)
    if any(structured is not None and payload == structured for _, structured in sentinels):
        return True
    return any(_contains_forbidden_value(child, sentinels) for child in children)


__all__ = [
    "CanonicalMappingCanaryCaseV1",
    "CanonicalMappingCanaryFailure",
    "CanonicalMappingCanaryLimitsV1",
    "CanonicalMappingCanaryMapperOutputV1",
    "CanonicalMappingCanaryOperationalMetricsV1",
    "CanonicalMappingCanaryOutcomeV1",
    "CanonicalMappingCanaryRequestV1",
    "CanonicalMappingCanaryReportV1",
    "CanonicalMappingRolloutAuthorizationV1",
    "CanonicalMappingRolloutError",
    "MAX_CANARY_CASES",
    "MAPPER_CLEANUP_GRACE_SECONDS",
    "canonical_mapping_canary_report_json",
    "canonical_mapping_canary_report_sha256",
    "rollback_canonical_mapping_rollout",
    "run_canonical_mapping_canary_suite",
]
