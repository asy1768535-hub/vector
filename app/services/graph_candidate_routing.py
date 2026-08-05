from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select

from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_review import GraphExtractionConflict


_WEIGHTS_V1 = {"model": 0.25, "evidence": 0.35, "schema": 0.25, "normalization": 0.15}
_EVIDENCE_SCORES_V1 = {"direct_statement": 1.0, "table_cell": 0.95}
_SCHEMA_SCORES_V1 = {"valid": 1.0, "warning": 0.6, "boundary_unclear": 0.4, "invalid": 0.0}
_NORMALIZATION_SCORES_V1 = {
    "exact_normalized_match": 1.0,
    "exact_alias_match": 0.95,
    "new_entity": 0.9,
    "ambiguous": 0.0,
}
_CONCEPTUAL_ENTITY_TYPE_KEYS = frozenset({"concept", "term"})
_GENERIC_ENTITY_NAMES = frozenset({"重大", "较大", "稳定性", "概况", "安排", "处理"})
_DATE_ONLY_NAME = re.compile(
    r"^(?:\d{4}[-/.年])?\d{1,2}[-/.月]\d{1,2}(?:日)?$"
)
_IP_ADDRESS_NAME = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$")
_SECTION_HEADING_NAME = re.compile(r"^\d+(?:\.\d+)*\s+\S+")
_GENERIC_ACTION_NAMES = frozenset(
    {
        "\u64cd\u4f5c\u5b89\u6392",
        "\u5de5\u4f5c\u5b89\u6392",
        "\u5b9e\u65bd\u5b89\u6392",
        "\u8be6\u7ec6\u8bbe\u8ba1\u4e0e\u7f16\u7801\u8854\u63a5",
    }
)


class ConfidencePolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ConfidencePolicyV1:
    entity_materialization_threshold: float = 0.85
    relation_draft_threshold: float = 0.85
    candidate_review_policy: str = "manual_review"


@dataclass(frozen=True)
class CandidateRoute:
    status: str
    review_reason: str | None


@dataclass(frozen=True)
class JobCandidateRouteResult:
    validated_count: int
    pending_review_count: int
    rejected_count: int
    entity_status_counts: dict[str, int]
    relation_status_counts: dict[str, int]
    failure_reasons: dict[str, dict[str, int]]


def _bounded_component(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfidencePolicyError("invalid_confidence_component", f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise ConfidencePolicyError(
            "invalid_confidence_component", f"{label} must be finite and between 0 and 1"
        )
    return result


def compute_final_confidence_v1(
    *,
    model: float,
    evidence: float,
    schema: float,
    normalization: float,
) -> float:
    components = {
        "model": _bounded_component(model, label="model confidence"),
        "evidence": _bounded_component(evidence, label="evidence score"),
        "schema": _bounded_component(schema, label="schema score"),
        "normalization": _bounded_component(normalization, label="normalization score"),
    }
    return sum(components[key] * weight for key, weight in _WEIGHTS_V1.items())


def _require_exact_mapping(snapshot: dict[str, Any], key: str, expected: dict[str, float]) -> None:
    if snapshot.get(key) != expected:
        raise ConfidencePolicyError(
            "invalid_confidence_policy", f"v1 policy {key} does not match the frozen contract"
        )


def load_confidence_policy_v1(job: Any) -> ConfidencePolicyV1:
    if getattr(job, "confidence_policy_version", None) != "v1":
        raise ConfidencePolicyError(
            "unsupported_confidence_policy", "Job confidence policy must be v1"
        )
    snapshot = getattr(job, "policy_config_snapshot", None)
    if not isinstance(snapshot, dict):
        raise ConfidencePolicyError(
            "invalid_confidence_policy", "Job policy snapshot must be an object"
        )
    _require_exact_mapping(snapshot, "confidence_weights", _WEIGHTS_V1)
    _require_exact_mapping(snapshot, "evidence_score_map", _EVIDENCE_SCORES_V1)
    _require_exact_mapping(snapshot, "schema_score_map", _SCHEMA_SCORES_V1)
    _require_exact_mapping(snapshot, "normalization_score_map", _NORMALIZATION_SCORES_V1)
    entity_threshold = snapshot.get("entity_materialization_threshold")
    relation_threshold = snapshot.get("relation_draft_threshold")
    if entity_threshold != 0.85 or relation_threshold != 0.85:
        raise ConfidencePolicyError(
            "invalid_confidence_policy", "v1 policy thresholds must both be 0.85"
        )
    if snapshot.get("evidence_group_policy") != "all_claims_valid":
        raise ConfidencePolicyError(
            "invalid_confidence_policy", "v1 Evidence Group policy must be all_claims_valid"
        )
    candidate_review_policy = snapshot.get("candidate_review_policy", "manual_review")
    if candidate_review_policy not in {"manual_review", "precision_first_auto"}:
        raise ConfidencePolicyError(
            "invalid_confidence_policy",
            "v1 Candidate review policy is not supported",
        )
    return ConfidencePolicyV1(candidate_review_policy=candidate_review_policy)


def route_entity_candidate_v1(
    *,
    schema_invalid: bool,
    evidence_invalid: bool,
    evidence_ambiguous: bool,
    has_open_conflict: bool,
    normalization_method: str,
    matched_entity_id: Any | None,
    final_confidence: float,
    threshold: float = 0.85,
    automatic: bool = False,
    generic_name: bool = False,
    conceptual: bool = False,
    repeated_evidence: bool = True,
) -> CandidateRoute:
    if schema_invalid:
        return CandidateRoute("rejected", "schema_invalid")
    if evidence_invalid:
        return CandidateRoute("rejected", "evidence_invalid")
    if generic_name:
        return CandidateRoute("rejected", "generic_entity_name")
    if evidence_ambiguous:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "evidence_ambiguous",
        )
    if normalization_method == "ambiguous":
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "entity_match_ambiguous",
        )
    if has_open_conflict:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "conflict_open",
        )
    if conceptual and not repeated_evidence:
        return CandidateRoute("pending_review", "concept_single_occurrence")
    if matched_entity_id is not None and not automatic:
        return CandidateRoute("validated", None)
    _bounded_component(final_confidence, label="final confidence")
    if final_confidence < threshold:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "low_confidence",
        )
    return CandidateRoute("validated", None)


def route_relation_candidate_v1(
    *,
    schema_invalid: bool,
    evidence_invalid: bool,
    endpoint_rejected: bool,
    evidence_ambiguous: bool,
    schema_boundary_unclear: bool,
    has_open_conflict: bool,
    endpoint_pending_review: bool,
    evidence_support_mode: str,
    final_confidence: float,
    threshold: float = 0.85,
    automatic: bool = False,
    self_relation: bool = False,
) -> CandidateRoute:
    if schema_invalid:
        return CandidateRoute("rejected", "schema_invalid")
    if evidence_invalid:
        return CandidateRoute("rejected", "evidence_invalid")
    if self_relation:
        return CandidateRoute("rejected", "self_relation")
    if endpoint_rejected:
        return CandidateRoute("rejected", "endpoint_rejected")
    if evidence_ambiguous:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "evidence_ambiguous",
        )
    if schema_boundary_unclear:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "schema_boundary_unclear",
        )
    if has_open_conflict:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "conflict_open",
        )
    if endpoint_pending_review:
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "endpoint_pending_review",
        )
    if evidence_support_mode == "evidence_group":
        return CandidateRoute(
            "rejected" if automatic else "pending_review",
            "evidence_group_unsupported" if automatic else "evidence_group",
        )
    _bounded_component(final_confidence, label="final confidence")
    if automatic and final_confidence < threshold:
        return CandidateRoute("rejected", "low_confidence")
    return CandidateRoute("validated", None)


def _evidence_flags(rows: Iterable[Any]) -> tuple[bool, bool]:
    values = list(rows)
    if not values:
        return True, False
    return (
        any(row.validation_status == "invalid" for row in values),
        any(row.validation_status == "ambiguous" for row in values),
    )


def _valid_evidence_chunk_count(rows: Iterable[Any]) -> int:
    return len(
        {
            row.resolved_chunk_id
            for row in rows
            if row.validation_status == "valid" and row.resolved_chunk_id is not None
        }
    )


def _is_conceptual_entity(candidate: Any) -> bool:
    return str(candidate.entity_type_key).strip().casefold() in _CONCEPTUAL_ENTITY_TYPE_KEYS


def _has_generic_entity_name(candidate: Any) -> bool:
    value = candidate.normalized_name or candidate.canonical_name or ""
    normalized = str(value).strip().casefold()
    return (
        normalized in _GENERIC_ENTITY_NAMES
        or bool(_SECTION_HEADING_NAME.fullmatch(normalized))
        or normalized in _GENERIC_ACTION_NAMES
        or bool(_DATE_ONLY_NAME.fullmatch(normalized))
        or bool(_IP_ADDRESS_NAME.fullmatch(normalized))
        or normalized.startswith(("http://", "https://", "/api/", "api/"))
    )


def _append_error(candidate: Any, code: str) -> None:
    errors = list(candidate.validation_errors or [])
    if not any(isinstance(item, dict) and item.get("code") == code for item in errors):
        errors.append({"code": code})
    candidate.validation_errors = sorted(
        errors,
        key=lambda item: (
            item.get("code", "") if isinstance(item, dict) else "",
            item.get("field", "") if isinstance(item, dict) else "",
            item.get("value_hash", "") if isinstance(item, dict) else "",
        ),
    )


def _candidate_status_counts(candidates: Iterable[Any]) -> dict[str, int]:
    counts = {"validated": 0, "pending_review": 0, "rejected": 0}
    for candidate in candidates:
        if candidate.status in counts:
            counts[candidate.status] += 1
    return counts


def _candidate_failure_reasons(candidates: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.review_reason:
            counts[candidate.review_reason] = counts.get(candidate.review_reason, 0) + 1
    return dict(sorted(counts.items()))


def _apply_concept_relation_gate(
    entity_candidates: Iterable[Any],
    relation_candidates: Iterable[Any],
) -> None:
    related_entity_ids = {
        endpoint_id
        for relation in relation_candidates
        if relation.status == "validated"
        for endpoint_id in (relation.source_candidate_id, relation.target_candidate_id)
    }
    for candidate in entity_candidates:
        if (
            candidate.status == "validated"
            and _is_conceptual_entity(candidate)
            and candidate.id not in related_entity_ids
        ):
            candidate.status = "pending_review"
            candidate.review_reason = "concept_missing_valid_relation"
            _append_error(candidate, candidate.review_reason)


async def apply_job_candidate_routes(db, *, job: Any) -> JobCandidateRouteResult:
    policy = load_confidence_policy_v1(job)
    conflict_result = await db.execute(
        select(GraphExtractionConflict).where(
            GraphExtractionConflict.job_id == job.id,
            GraphExtractionConflict.status == "open",
            GraphExtractionConflict.purged_at.is_(None),
        )
    )
    conflict_entity_ids: set[str] = set()
    conflict_relation_ids: set[str] = set()
    for conflict in conflict_result.scalars().all():
        conflict_entity_ids.update(conflict.entity_candidate_ids)
        conflict_relation_ids.update(conflict.relation_candidate_ids)

    entity_evidence_result = await db.execute(
        select(GraphEntityCandidateEvidence).where(
            GraphEntityCandidateEvidence.job_id == job.id,
            GraphEntityCandidateEvidence.purged_at.is_(None),
        )
    )
    entity_evidence: dict[Any, list[Any]] = {}
    for row in entity_evidence_result.scalars().all():
        entity_evidence.setdefault(row.candidate_id, []).append(row)

    relation_evidence_result = await db.execute(
        select(GraphRelationCandidateEvidence).where(
            GraphRelationCandidateEvidence.job_id == job.id,
            GraphRelationCandidateEvidence.purged_at.is_(None),
        )
    )
    relation_evidence: dict[Any, list[Any]] = {}
    for row in relation_evidence_result.scalars().all():
        relation_evidence.setdefault(row.candidate_id, []).append(row)

    relation_result = await db.execute(
        select(GraphRelationCandidate)
        .where(
            GraphRelationCandidate.job_id == job.id,
            GraphRelationCandidate.purged_at.is_(None),
            GraphRelationCandidate.status.notin_({"materialized", "superseded"}),
        )
        .order_by(GraphRelationCandidate.candidate_key)
        .with_for_update()
    )
    relation_candidates = list(relation_result.scalars().all())

    entity_result = await db.execute(
        select(GraphEntityCandidate)
        .where(
            GraphEntityCandidate.job_id == job.id,
            GraphEntityCandidate.purged_at.is_(None),
            GraphEntityCandidate.status.notin_({"materialized", "superseded"}),
        )
        .order_by(GraphEntityCandidate.candidate_key)
        .with_for_update()
    )
    entity_candidates = list(entity_result.scalars().all())
    entity_by_id = {candidate.id: candidate for candidate in entity_candidates}
    for candidate in entity_candidates:
        if candidate.model_confidence is None:
            raise ConfidencePolicyError(
                "missing_confidence_component", "Entity Candidate model confidence is missing"
            )
        normalization_score = _NORMALIZATION_SCORES_V1.get(
            candidate.normalization_method or "ambiguous", 0.0
        )
        schema_score = candidate.schema_validation_score
        evidence_score = candidate.evidence_quality_score
        if schema_score is None or evidence_score is None:
            raise ConfidencePolicyError(
                "missing_confidence_component", "Entity Candidate score component is missing"
            )
        candidate.final_confidence = compute_final_confidence_v1(
            model=candidate.model_confidence,
            evidence=evidence_score,
            schema=schema_score,
            normalization=normalization_score,
        )
        evidence_invalid, evidence_ambiguous = _evidence_flags(
            entity_evidence.get(candidate.id, [])
        )
        route = route_entity_candidate_v1(
            schema_invalid=schema_score == 0.0,
            evidence_invalid=evidence_invalid,
            evidence_ambiguous=evidence_ambiguous,
            has_open_conflict=(
                str(candidate.id) in conflict_entity_ids
                or candidate.review_reason == "property_conflict"
            ),
            normalization_method=candidate.normalization_method or "ambiguous",
            matched_entity_id=candidate.matched_entity_id,
            final_confidence=candidate.final_confidence,
            threshold=policy.entity_materialization_threshold,
            automatic=policy.candidate_review_policy == "precision_first_auto",
            generic_name=_has_generic_entity_name(candidate),
            conceptual=_is_conceptual_entity(candidate),
            repeated_evidence=(
                _valid_evidence_chunk_count(entity_evidence.get(candidate.id, [])) >= 2
            ),
        )
        candidate.status = route.status
        candidate.review_reason = route.review_reason
        if route.review_reason is not None:
            _append_error(candidate, route.review_reason)
        elif route.status == "validated":
            candidate.validation_errors = []

    for candidate in relation_candidates:
        if candidate.model_confidence is None:
            raise ConfidencePolicyError(
                "missing_confidence_component", "Relation Candidate model confidence is missing"
            )
        if (
            candidate.evidence_quality_score is None
            or candidate.schema_validation_score is None
            or candidate.normalization_score is None
        ):
            raise ConfidencePolicyError(
                "missing_confidence_component", "Relation Candidate score component is missing"
            )
        candidate.final_confidence = compute_final_confidence_v1(
            model=candidate.model_confidence,
            evidence=candidate.evidence_quality_score,
            schema=candidate.schema_validation_score,
            normalization=candidate.normalization_score,
        )
        evidence_invalid, evidence_ambiguous = _evidence_flags(
            relation_evidence.get(candidate.id, [])
        )
        source = entity_by_id.get(candidate.source_candidate_id)
        target = entity_by_id.get(candidate.target_candidate_id)
        endpoint_rejected = source is None or target is None or any(
            endpoint.status == "rejected" for endpoint in (source, target)
        )
        endpoint_pending = not endpoint_rejected and any(
            endpoint.status == "pending_review"
            and (endpoint.matched_entity_id is None or _is_conceptual_entity(endpoint))
            for endpoint in (source, target)
        )
        route = route_relation_candidate_v1(
            schema_invalid=candidate.ontology_validation_status == "invalid",
            evidence_invalid=evidence_invalid,
            endpoint_rejected=endpoint_rejected,
            evidence_ambiguous=evidence_ambiguous,
            schema_boundary_unclear=(
                candidate.ontology_validation_status == "boundary_unclear"
            ),
            has_open_conflict=(
                candidate.has_conflict or str(candidate.id) in conflict_relation_ids
            ),
            endpoint_pending_review=endpoint_pending,
            evidence_support_mode=candidate.evidence_support_mode,
            final_confidence=candidate.final_confidence,
            threshold=policy.relation_draft_threshold,
            automatic=policy.candidate_review_policy == "precision_first_auto",
            self_relation=candidate.source_candidate_id == candidate.target_candidate_id,
        )
        candidate.status = route.status
        candidate.review_reason = route.review_reason
        if route.review_reason is not None:
            _append_error(candidate, route.review_reason)
        elif route.status == "validated":
            candidate.validation_errors = []

    _apply_concept_relation_gate(entity_candidates, relation_candidates)

    await db.flush()
    all_candidates: list[Any] = [*entity_candidates, *relation_candidates]
    return JobCandidateRouteResult(
        validated_count=sum(item.status == "validated" for item in all_candidates),
        pending_review_count=sum(
            item.status == "pending_review" for item in all_candidates
        ),
        rejected_count=sum(item.status == "rejected" for item in all_candidates),
        entity_status_counts=_candidate_status_counts(entity_candidates),
        relation_status_counts=_candidate_status_counts(relation_candidates),
        failure_reasons={
            "entities": _candidate_failure_reasons(entity_candidates),
            "relations": _candidate_failure_reasons(relation_candidates),
        },
    )
