"""Eval-only comparison of raw extraction and canonical mapping families.

This module is deliberately outside the production mapping flow.  It accepts
already validated M1 inputs and explicit M1 results, then computes a separate
canonical section.  Raw replay metrics are obtained through the existing M4
typed report API and are never used as canonical gold truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from app.schemas.canonical_mapping import (
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    canonical_mapping_input_json,
    canonical_mapping_json,
)
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    canonical_claim_shadow_replay_json,
    canonical_claim_shadow_replay_v2_json,
)
from app.services.claim_shadow_replay_assembler import (
    ClaimShadowReplayAssemblyError,
    ClaimShadowReplayReportV1,
    OptionalRawGoldMetricsSectionV1,
    StructuralRawMetricsSectionV1,
    build_claim_shadow_replay_report,
)
from app.services.claim_shadow_replay import (
    ClaimShadowReplayLoadError,
    load_claim_shadow_replay_artifact,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OVERALL_SCOPE_KEY = hashlib.sha256(b"canonical_mapping_comparison_v1:overall").hexdigest()
_UNAVAILABLE_REASONS = frozenset(
    {
        "invalid_locator",
        "no_gold_canonical_mappings",
        "no_predicted_canonical_mappings",
        "no_unknown_direction_diagnostic",
        "no_unknown_endpoint_diagnostic",
        "no_unknown_predicate_diagnostic",
        "raw_artifact_not_provided",
        "raw_gold_not_provided",
    }
)


class CanonicalMappingComparisonError(ValueError):
    """Stable fail-closed comparison error without input payload leakage."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ComparisonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class CanonicalComparisonMetricV1(_ComparisonModel):
    """A metric whose zero-denominator state is explicit and non-zero."""

    value: StrictFloat | None = None
    unavailable_reason: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: Any) -> float | None:
        if value is None:
            return None
        if type(value) is not float or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("metric value must be a finite native float in [0, 1]")
        return value

    @field_validator("unavailable_reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str | None:
        return _validate_unavailable_reason(value)

    @model_validator(mode="after")
    def validate_metric(self) -> CanonicalComparisonMetricV1:
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("metric must be available or explicitly unavailable")
        return self


class CanonicalMappingComparisonCaseV1(_ComparisonModel):
    """One typed M1 input with optional explicit gold and mapper outputs."""

    # Nested M0 models carry private authority bookkeeping.  Pydantic's
    # normal nested coercion serializes that bookkeeping as extra fields, so
    # the comparison boundary intentionally accepts opaque slots and performs
    # the authoritative JSON rebuild in _rebuild_input/_rebuild_result.
    input: Any
    expected: Any = None
    predicted: Any = None


class RawExtractionComparisonSectionV1(_ComparisonModel):
    """The Phase 2 raw sections copied from the typed M4 report."""

    structural_raw_metrics: StructuralRawMetricsSectionV1
    optional_raw_gold_metrics: OptionalRawGoldMetricsSectionV1


class CanonicalMappingScopeMetricsV1(_ComparisonModel):
    """Canonical metrics for one immutable revision/job/unit scope."""

    scope_key: str
    revision_no: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=0)
    unique_claim_count: StrictInt = Field(ge=0)
    duplicate_occurrence_count: StrictInt = Field(ge=0)
    predicted_unique_mapping_count: StrictInt = Field(ge=0)
    gold_unique_mapping_count: StrictInt = Field(ge=0)
    matched_unique_mapping_count: StrictInt = Field(ge=0)
    extra_unique_mapping_count: StrictInt = Field(ge=0)
    missing_unique_mapping_count: StrictInt = Field(ge=0)
    ignored_decision_candidate_count: StrictInt = Field(ge=0)
    mapping_accuracy: CanonicalComparisonMetricV1
    mapping_coverage: CanonicalComparisonMetricV1
    unknown_predicate_retention: CanonicalComparisonMetricV1
    unknown_endpoint_retention: CanonicalComparisonMetricV1
    unknown_direction_retention: CanonicalComparisonMetricV1

    @field_validator("scope_key")
    @classmethod
    def validate_scope_key(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("scope_key must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> CanonicalMappingScopeMetricsV1:
        if self.unique_claim_count > self.occurrence_count:
            raise ValueError("unique claims cannot exceed occurrences")
        if self.duplicate_occurrence_count != self.occurrence_count - self.unique_claim_count:
            raise ValueError("duplicate occurrence count is not deterministic")
        if self.matched_unique_mapping_count > min(
            self.predicted_unique_mapping_count,
            self.gold_unique_mapping_count,
        ):
            raise ValueError("matched mappings exceed an input set")
        if self.extra_unique_mapping_count != (
            self.predicted_unique_mapping_count - self.matched_unique_mapping_count
        ):
            raise ValueError("extra mapping count is inconsistent")
        if self.missing_unique_mapping_count != (
            self.gold_unique_mapping_count - self.matched_unique_mapping_count
        ):
            raise ValueError("missing mapping count is inconsistent")
        return self


class CanonicalMappingMetricsV1(_ComparisonModel):
    """Independent canonical section; it is not a raw scorer extension."""

    schema_version: Literal["canonical_mapping_metrics_v1"] = "canonical_mapping_metrics_v1"
    scope_metrics: tuple[CanonicalMappingScopeMetricsV1, ...]
    overall: CanonicalMappingScopeMetricsV1
    mapping_accuracy: CanonicalComparisonMetricV1
    mapping_coverage: CanonicalComparisonMetricV1
    unavailable_reasons: tuple[str, ...] = ()

    @field_validator("unavailable_reasons", mode="before")
    @classmethod
    def validate_unavailable_reasons(cls, value: Any) -> tuple[str, ...]:
        return _validate_unavailable_reason_sequence(value)

    @model_validator(mode="after")
    def validate_overall_metrics(self) -> CanonicalMappingMetricsV1:
        if self.mapping_accuracy != self.overall.mapping_accuracy:
            raise ValueError("overall accuracy is not bound to the canonical section")
        if self.mapping_coverage != self.overall.mapping_coverage:
            raise ValueError("overall coverage is not bound to the canonical section")
        return self


class CanonicalMappingComparisonReportV1(_ComparisonModel):
    """A report with raw and canonical families that cannot be conflated."""

    schema_version: Literal["canonical_mapping_comparison_report_v1"] = (
        "canonical_mapping_comparison_report_v1"
    )
    raw_extraction_metrics: RawExtractionComparisonSectionV1
    canonical_mapping_metrics: CanonicalMappingMetricsV1
    artifact_binding: CanonicalMappingReplayArtifactBindingV1 | None = None
    artifact_binding_status: Literal["bound", "unbound_sidecar"] = "unbound_sidecar"
    _trusted_report_sha256: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_artifact_binding_state(self) -> CanonicalMappingComparisonReportV1:
        if (self.artifact_binding is None) != (self.artifact_binding_status == "unbound_sidecar"):
            raise ValueError("artifact binding status does not match binding")
        return self

    def to_claim_shadow_replay_report(
        self,
        artifact: Any,
        *,
        raw_gold: Any = None,
    ) -> ClaimShadowReplayReportV1:
        """Attach this independent section through the existing M4 report API."""

        try:
            trusted_digest = self._trusted_report_sha256
            if trusted_digest is None:
                raise CanonicalMappingComparisonError("comparison_report_untrusted")
            try:
                current_digest = _comparison_report_sha256(self)
            except CanonicalMappingComparisonError as exc:
                raise CanonicalMappingComparisonError("comparison_report_forged") from exc
            if current_digest != trusted_digest:
                raise CanonicalMappingComparisonError("comparison_report_forged")
            rebuilt_report = _rebuild_comparison_report(self)
            if rebuilt_report.artifact_binding is None:
                raise CanonicalMappingComparisonError("comparison_report_unbound")
            validated_artifact, artifact_binding = _load_replay_artifact_binding(artifact)
            if rebuilt_report.artifact_binding != artifact_binding:
                raise CanonicalMappingComparisonError("replay_artifact_identity_mismatch")
            return build_claim_shadow_replay_report(
                validated_artifact,
                raw_gold=raw_gold,
                canonical_metrics=rebuilt_report.canonical_mapping_metrics.model_dump(mode="json"),
            )
        except CanonicalMappingComparisonError:
            raise
        except (ClaimShadowReplayAssemblyError, ClaimShadowReplayLoadError, ValueError) as exc:
            raise CanonicalMappingComparisonError("replay_report_unavailable") from exc


class CanonicalMappingReplayArtifactBindingV1(_ComparisonModel):
    """M2-only identity for a validated M4 artifact, never an artifact field."""

    schema_version: Literal["canonical_mapping_replay_artifact_binding_v1"] = (
        "canonical_mapping_replay_artifact_binding_v1"
    )
    artifact_schema_version: Literal[
        "claim_shadow_replay_artifact_v1", "claim_shadow_replay_artifact_v2"
    ]
    artifact_id_sha256: str
    run_id_sha256: str
    artifact_json_sha256: str

    @field_validator("artifact_id_sha256", "run_id_sha256", "artifact_json_sha256", mode="before")
    @classmethod
    def validate_hash(cls, value: Any) -> str:
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError("artifact binding hash must be lowercase SHA-256")
        return value


@dataclass(frozen=True, slots=True)
class _Observation:
    scope_key: str
    revision_no: int
    core_key: tuple[str, str]
    relation_signature: str


@dataclass(slots=True)
class _ScopeAccumulator:
    revision_no: int
    occurrence_keys: set[tuple[tuple[str, str], str]] = field(default_factory=set)
    occurrence_ids: set[str] = field(default_factory=set)
    predicted: set[tuple[tuple[str, str], str]] = field(default_factory=set)
    gold: set[tuple[tuple[str, str], str]] = field(default_factory=set)
    decision_candidate_count: int = 0
    unknown_facts: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    retained_reasons: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))


def _canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_unavailable_reason(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("unavailable_reason must be a stable enum")
    if value not in _UNAVAILABLE_REASONS:
        raise ValueError("unavailable_reason must be a stable enum")
    return value


def _validate_unavailable_reason_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("unavailable reasons must be a sequence")
    validated = tuple(_validate_unavailable_reason(reason) for reason in value)
    if len(validated) != len(set(validated)):
        raise ValueError("unavailable reasons must be unique")
    return tuple(sorted(validated))


def _metric(value: float | None, reason: str | None = None) -> CanonicalComparisonMetricV1:
    return CanonicalComparisonMetricV1(value=value, unavailable_reason=reason)


def _ratio(numerator: int, denominator: int, *, empty_reason: str) -> CanonicalComparisonMetricV1:
    return _metric(numerator / denominator) if denominator else _metric(None, empty_reason)


def _scope_key(input_value: CanonicalMappingInputV1) -> str:
    payload = {
        "library_id": str(input_value.library_id),
        "document_id": str(input_value.document_id),
        "document_revision_id": str(input_value.document_revision_id),
        "revision_no": input_value.revision_no,
        "job_id": str(input_value.job_id),
        "extraction_unit_id": str(input_value.extraction_unit_id),
    }
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _rebuild_input(value: Any) -> CanonicalMappingInputV1:
    if not isinstance(value, CanonicalMappingInputV1):
        raise CanonicalMappingComparisonError("input_not_m1_typed")
    try:
        canonical = canonical_mapping_input_json(value)
        rebuilt = CanonicalMappingInputV1.from_raw_claim_json(
            value.raw_claim_authority_json,
            frozen_ontology=value.frozen_ontology,
            endpoint_type_binding=value.endpoint_type_binding,
            source_endpoint_resolution=value.source_endpoint_resolution,
            target_endpoint_resolution=value.target_endpoint_resolution,
            authorization_registry_snapshot=value.authorization_registry_snapshot,
            verified_evidence_ref_ids=value.verified_evidence_ref_ids,
            evidence_attestations={
                binding.evidence_ref_id: binding.attestation
                for binding in value.evidence_bindings
            },
            authorization_provenance=value.authorization_provenance,
            decision=value.decision,
        )
        if canonical_mapping_input_json(rebuilt) != canonical:
            raise ValueError("rebuilt input differs from authoritative input")
        return rebuilt
    except Exception as exc:
        raise CanonicalMappingComparisonError("input_contract_invalid") from exc


def _rebuild_result(
    input_value: CanonicalMappingInputV1,
    value: Any,
) -> CanonicalMappingV1 | None:
    if value is None:
        return None
    if not isinstance(value, CanonicalMappingV1):
        raise CanonicalMappingComparisonError("output_not_m1_typed")
    try:
        result = CanonicalMappingV1.model_validate(
            json.loads(_canonical_payload(value.model_dump(mode="json")))
        )
        result_bound_input = CanonicalMappingInputV1.from_raw_claim_json(
            input_value.raw_claim_authority_json,
            frozen_ontology=input_value.frozen_ontology,
            endpoint_type_binding=input_value.endpoint_type_binding,
            source_endpoint_resolution=input_value.source_endpoint_resolution,
            target_endpoint_resolution=input_value.target_endpoint_resolution,
            authorization_registry_snapshot=input_value.authorization_registry_snapshot,
            verified_evidence_ref_ids=input_value.verified_evidence_ref_ids,
            evidence_attestations={
                binding.evidence_ref_id: binding.attestation
                for binding in result.evidence_bindings
            },
            authorization_provenance=input_value.authorization_provenance,
            decision=input_value.decision,
        )
        return CanonicalMappingV1.model_validate(
            json.loads(canonical_mapping_json(result_bound_input, result))
        )
    except Exception as exc:
        raise CanonicalMappingComparisonError("output_contract_invalid") from exc


def _rebuild_comparison_report(
    value: CanonicalMappingComparisonReportV1,
) -> CanonicalMappingComparisonReportV1:
    if not isinstance(value, CanonicalMappingComparisonReportV1):
        raise CanonicalMappingComparisonError("comparison_report_invalid")
    try:
        encoded = _canonical_payload(value.model_dump(mode="json"))
        return CanonicalMappingComparisonReportV1.model_validate(json.loads(encoded))
    except CanonicalMappingComparisonError:
        raise
    except Exception as exc:
        raise CanonicalMappingComparisonError("comparison_report_invalid") from exc


def _comparison_report_sha256(value: CanonicalMappingComparisonReportV1) -> str:
    try:
        encoded = _canonical_payload(value.model_dump(mode="json"))
    except Exception as exc:
        raise CanonicalMappingComparisonError("comparison_report_invalid") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_replay_artifact_binding(
    value: Any,
) -> tuple[ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2, CanonicalMappingReplayArtifactBindingV1]:
    try:
        loaded = load_claim_shadow_replay_artifact(value)
        artifact = loaded.artifact
        if isinstance(artifact, ClaimShadowReplayArtifactV2):
            canonical = canonical_claim_shadow_replay_v2_json(artifact)
        else:
            canonical = canonical_claim_shadow_replay_json(artifact)
        binding = CanonicalMappingReplayArtifactBindingV1(
            artifact_schema_version=artifact.schema_version,
            artifact_id_sha256=artifact.artifact_id_sha256,
            run_id_sha256=artifact.run_id_sha256,
            artifact_json_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        return artifact, binding
    except (ClaimShadowReplayLoadError, ValueError, TypeError) as exc:
        raise CanonicalMappingComparisonError("raw_artifact_invalid") from exc


def _relation_signature(result: CanonicalMappingV1) -> str:
    def endpoint_signature(endpoint: Any) -> dict[str, Any]:
        resolution = endpoint.resolution_attestation
        link = endpoint.entity_link
        return {
            "role": endpoint.role,
            "entity_type_key": endpoint.entity_type_key,
            "mention_surface_sha256": resolution.mention_surface_sha256,
            "entity_link": {
                "status": link.status,
                "entity_id": str(link.entity_id) if link.entity_id is not None else None,
                "entity_candidate_key_hash": link.entity_candidate_key_hash,
            },
        }

    payload = {
        "canonical_relation_key": result.canonical_relation_key,
        "canonical_direction": result.canonical_direction,
        "endpoint_transform": result.endpoint_transform,
        "predicate_transform": result.predicate_transform,
        "canonical_source_endpoint": endpoint_signature(result.canonical_source_endpoint),
        "canonical_target_endpoint": endpoint_signature(result.canonical_target_endpoint),
        "verified_evidence_identity_hashes": result.verified_evidence_identity_hashes,
        "semantic_projection_fingerprint": result.semantic_projection.semantic_projection_fingerprint,
    }
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _unknown_fact_reasons(input_value: CanonicalMappingInputV1) -> tuple[str, ...]:
    reasons: list[str] = []
    if input_value.surface_raw_predicate not in input_value.frozen_ontology.relation_type_keys:
        reasons.append("unknown_predicate")
    if input_value.endpoint_type_binding.source_type_key is None:
        reasons.append("unknown_source_type")
    if input_value.endpoint_type_binding.target_type_key is None:
        reasons.append("unknown_target_type")
    if input_value.surface_direction == "unknown":
        reasons.append("unknown_direction")
    return tuple(reasons)


def _is_canonical_mapping(
    input_value: CanonicalMappingInputV1,
    result: CanonicalMappingV1 | None,
) -> bool:
    return bool(
        result is not None
        and result.outcome == "mapped"
        and result.decision_kind is None
        and input_value.decision_kind is None
    )


def _scope_metrics(
    scope_key: str,
    accumulator: _ScopeAccumulator,
) -> CanonicalMappingScopeMetricsV1:
    predicted = accumulator.predicted
    gold = accumulator.gold
    matched = predicted & gold
    unknown_denominators = {
        "unknown_predicate": sum(
            "unknown_predicate" in facts for facts in accumulator.unknown_facts.values()
        ),
        "unknown_endpoint": sum(
            bool(facts & {"unknown_source_type", "unknown_target_type"})
            for facts in accumulator.unknown_facts.values()
        ),
        "unknown_direction": sum(
            "unknown_direction" in facts for facts in accumulator.unknown_facts.values()
        ),
    }

    def retention(
        reasons: set[str],
        *,
        denominator_key: str,
        retained: Callable[[set[str]], bool],
    ) -> CanonicalComparisonMetricV1:
        denominator = unknown_denominators[denominator_key]
        if denominator == 0:
            return _metric(None, f"no_{denominator_key}_diagnostic")
        numerator = sum(
            1
            for core, facts in accumulator.unknown_facts.items()
            if facts & reasons and retained(accumulator.retained_reasons.get(core, set()))
        )
        return _ratio(numerator, denominator, empty_reason=f"no_{denominator_key}_diagnostic")

    endpoint_reasons = {"unknown_source_type", "unknown_target_type"}
    return CanonicalMappingScopeMetricsV1(
        scope_key=scope_key,
        revision_no=accumulator.revision_no,
        occurrence_count=len(accumulator.occurrence_keys),
        unique_claim_count=len({core for core, _ in accumulator.occurrence_keys}),
        duplicate_occurrence_count=len(accumulator.occurrence_keys)
        - len({core for core, _ in accumulator.occurrence_keys}),
        predicted_unique_mapping_count=len(predicted),
        gold_unique_mapping_count=len(gold),
        matched_unique_mapping_count=len(matched),
        extra_unique_mapping_count=len(predicted - gold),
        missing_unique_mapping_count=len(gold - predicted),
        ignored_decision_candidate_count=accumulator.decision_candidate_count,
        mapping_accuracy=_ratio(
            len(matched),
            len(predicted),
            empty_reason="no_predicted_canonical_mappings",
        ),
        mapping_coverage=_ratio(
            len(matched),
            len(gold),
            empty_reason="no_gold_canonical_mappings",
        ),
        unknown_predicate_retention=retention(
            {"unknown_predicate"},
            denominator_key="unknown_predicate",
            retained=lambda values: "unknown_predicate" in values,
        ),
        unknown_endpoint_retention=retention(
            {"unknown_source_type", "unknown_target_type"},
            denominator_key="unknown_endpoint",
            retained=lambda values: bool(values & endpoint_reasons),
        ),
        unknown_direction_retention=retention(
            {"unknown_direction"},
            denominator_key="unknown_direction",
            retained=lambda values: "unknown_direction" in values,
        ),
    )


def _raw_metrics_section(
    artifact: Any,
    raw_gold: Any,
) -> RawExtractionComparisonSectionV1:
    if artifact is None:
        reason = "raw_artifact_not_provided"
        return RawExtractionComparisonSectionV1(
            structural_raw_metrics=StructuralRawMetricsSectionV1(unavailable_reason=reason),
            optional_raw_gold_metrics=OptionalRawGoldMetricsSectionV1(
                unavailable_reason=reason if raw_gold is not None else "raw_gold_not_provided"
            ),
        )
    try:
        report = build_claim_shadow_replay_report(
            artifact,
            raw_gold=raw_gold,
        )
    except ClaimShadowReplayAssemblyError as exc:
        raise CanonicalMappingComparisonError("raw_artifact_invalid") from exc
    try:
        payload = {
            "structural_raw_metrics": report.structural_raw_metrics.model_dump(mode="json"),
            "optional_raw_gold_metrics": report.optional_raw_gold_metrics.model_dump(mode="json"),
        }
        return RawExtractionComparisonSectionV1.model_validate(
            json.loads(_canonical_payload(payload))
        )
    except Exception as exc:
        raise CanonicalMappingComparisonError("raw_section_invalid") from exc


def _stable_input_identity_projection(input_value: CanonicalMappingInputV1) -> dict[str, Any]:
    return {
        "mapping_schema_hash": input_value.mapping_schema_hash,
        "canonical_schema_hash": input_value.canonical_schema_hash,
        "scope": {
            "library_id": str(input_value.library_id),
            "document_id": str(input_value.document_id),
            "document_revision_id": str(input_value.document_revision_id),
            "revision_no": input_value.revision_no,
            "job_id": str(input_value.job_id),
            "extraction_unit_id": str(input_value.extraction_unit_id),
            "claim_id": str(input_value.claim_id),
        },
        "claim_content_scoped_fingerprint": input_value.claim.content_scoped_claim_fingerprint,
        "extraction_occurrence_id": str(input_value.extraction_occurrence_id),
        "extraction_occurrence_fingerprint": input_value.claim.extraction_occurrence_fingerprint,
    }


def _stable_result_identity_projection(
    input_value: CanonicalMappingInputV1,
    result: CanonicalMappingV1 | None,
) -> dict[str, Any]:
    if result is None:
        return {"presence": "absent"}
    return {
        "presence": "present",
        "schema_version": result.schema_version,
        "mapping_schema_hash": result.mapping_schema_hash,
        "canonical_schema_hash": result.canonical_schema_hash,
        "mapping_version": result.mapping_version,
        "mapping_attempt_fingerprint": result.mapping_attempt_fingerprint,
        "mapping_result_fingerprint": result.mapping_result_fingerprint,
        "outcome": result.outcome,
        "semantic_status": result.semantic_status,
        "reason_code": result.reason_code,
        "decision_kind": result.decision_kind,
        "relation_signature": (
            _relation_signature(result)
            if _is_canonical_mapping(input_value, result)
            else None
        ),
    }


def _comparison_case_signature(
    input_value: CanonicalMappingInputV1,
    expected: CanonicalMappingV1 | None,
    predicted: CanonicalMappingV1 | None,
) -> str:
    payload = {
        "schema_version": "canonical_mapping_comparison_occurrence_identity_v1",
        "input": _stable_input_identity_projection(input_value),
        "expected": _stable_result_identity_projection(input_value, expected),
        "predicted": _stable_result_identity_projection(input_value, predicted),
    }
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def compare_canonical_mappings(
    cases: Sequence[CanonicalMappingComparisonCaseV1],
    *,
    replay_artifact: Any = None,
    raw_gold: Any = None,
    unavailable_reasons: Sequence[str] = (),
) -> CanonicalMappingComparisonReportV1:
    """Compare explicit mapper outputs without invoking a mapper or provider."""

    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
        raise CanonicalMappingComparisonError("cases_input_invalid")
    if isinstance(unavailable_reasons, (str, bytes)) or not isinstance(unavailable_reasons, Sequence):
        raise CanonicalMappingComparisonError("unavailable_reasons_input_invalid")
    try:
        stable_unavailable = _validate_unavailable_reason_sequence(unavailable_reasons)
    except (TypeError, ValueError) as exc:
        raise CanonicalMappingComparisonError("unavailable_reason_invalid") from exc

    validated_artifact: ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2 | None = None
    artifact_binding: CanonicalMappingReplayArtifactBindingV1 | None = None
    if replay_artifact is not None:
        validated_artifact, artifact_binding = _load_replay_artifact_binding(replay_artifact)

    scopes: dict[str, _ScopeAccumulator] = {}
    occurrence_bindings: dict[str, tuple[tuple[str, str, str, str], str]] = {}
    for case in cases:
        if not isinstance(case, CanonicalMappingComparisonCaseV1):
            raise CanonicalMappingComparisonError("case_not_typed")
        input_value = _rebuild_input(case.input)
        expected = _rebuild_result(input_value, case.expected)
        predicted = _rebuild_result(input_value, case.predicted)
        scope_key = _scope_key(input_value)
        occurrence_id = str(input_value.extraction_occurrence_id)
        occurrence_binding = (
            scope_key,
            str(input_value.claim_id),
            input_value.claim.content_scoped_claim_fingerprint,
            input_value.claim.extraction_occurrence_fingerprint,
        )
        case_signature = _comparison_case_signature(input_value, expected, predicted)
        previous = occurrence_bindings.get(occurrence_id)
        if previous is not None:
            previous_binding, previous_signature = previous
            if previous_binding != occurrence_binding:
                raise CanonicalMappingComparisonError("occurrence_identity_conflict")
            if previous_signature == case_signature:
                continue
            raise CanonicalMappingComparisonError("duplicate_occurrence_input")
        occurrence_bindings[occurrence_id] = (occurrence_binding, case_signature)
        accumulator = scopes.setdefault(scope_key, _ScopeAccumulator(input_value.revision_no))
        core_key = (scope_key, input_value.claim.content_scoped_claim_fingerprint)
        accumulator.occurrence_ids.add(occurrence_id)
        occurrence_key = (core_key, occurrence_id)
        accumulator.occurrence_keys.add(occurrence_key)
        accumulator.unknown_facts[core_key].update(_unknown_fact_reasons(input_value))

        for result, destination in ((expected, accumulator.gold), (predicted, accumulator.predicted)):
            if _is_canonical_mapping(input_value, result):
                observation = _Observation(
                    scope_key=scope_key,
                    revision_no=input_value.revision_no,
                    core_key=core_key,
                    relation_signature=_relation_signature(result),
                )
                destination.add((observation.core_key, observation.relation_signature))
            elif result is not None and result.decision_kind is not None:
                accumulator.decision_candidate_count += 1

        if predicted is not None:
            accumulator.retained_reasons[core_key].add(predicted.reason_code or "mapped")

    scope_rows = tuple(
        _scope_metrics(scope_key, scopes[scope_key])
        for scope_key in sorted(scopes)
    )
    overall = _ScopeAccumulator(revision_no=0)
    for accumulator in scopes.values():
        overall.occurrence_keys.update(accumulator.occurrence_keys)
        overall.occurrence_ids.update(accumulator.occurrence_ids)
        overall.predicted.update(accumulator.predicted)
        overall.gold.update(accumulator.gold)
        overall.decision_candidate_count += accumulator.decision_candidate_count
        for core, facts in accumulator.unknown_facts.items():
            overall.unknown_facts[core].update(facts)
        for core, reasons in accumulator.retained_reasons.items():
            overall.retained_reasons[core].update(reasons)
    overall_row = _scope_metrics(_OVERALL_SCOPE_KEY, overall)
    metrics = CanonicalMappingMetricsV1(
        scope_metrics=scope_rows,
        overall=overall_row,
        mapping_accuracy=overall_row.mapping_accuracy,
        mapping_coverage=overall_row.mapping_coverage,
        unavailable_reasons=stable_unavailable,
    )
    report = CanonicalMappingComparisonReportV1(
        raw_extraction_metrics=_raw_metrics_section(
            validated_artifact,
            raw_gold,
        ),
        canonical_mapping_metrics=metrics,
        artifact_binding=artifact_binding,
        artifact_binding_status="bound" if artifact_binding is not None else "unbound_sidecar",
    )
    object.__setattr__(report, "_trusted_report_sha256", _comparison_report_sha256(report))
    return report


__all__ = [
    "CanonicalComparisonMetricV1",
    "CanonicalMappingComparisonCaseV1",
    "CanonicalMappingComparisonError",
    "CanonicalMappingComparisonReportV1",
    "CanonicalMappingReplayArtifactBindingV1",
    "CanonicalMappingMetricsV1",
    "CanonicalMappingScopeMetricsV1",
    "RawExtractionComparisonSectionV1",
    "compare_canonical_mappings",
]
