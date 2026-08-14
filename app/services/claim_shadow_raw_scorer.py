"""Strict, DB-free raw gold scorer for Claim Shadow Replay Artifact v2."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.claim_shadow_raw_gold import RawGoldAnnotationV1, RawGoldFixtureV1, raw_gold_anchor_v1
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    RedactedRawClaimV2,
    validate_claim_shadow_replay_artifact_any,
)


class RawGoldScoringError(ValueError):
    """Stable raw-gold scoring failure without payload or exception leakage."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ScoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawGoldMetricV1(_ScoreModel):
    value: int | float | None = None
    unavailable_reason: str | None = None

    @field_validator("unavailable_reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("unavailable_reason must be bounded")
        if not value.replace("_", "").isalnum():
            raise ValueError("unavailable_reason must be a stable code")
        return value

    @model_validator(mode="after")
    def validate_metric(self) -> RawGoldMetricV1:
        if self.value is None and self.unavailable_reason is None:
            raise ValueError("unavailable metric requires a reason")
        if self.value is not None and self.unavailable_reason is not None:
            raise ValueError("available metric cannot have a reason")
        return self


class RawGoldScopeScoreV1(_ScoreModel):
    scope_id: str
    predicted_unique_claim_count: StrictInt = Field(ge=0)
    gold_unique_claim_count: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=0)
    duplicate_occurrence_count: StrictInt = Field(ge=0)
    raw_relation_precision: RawGoldMetricV1
    raw_relation_recall: RawGoldMetricV1
    source_endpoint_accuracy: RawGoldMetricV1
    target_endpoint_accuracy: RawGoldMetricV1
    surface_direction_accuracy: RawGoldMetricV1
    evidence_accuracy: RawGoldMetricV1
    locator_accuracy: RawGoldMetricV1
    negation_accuracy: RawGoldMetricV1
    modality_accuracy: RawGoldMetricV1
    qualifier_accuracy: RawGoldMetricV1
    valid_time_accuracy: RawGoldMetricV1
    effective_time_accuracy: RawGoldMetricV1
    unknown_predicate_retention: RawGoldMetricV1
    unknown_endpoint_retention: RawGoldMetricV1
    unknown_direction_retention: RawGoldMetricV1
    canonicalization_coverage: RawGoldMetricV1


class RawGoldScoreV1(_ScoreModel):
    schema_version: Literal["claim_shadow_raw_score_v1"] = "claim_shadow_raw_score_v1"
    artifact_schema_version: Literal["claim_shadow_replay_artifact_v2"] = "claim_shadow_replay_artifact_v2"
    gold_schema_version: Literal["claim_shadow_raw_gold_v1"] = "claim_shadow_raw_gold_v1"
    scope_scores: tuple[RawGoldScopeScoreV1, ...]
    overall: RawGoldScopeScoreV1


def _metric(value: int | float | None, reason: str | None = None) -> RawGoldMetricV1:
    return RawGoldMetricV1(value=value, unavailable_reason=reason)


def _ratio(numerator: int, denominator: int, *, reason: str) -> RawGoldMetricV1:
    return _metric(numerator / denominator) if denominator else _metric(None, reason)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _relation_core(claim: RedactedRawClaimV2) -> tuple[Any, ...]:
    return (
        claim.source_surface_sha256,
        claim.source_type_hint_present,
        claim.source_type_hint_sha256,
        claim.target_surface_sha256,
        claim.target_type_hint_present,
        claim.target_type_hint_sha256,
        claim.raw_predicate_sha256,
        claim.direction,
        claim.negation,
        claim.modality_value_sha256,
        _canonical([item.model_dump(mode="json") for item in claim.qualifiers]),
        _canonical(claim.valid_time.model_dump(mode="json") if claim.valid_time else None),
        _canonical(claim.effective_time.model_dump(mode="json") if claim.effective_time else None),
    )


def _endpoint_equal(left: RedactedRawClaimV2, right: RedactedRawClaimV2, *, source: bool) -> bool:
    fields = (
        ("source_surface_sha256", "source_type_hint_present", "source_type_hint_sha256")
        if source
        else ("target_surface_sha256", "target_type_hint_present", "target_type_hint_sha256")
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _evidence_map(claim: RedactedRawClaimV2) -> dict[str, Any]:
    return {item.evidence_ref_sha256: item for item in claim.evidence_refs}


def _evidence_equal(left: RedactedRawClaimV2, right: RedactedRawClaimV2) -> bool:
    left_map = _evidence_map(left)
    right_map = _evidence_map(right)
    if set(left_map) != set(right_map):
        return False
    if any(
        getattr(left, field) != getattr(right, field)
        for field in (
            "source_evidence_ref_sha256",
            "target_evidence_ref_sha256",
            "negation_evidence_ref_sha256",
            "modality_evidence_ref_sha256",
        )
    ):
        return False
    if tuple(item.evidence_ref_sha256 for item in left.qualifiers) != tuple(
        item.evidence_ref_sha256 for item in right.qualifiers
    ):
        return False
    if tuple(
        item.evidence_ref_sha256 for item in (left.valid_time, left.effective_time) if item is not None
    ) != tuple(
        item.evidence_ref_sha256 for item in (right.valid_time, right.effective_time) if item is not None
    ):
        return False
    return all(
        left_map[key].quote_sha256 == right_map[key].quote_sha256
        and left_map[key].unit_text_sha256 == right_map[key].unit_text_sha256
        for key in left_map
    )


def _locator_equal(left: RedactedRawClaimV2, right: RedactedRawClaimV2) -> bool:
    left_map = _evidence_map(left)
    right_map = _evidence_map(right)
    if set(left_map) != set(right_map):
        return False
    return all(
        (
            left_map[key].locator_present,
            left_map[key].locator_valid,
            left_map[key].source_span_valid,
        )
        == (
            right_map[key].locator_present,
            right_map[key].locator_valid,
            right_map[key].source_span_valid,
        )
        for key in left_map
    )


def _dedupe_predictions(
    artifact: ClaimShadowReplayArtifactV2,
) -> tuple[list[RedactedRawClaimV2], int, dict[str, set[str]]]:
    groups: dict[tuple[str, str], list[RedactedRawClaimV2]] = {}
    for claim in artifact.raw_claims:
        groups.setdefault((claim.scope_id, claim.content_fingerprint), []).append(claim)
    unique: list[RedactedRawClaimV2] = []
    duplicate_count = 0
    for rows in groups.values():
        first = rows[0]
        comparable = first.model_dump(mode="json", exclude={"claim_id_sha256", "content_fingerprint"})
        if any(
            _canonical(row.model_dump(mode="json", exclude={"claim_id_sha256", "content_fingerprint"}))
            != _canonical(comparable)
            for row in rows[1:]
        ):
            raise RawGoldScoringError("duplicate_content_fingerprint_conflict")
        unique.append(first)
        duplicate_count += len(rows) - 1
    by_anchor: dict[str, list[RedactedRawClaimV2]] = {}
    for claim in unique:
        by_anchor.setdefault(raw_gold_anchor_v1(claim), []).append(claim)
    if any(len(rows) > 1 for rows in by_anchor.values()):
        raise RawGoldScoringError("prediction_anchor_ambiguous")
    decision_reasons: dict[str, set[str]] = {}
    decisions_by_claim = {}
    for decision in artifact.decisions:
        decisions_by_claim.setdefault(decision.claim_id_sha256, set()).add(decision.reason_code)
    for claim in unique:
        decision_reasons[raw_gold_anchor_v1(claim)] = decisions_by_claim.get(claim.claim_id_sha256, set())
    return unique, duplicate_count, decision_reasons


def _dedupe_gold(gold: RawGoldFixtureV1) -> list[RawGoldAnnotationV1]:
    groups: dict[tuple[str, str], list[RawGoldAnnotationV1]] = {}
    for annotation in gold.annotations:
        key = (annotation.claim.scope_id, annotation.claim.content_fingerprint)
        groups.setdefault(key, []).append(annotation)
    if any(len(rows) > 1 for rows in groups.values()):
        raise RawGoldScoringError("gold_duplicate_content_claim")
    return [rows[0] for rows in groups.values()]


def _retention(
    selected_gold: Iterable[RawGoldAnnotationV1],
    predictions: Mapping[str, RedactedRawClaimV2],
    *,
    predicted_flags: Mapping[str, bool] | None,
    unavailable_reason: str,
) -> RawGoldMetricV1:
    selected = list(selected_gold)
    if not selected:
        return _metric(None, "gold_unknown_denominator_zero")
    if predicted_flags is None:
        return _metric(None, unavailable_reason)
    retained = sum(
        1
        for row in selected
        if raw_gold_anchor_v1(row.claim) in predictions
        and predicted_flags.get(raw_gold_anchor_v1(row.claim), False)
    )
    return _ratio(retained, len(selected), reason="gold_unknown_denominator_zero")


def _score_scope(
    *,
    scope_id: str,
    predictions: list[RedactedRawClaimV2],
    gold: list[RawGoldAnnotationV1],
    decision_reasons: Mapping[str, set[str]],
    occurrence_count: int,
    duplicate_occurrence_count: int,
) -> RawGoldScopeScoreV1:
    prediction_by_anchor = {raw_gold_anchor_v1(row): row for row in predictions}
    gold_by_anchor = {raw_gold_anchor_v1(row.claim): row for row in gold}
    matched = [
        (gold_by_anchor[anchor].claim, prediction_by_anchor[anchor])
        for anchor in sorted(set(gold_by_anchor) & set(prediction_by_anchor))
    ]
    exact_relation_count = sum(_relation_core(left) == _relation_core(right) for left, right in matched)
    endpoint_pairs = len(matched)
    pred_unknown_predicate = (
        {
            anchor: "unknown_predicate" in decision_reasons.get(anchor, set())
            for anchor in prediction_by_anchor
        }
        if any("unknown_predicate" in reasons for reasons in decision_reasons.values())
        else None
    )
    pred_unknown_endpoint = {
        anchor: (not claim.source_type_hint_present or not claim.target_type_hint_present)
        for anchor, claim in prediction_by_anchor.items()
    }
    return RawGoldScopeScoreV1(
        scope_id=scope_id,
        predicted_unique_claim_count=len(predictions),
        gold_unique_claim_count=len(gold),
        occurrence_count=occurrence_count,
        duplicate_occurrence_count=duplicate_occurrence_count,
        raw_relation_precision=_ratio(exact_relation_count, len(predictions), reason="no_predicted_claims"),
        raw_relation_recall=_ratio(exact_relation_count, len(gold), reason="no_gold_claims"),
        source_endpoint_accuracy=_ratio(
            sum(_endpoint_equal(left, right, source=True) for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        target_endpoint_accuracy=_ratio(
            sum(_endpoint_equal(left, right, source=False) for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        surface_direction_accuracy=_ratio(
            sum(left.direction == right.direction for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        evidence_accuracy=_ratio(
            sum(_evidence_equal(left, right) for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        locator_accuracy=_ratio(
            sum(_locator_equal(left, right) for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        negation_accuracy=_ratio(
            sum(left.negation == right.negation for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        modality_accuracy=_ratio(
            sum(left.modality_value_sha256 == right.modality_value_sha256 for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        qualifier_accuracy=_ratio(
            sum(left.qualifiers == right.qualifiers for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        valid_time_accuracy=_ratio(
            sum(left.valid_time == right.valid_time for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        effective_time_accuracy=_ratio(
            sum(left.effective_time == right.effective_time for left, right in matched),
            endpoint_pairs,
            reason="no_anchor_matches",
        ),
        unknown_predicate_retention=_retention(
            [row for row in gold if row.unknown_predicate],
            prediction_by_anchor,
            predicted_flags=pred_unknown_predicate,
            unavailable_reason="no_unknown_predicate_diagnostic",
        ),
        unknown_endpoint_retention=_retention(
            [
                row
                for row in gold
                if row.unknown_source_endpoint or row.unknown_target_endpoint
            ],
            prediction_by_anchor,
            predicted_flags=pred_unknown_endpoint,
            unavailable_reason="no_unknown_endpoint_diagnostic",
        ),
        unknown_direction_retention=_retention(
            [row for row in gold if row.claim.direction == "unknown"],
            prediction_by_anchor,
            predicted_flags={
                anchor: claim.direction == "unknown"
                for anchor, claim in prediction_by_anchor.items()
            },
            unavailable_reason="no_unknown_direction_diagnostic",
        ),
        canonicalization_coverage=_metric(None, "no_canonical_mapping_observation"),
    )


def score_claim_shadow_raw_gold(
    artifact: ClaimShadowReplayArtifactV2
    | Mapping[str, Any],
    gold: RawGoldFixtureV1 | Mapping[str, Any],
) -> RawGoldScoreV1:
    try:
        if isinstance(artifact, (ClaimShadowReplayArtifactV1, ClaimShadowReplayArtifactV2)):
            validated_artifact = artifact
        else:
            validated_artifact = validate_claim_shadow_replay_artifact_any(artifact)
    except Exception as exc:
        raise RawGoldScoringError("artifact_contract_invalid") from exc
    if isinstance(validated_artifact, ClaimShadowReplayArtifactV1):
        raise RawGoldScoringError("unsupported_schema_for_raw_gold_scoring")
    if validated_artifact.status != "success":
        raise RawGoldScoringError("failed_artifact_not_scored")
    try:
        validated_gold = gold if isinstance(gold, RawGoldFixtureV1) else RawGoldFixtureV1.model_validate(gold)
    except Exception as exc:
        raise RawGoldScoringError("raw_gold_contract_invalid") from exc
    gold_rows = _dedupe_gold(validated_gold)
    predictions, duplicate_count, decision_reasons = _dedupe_predictions(validated_artifact)
    scope_ids = sorted({row.scope_id for row in predictions} | {row.claim.scope_id for row in gold_rows})
    scope_scores = []
    for scope_id in scope_ids:
        scope_predictions = [row for row in predictions if row.scope_id == scope_id]
        scope_gold = [row for row in gold_rows if row.claim.scope_id == scope_id]
        scope_scores.append(
            _score_scope(
                scope_id=scope_id,
                predictions=scope_predictions,
                gold=scope_gold,
                decision_reasons=decision_reasons,
                occurrence_count=sum(row.scope_id == scope_id for row in validated_artifact.occurrences),
                duplicate_occurrence_count=sum(
                    row.scope_id == scope_id for row in validated_artifact.occurrences
                )
                - len(scope_predictions),
            )
        )
    overall = _score_scope(
        scope_id="overall",
        predictions=predictions,
        gold=gold_rows,
        decision_reasons=decision_reasons,
        occurrence_count=len(validated_artifact.occurrences),
        duplicate_occurrence_count=max(0, len(validated_artifact.occurrences) - len(predictions)),
    )
    return RawGoldScoreV1(scope_scores=tuple(scope_scores), overall=overall)
