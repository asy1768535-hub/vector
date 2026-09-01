"""DB-free assembly and reporting for Claim Shadow Replay v2."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.schemas.claim_decision import ClaimDecisionProjectionV1, canonical_claim_decision_json
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    RedactedDecisionV1,
    RedactedOccurrenceV1,
    RedactedRawClaimV2,
    ReplayCountV1,
    ReplayFailureV1,
    ReplayMetricsV1,
    canonical_claim_shadow_replay_v2_json,
    claim_shadow_v2_hash_text,
    redact_raw_claim_v2,
    replay_scope_v2_from_raw_claim,
    validate_claim_shadow_replay_artifact_any,
    validate_claim_shadow_replay_artifact_v2,
)
from app.schemas.raw_claim import RawClaimV1, revalidate_raw_claim
from app.schemas.shadow_extraction import ShadowTelemetryV1
from app.services.claim_shadow_raw_scorer import RawGoldScoreV1, score_claim_shadow_raw_gold
from app.services.claim_shadow_replay import (
    ClaimShadowReplayLoadError,
    ClaimShadowReplayRuntimeMetricsV1,
    load_claim_shadow_replay_artifact,
)


MAX_ASSEMBLY_RECORDS = 4096
_STAGE_ORDER = {"request": 0, "provider": 1, "parse": 2, "build": 3, "write": 4}
_SHA256_FIELDS = {
    "artifact_id_sha256",
    "run_id_sha256",
    "provider_key_sha256",
    "model_key_sha256",
    "config_sha256",
    "prompt_sha256",
}


class ClaimShadowReplayAssemblyError(ValueError):
    """Stable assembly failure without claim, decision, or provider payloads."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StructuralRawMetricsSectionV1(_ReportModel):
    metrics: ClaimShadowReplayRuntimeMetricsV1 | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_section(self) -> StructuralRawMetricsSectionV1:
        if (self.metrics is None) == (self.unavailable_reason is None):
            raise ValueError("structural raw metrics must be available or explicitly unavailable")
        return self


class OptionalRawGoldMetricsSectionV1(_ReportModel):
    metrics: RawGoldScoreV1 | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_section(self) -> OptionalRawGoldMetricsSectionV1:
        if (self.metrics is None) == (self.unavailable_reason is None):
            raise ValueError("raw gold metrics must be available or explicitly unavailable")
        return self


def _validate_json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise ValueError("canonical metrics must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical metrics must be bounded JSON") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("canonical metrics exceed the byte bound")
    return dict(value)


class CanonicalMetricsSectionV1(_ReportModel):
    metrics: dict[str, Any] | None = None
    unavailable_reason: str | None = None

    @field_validator("metrics", mode="before")
    @classmethod
    def validate_metrics(cls, value: Any) -> dict[str, Any] | None:
        return None if value is None else _validate_json_payload(value)

    @model_validator(mode="after")
    def validate_section(self) -> CanonicalMetricsSectionV1:
        if (self.metrics is None) == (self.unavailable_reason is None):
            raise ValueError("canonical metrics must be explicitly provided or unavailable")
        return self


class ClaimShadowReplayReportV1(_ReportModel):
    schema_version: Literal["claim_shadow_replay_report_v1"] = "claim_shadow_replay_report_v1"
    artifact_schema_version: Literal[
        "claim_shadow_replay_artifact_v1", "claim_shadow_replay_artifact_v2"
    ]
    artifact_id_sha256: str
    run_id_sha256: str
    structural_raw_metrics: StructuralRawMetricsSectionV1
    optional_raw_gold_metrics: OptionalRawGoldMetricsSectionV1
    canonical_metrics: CanonicalMetricsSectionV1
    failure: ReplayFailureV1 | None = None

    @field_validator("artifact_id_sha256", "run_id_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("report identity must be a lowercase SHA-256 hex digest")
        return value


@dataclass(frozen=True, slots=True)
class ClaimShadowReplayAssembly:
    artifact: ClaimShadowReplayArtifactV2
    report: ClaimShadowReplayReportV1


def _fail(code: str) -> None:
    raise ClaimShadowReplayAssemblyError(code)


def _as_claims(raw_claims: Sequence[RawClaimV1]) -> tuple[RawClaimV1, ...]:
    if isinstance(raw_claims, (str, bytes)) or not isinstance(raw_claims, Sequence):
        _fail("raw_claims_input_invalid")
    if len(raw_claims) > MAX_ASSEMBLY_RECORDS:
        _fail("raw_claims_bound_exceeded")
    claims = tuple(raw_claims)
    if any(not isinstance(claim, RawClaimV1) for claim in claims):
        _fail("raw_claim_not_validated")
    try:
        claims = tuple(revalidate_raw_claim(claim) for claim in claims)
    except Exception as exc:
        raise ClaimShadowReplayAssemblyError("raw_claim_contract_invalid") from exc
    occurrence_ids = [claim.extraction_occurrence_id for claim in claims]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        _fail("duplicate_occurrence_id")
    return claims


def _redacted_core_records(
    claims: tuple[RawClaimV1, ...],
) -> tuple[
    tuple[RedactedRawClaimV2, ...],
    dict[UUID, RedactedRawClaimV2],
    dict[UUID, RawClaimV1],
    dict[UUID, RedactedRawClaimV2],
]:
    representatives: dict[tuple[str, str], tuple[RawClaimV1, RedactedRawClaimV2]] = {}
    by_claim_id: dict[UUID, RawClaimV1] = {}
    by_occurrence_id: dict[UUID, RawClaimV1] = {}
    for claim in claims:
        if claim.claim_id in by_claim_id:
            previous = by_claim_id[claim.claim_id]
            if previous.content_scoped_claim_fingerprint != claim.content_scoped_claim_fingerprint:
                _fail("claim_id_content_conflict")
        by_claim_id[claim.claim_id] = claim
        by_occurrence_id[claim.extraction_occurrence_id] = claim
        record = redact_raw_claim_v2(claim)
        key = (record.scope_id, record.content_fingerprint)
        previous = representatives.get(key)
        if previous is None:
            representatives[key] = (claim, record)
            continue
        comparable = previous[1].model_dump(mode="json", exclude={"claim_id_sha256"})
        if record.model_dump(mode="json", exclude={"claim_id_sha256"}) != comparable:
            _fail("content_fingerprint_conflict")
        if str(claim.claim_id) < str(previous[0].claim_id):
            representatives[key] = (claim, record)
    records = tuple(
        record
        for _, record in sorted(
            representatives.values(), key=lambda item: (item[1].scope_id, item[1].content_fingerprint)
        )
    )
    by_claim_record = {
        claim.claim_id: representatives[(record.scope_id, record.content_fingerprint)][1]
        for claim in claims
        for record in (redact_raw_claim_v2(claim),)
    }
    return records, by_claim_record, by_claim_id, by_occurrence_id


def _redact_occurrence(claim: RawClaimV1, core: RedactedRawClaimV2) -> RedactedOccurrenceV1:
    return RedactedOccurrenceV1(
        scope_id=core.scope_id,
        occurrence_id_sha256=claim_shadow_v2_hash_text("occurrence_id", str(claim.extraction_occurrence_id)),
        claim_id_sha256=core.claim_id_sha256,
        occurrence_fingerprint=claim.extraction_occurrence_fingerprint or "",
        extractor_version=claim.extractor_version,
        parser_version=claim.parser_version,
        model_version=claim.model_name,
        prompt_version=claim.prompt_version,
        config_version=claim.model_config_hash,
    )


def _redact_decisions(
    decisions: Sequence[ClaimDecisionProjectionV1],
    *,
    by_claim_id: Mapping[UUID, RawClaimV1],
    by_occurrence_id: Mapping[UUID, RawClaimV1],
    by_claim_record: Mapping[UUID, RedactedRawClaimV2],
) -> tuple[RedactedDecisionV1, ...]:
    if isinstance(decisions, (str, bytes)) or not isinstance(decisions, Sequence):
        _fail("decisions_input_invalid")
    if len(decisions) > MAX_ASSEMBLY_RECORDS:
        _fail("decisions_bound_exceeded")
    records: list[RedactedDecisionV1] = []
    for decision in decisions:
        if not isinstance(decision, ClaimDecisionProjectionV1):
            _fail("decision_not_validated")
        try:
            validated_decision = ClaimDecisionProjectionV1.model_validate(
                json.loads(canonical_claim_decision_json(decision))
            )
        except Exception as exc:
            raise ClaimShadowReplayAssemblyError("decision_contract_invalid") from exc
        claim = by_claim_id.get(validated_decision.claim_id)
        core = by_claim_record.get(validated_decision.claim_id)
        if claim is None or core is None:
            _fail("decision_claim_unavailable")
        if (
            validated_decision.library_id != claim.library_id
            or validated_decision.document_id != claim.document_id
            or validated_decision.document_revision_id != claim.document_revision_id
            or validated_decision.revision_no != claim.revision_no
        ):
            _fail("decision_scope_mismatch")
        if validated_decision.extraction_occurrence_id is not None:
            occurrence = by_occurrence_id.get(validated_decision.extraction_occurrence_id)
            if occurrence is None or occurrence.claim_id != claim.claim_id:
                _fail("decision_occurrence_mismatch")
        records.append(
            RedactedDecisionV1(
                scope_id=core.scope_id,
                decision_id_sha256=claim_shadow_v2_hash_text("decision_id", str(validated_decision.decision_id)),
                claim_id_sha256=core.claim_id_sha256,
                occurrence_id_sha256=(
                    claim_shadow_v2_hash_text("occurrence_id", str(validated_decision.extraction_occurrence_id))
                    if validated_decision.extraction_occurrence_id is not None
                    else None
                ),
                decision_fingerprint=validated_decision.decision_fingerprint or "",
                decision_kind=validated_decision.decision_kind,
                status=validated_decision.status,
                reason_code=validated_decision.reason_code,
            )
        )
    if len({record.decision_id_sha256 for record in records}) != len(records):
        _fail("duplicate_decision_id")
    return tuple(sorted(records, key=lambda row: (row.scope_id, row.decision_fingerprint, row.decision_id_sha256)))


def _normalize_stages(stages: Sequence[str], *, status: Literal["success", "failed"], failure: ReplayFailureV1 | None) -> tuple[str, ...]:
    if isinstance(stages, (str, bytes)) or not isinstance(stages, Sequence):
        _fail("completed_stages_invalid")
    if not stages or len(stages) > 32 or any(not isinstance(stage, str) or not stage for stage in stages):
        _fail("completed_stages_invalid")
    if len(set(stages)) != len(stages):
        _fail("completed_stages_duplicate")
    ordered = tuple(sorted(stages, key=lambda value: (_STAGE_ORDER.get(value, 100), value)))
    known = [stage for stage in ordered if stage in _STAGE_ORDER]
    if known != [stage for stage in _STAGE_ORDER if stage in known]:
        _fail("completed_stages_order_invalid")
    if status == "success" and not {"request", "provider", "parse"}.issubset(ordered):
        _fail("success_stages_incomplete")
    if failure is not None and failure.stage in _STAGE_ORDER:
        completed_rank = max((_STAGE_ORDER[stage] for stage in known), default=-1)
        if _STAGE_ORDER[failure.stage] != completed_rank + 1:
            _fail("failed_stage_inconsistent")
    return ordered


def _normalize_counts(counts: Sequence[ReplayCountV1] | Mapping[str, int]) -> tuple[ReplayCountV1, ...]:
    if isinstance(counts, Mapping):
        values = tuple(ReplayCountV1(key=key, count=value) for key, value in counts.items())
    elif isinstance(counts, Sequence) and not isinstance(counts, (str, bytes)):
        values = tuple(
            item if isinstance(item, ReplayCountV1) else ReplayCountV1.model_validate(item)
            for item in counts
        )
    else:
        _fail("aggregate_counts_invalid")
    if len(values) > 128 or len({item.key for item in values}) != len(values):
        _fail("aggregate_counts_duplicate_or_bounded")
    return tuple(sorted(values, key=lambda item: item.key))


def assemble_claim_shadow_replay_artifact(
    *,
    status: Literal["success", "failed"],
    artifact_id_sha256: str,
    run_id_sha256: str,
    provider_key_sha256: str,
    model_key_sha256: str,
    config_sha256: str,
    prompt_sha256: str,
    artifact_producer_version: str,
    schema_producer_version: str,
    projection_producer_version: str,
    created_at: datetime,
    completed_stages: Sequence[str],
    raw_claims: Sequence[RawClaimV1],
    decisions: Sequence[ClaimDecisionProjectionV1] = (),
    telemetry: ShadowTelemetryV1 | None = None,
    aggregate_counts: Sequence[ReplayCountV1] | Mapping[str, int] = (),
    provider_call_count: int = 1,
    failure: ReplayFailureV1 | Mapping[str, Any] | None = None,
) -> ClaimShadowReplayArtifactV2:
    """Validate typed inputs, redact, deduplicate cores, and build one v2 artifact."""
    try:
        claims = _as_claims(raw_claims)
        records, by_claim_record, by_claim_id, by_occurrence_id = _redacted_core_records(claims)
        occurrences = tuple(
            sorted(
                (_redact_occurrence(claim, by_claim_record[claim.claim_id]) for claim in claims),
                key=lambda row: (row.scope_id, row.occurrence_fingerprint, row.occurrence_id_sha256),
            )
        )
        redacted_decisions = _redact_decisions(
            decisions,
            by_claim_id=by_claim_id,
            by_occurrence_id=by_occurrence_id,
            by_claim_record=by_claim_record,
        )
        scopes = tuple(
            sorted(
                {replay_scope_v2_from_raw_claim(claim).scope_id: replay_scope_v2_from_raw_claim(claim) for claim in claims}.values(),
                key=lambda scope: scope.scope_id,
            )
        )
        normalized_failure = (
            failure
            if isinstance(failure, ReplayFailureV1)
            else ReplayFailureV1.model_validate(failure)
            if failure is not None
            else None
        )
        stages = _normalize_stages(completed_stages, status=status, failure=normalized_failure)
        if status == "success":
            if normalized_failure is not None:
                _fail("success_failure_metadata_present")
            if telemetry is None or not isinstance(telemetry, ShadowTelemetryV1):
                _fail("success_telemetry_missing")
            if telemetry.error_code is not None or telemetry.input_token_count is None or telemetry.output_token_count is None:
                _fail("success_telemetry_incomplete")
            if telemetry.claim_count != len(claims):
                _fail("telemetry_claim_count_mismatch")
            if not isinstance(provider_call_count, int) or isinstance(provider_call_count, bool) or provider_call_count < 1:
                _fail("provider_call_count_invalid")
            metrics = ReplayMetricsV1(
                input_token_count=telemetry.input_token_count,
                output_token_count=telemetry.output_token_count,
                latency_ms=telemetry.latency_ms,
                provider_call_count=provider_call_count,
                raw_claim_count=len(claims),
                occurrence_count=len(occurrences),
                decision_count=len(redacted_decisions),
            )
        else:
            if normalized_failure is None:
                _fail("failed_failure_metadata_missing")
            metrics = None
        if status == "success" and (records or redacted_decisions) and not {"build", "write"}.issubset(stages):
            _fail("success_record_stages_incomplete")
        if status == "failed":
            completed_rank = max((_STAGE_ORDER[stage] for stage in stages if stage in _STAGE_ORDER), default=-1)
            if records and completed_rank < _STAGE_ORDER["build"]:
                _fail("failed_record_stages_incomplete")
            if redacted_decisions and completed_rank < _STAGE_ORDER["write"]:
                _fail("failed_decision_stages_incomplete")
        artifact = ClaimShadowReplayArtifactV2(
            artifact_id_sha256=artifact_id_sha256,
            run_id_sha256=run_id_sha256,
            provider_key_sha256=provider_key_sha256,
            model_key_sha256=model_key_sha256,
            config_sha256=config_sha256,
            prompt_sha256=prompt_sha256,
            artifact_producer_version=artifact_producer_version,
            schema_producer_version=schema_producer_version,
            projection_producer_version=projection_producer_version,
            created_at=created_at,
            status=status,
            completed_stages=stages,
            scopes=scopes,
            raw_claims=records,
            occurrences=occurrences,
            decisions=redacted_decisions,
            aggregate_counts=_normalize_counts(aggregate_counts),
            metrics=metrics,
            failure=normalized_failure,
        )
        return artifact
    except ClaimShadowReplayAssemblyError:
        raise
    except Exception as exc:
        code = "artifact_input_invalid"
        if any(field in str(exc) for field in _SHA256_FIELDS):
            code = "artifact_hash_invalid"
        raise ClaimShadowReplayAssemblyError(code) from exc


def write_claim_shadow_replay_artifact_v2(
    output_dir: Path,
    artifact: ClaimShadowReplayArtifactV2 | Mapping[str, Any],
) -> Path:
    """Write one canonical V2 artifact without changing its schema."""
    path: Path | None = None
    artifact_created = False
    try:
        source = artifact
        if not isinstance(source, ClaimShadowReplayArtifactV2):
            if isinstance(source, Mapping):
                source = validate_claim_shadow_replay_artifact_any(source)
            if not isinstance(source, ClaimShadowReplayArtifactV2):
                _fail("artifact_schema_unsupported")
        validated = validate_claim_shadow_replay_artifact_v2(source)
        payload = (canonical_claim_shadow_replay_v2_json(validated) + "\n").encode("utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"claim-shadow-replay-{validated.run_id_sha256[:16]}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        artifact_created = True
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            if artifact_created and path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return path
    except ClaimShadowReplayAssemblyError:
        raise
    except FileExistsError:
        raise
    except Exception as exc:
        raise ClaimShadowReplayAssemblyError("artifact_write_failed") from exc


def build_claim_shadow_replay_report(
    artifact: ClaimShadowReplayArtifactV1
    | ClaimShadowReplayArtifactV2
    | Mapping[str, Any],
    *,
    raw_gold: Any = None,
    canonical_metrics: Mapping[str, Any] | BaseModel | None = None,
) -> ClaimShadowReplayReportV1:
    """Load M4A output and expose structural/raw-gold/canonical sections independently."""
    try:
        loaded = load_claim_shadow_replay_artifact(artifact)
    except (ClaimShadowReplayLoadError, ValueError) as exc:
        raise ClaimShadowReplayAssemblyError("artifact_contract_invalid") from exc
    validated = loaded.artifact
    if validated.status == "success":
        structural = StructuralRawMetricsSectionV1(metrics=loaded.runtime_metrics)
    else:
        structural = StructuralRawMetricsSectionV1(unavailable_reason="failed_artifact_not_scored")
    if raw_gold is None:
        raw_gold_section = OptionalRawGoldMetricsSectionV1(unavailable_reason="raw_gold_not_provided")
    else:
        try:
            raw_gold_section = OptionalRawGoldMetricsSectionV1(
                metrics=score_claim_shadow_raw_gold(validated, raw_gold)
            )
        except Exception as exc:
            code = getattr(exc, "code", "raw_gold_unavailable")
            raw_gold_section = OptionalRawGoldMetricsSectionV1(unavailable_reason=code)
    canonical_section = (
        CanonicalMetricsSectionV1(metrics=canonical_metrics)
        if canonical_metrics is not None
        else CanonicalMetricsSectionV1(unavailable_reason="canonical_metrics_not_provided")
    )
    return ClaimShadowReplayReportV1(
        artifact_schema_version=validated.schema_version,
        artifact_id_sha256=validated.artifact_id_sha256,
        run_id_sha256=validated.run_id_sha256,
        structural_raw_metrics=structural,
        optional_raw_gold_metrics=raw_gold_section,
        canonical_metrics=canonical_section,
        failure=validated.failure,
    )


def assemble_claim_shadow_replay_report(*, raw_gold: Any = None, canonical_metrics: Mapping[str, Any] | BaseModel | None = None, **assembly_kwargs: Any) -> ClaimShadowReplayAssembly:
    artifact = assemble_claim_shadow_replay_artifact(**assembly_kwargs)
    report = build_claim_shadow_replay_report(
        artifact,
        raw_gold=raw_gold,
        canonical_metrics=canonical_metrics,
    )
    return ClaimShadowReplayAssembly(artifact=artifact, report=report)
