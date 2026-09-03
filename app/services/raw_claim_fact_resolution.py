"""RawClaim adapter for the existing P2.3 fact-plan compiler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.models.fact_foundation import FACT_ASSERTION_MODALITIES, StablePredicateIdentity
from app.services.graph_relation_fact_resolution import (
    FactResolutionSource,
    GraphRelationFactPreflight,
    GraphRelationFactResolutionResult,
    _policy_property_keys,
    finalize_resolved_graph_relation_fact,
    materialize_resolved_graph_relation_fact,
    preflight_graph_relation_candidate_fact,
    preflight_raw_claim_projection_pending_fact,
)
from app.services.stable_predicate_resolution_policy import parse_stable_predicate_resolution_policy


@dataclass(frozen=True, slots=True)
class RawClaimFactAdapterPlan:
    status: str
    reason_code: str | None
    candidate: Any | None


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _qualifier_properties(raw_claim: Any, *, allowed_keys: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    properties: dict[str, Any] = {}
    for qualifier in _field(raw_claim, "qualifiers", ()):
        key = _field(qualifier, "key")
        if not isinstance(key, str) or key in properties:
            return None, "raw_claim_qualifier_ambiguous"
        if key not in allowed_keys:
            return None, "unclassified_relation_property"
        properties[key] = _field(qualifier, "value")
    return properties, None


def _inject_observed_value(
    properties: dict[str, Any],
    *,
    policy: Any,
    observed: str,
) -> bool:
    if policy.source == "fixed":
        return policy.value == observed
    matches = [raw for raw, resolved in policy.mapping.items() if resolved == observed]
    if policy.property_key is None or len(matches) != 1:
        return False
    properties[policy.property_key] = matches[0]
    return True


def _inject_time(properties: dict[str, Any], *, interval: Any, policy: Any) -> bool:
    if interval is None:
        return policy.source == "none"
    if not isinstance(interval, Mapping) or policy.source == "none":
        return False
    start = interval.get("start")
    end = interval.get("end")
    if policy.source == "instant_property":
        if policy.property_key is None or not isinstance(start, str) or (end is not None and end != start):
            return False
        properties[policy.property_key] = start
        return True
    if (
        policy.from_property_key is None
        or policy.to_property_key is None
        or not isinstance(start, str)
        or not isinstance(end, str)
    ):
        return False
    properties[policy.from_property_key] = start
    properties[policy.to_property_key] = end
    return True


def build_raw_claim_fact_adapter_plan(
    *,
    raw_claim: Any,
    projection_candidate: Any,
    predicate: StablePredicateIdentity,
) -> RawClaimFactAdapterPlan:
    """Map only policy-declared RawClaim observations into the P2.3 input shape."""
    try:
        policy = parse_stable_predicate_resolution_policy(predicate.resolution_policy or {})
    except (TypeError, ValueError, KeyError):
        return RawClaimFactAdapterPlan("pending", "predicate_not_ready", None)

    properties, reason_code = _qualifier_properties(
        raw_claim,
        allowed_keys=_policy_property_keys(policy),
    )
    if reason_code is not None:
        return RawClaimFactAdapterPlan("pending", reason_code, None)
    assert properties is not None
    if policy.measurement_policy is not None and (
        policy.measurement_policy.value_property_key not in properties
    ):
        return RawClaimFactAdapterPlan("pending", "measurement_value_unresolved", None)

    negation = _field(raw_claim, "negation", {})
    observed_polarity = "negated" if _field(negation, "value") is True else "affirmed"
    modality = _field(_field(raw_claim, "modality", {}), "value") or "unknown"
    if modality not in FACT_ASSERTION_MODALITIES:
        return RawClaimFactAdapterPlan("pending", "raw_claim_modality_invalid", None)
    if not _inject_observed_value(
        properties,
        policy=policy.polarity_policy,
        observed=observed_polarity,
    ) or not _inject_observed_value(
        properties,
        policy=policy.modality_policy,
        observed=modality,
    ):
        return RawClaimFactAdapterPlan("pending", "raw_claim_policy_semantic_mismatch", None)
    if not _inject_time(
        properties,
        interval=_field(raw_claim, "valid_time"),
        policy=policy.valid_time_policy,
    ) or not _inject_time(
        properties,
        interval=_field(raw_claim, "effective_time"),
        policy=policy.effective_time_policy,
    ):
        return RawClaimFactAdapterPlan("pending", "raw_claim_policy_semantic_mismatch", None)

    return RawClaimFactAdapterPlan(
        "ready",
        None,
        SimpleNamespace(
            id=_field(raw_claim, "id"),
            relation_type_key=projection_candidate.relation_type_key,
            proposed_properties=properties,
            final_confidence=getattr(projection_candidate, "final_confidence", None),
            evidence_support_mode="single_evidence",
        ),
    )


async def record_raw_claim_projection_pending_fact(
    db: Any,
    *,
    library_id: Any,
    raw_claim: Any,
    evidence: Any,
    reason_code: str,
    projection_contexts: list[Mapping[str, Any]],
) -> GraphRelationFactPreflight | None:
    """Record a RawClaim projection failure without inventing a formal Fact."""
    source_content_fingerprint = _field(raw_claim, "content_scoped_claim_fingerprint")
    if (
        not isinstance(source_content_fingerprint, str)
        or len(source_content_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in source_content_fingerprint)
    ):
        return None
    return await preflight_raw_claim_projection_pending_fact(
        db,
        library_id=library_id,
        raw_claim_id=raw_claim.id,
        source_content_fingerprint=source_content_fingerprint,
        evidence=evidence,
        reason_code=reason_code,
        projection_contexts=projection_contexts,
    )


async def resolve_raw_claim_fact(
    db: Any,
    *,
    library_id: Any,
    raw_claim: Any,
    projection_candidate: Any,
    relation: Any,
    relation_evidence: Any,
    source_entity: Any,
    target_entity: Any,
    evidence: Any,
    expected_logical_fact: Any | None,
    expected_assertion: Any | None,
) -> GraphRelationFactResolutionResult:
    """Resolve one binding-backed RawClaim through the P2.3 Fact core."""
    source = FactResolutionSource.raw_claim(raw_claim.id)
    adapted: RawClaimFactAdapterPlan | None = None

    def adapt(predicate: StablePredicateIdentity) -> RawClaimFactAdapterPlan:
        nonlocal adapted
        adapted = build_raw_claim_fact_adapter_plan(
            raw_claim=raw_claim,
            projection_candidate=projection_candidate,
            predicate=predicate,
        )
        return adapted

    preflight = await preflight_graph_relation_candidate_fact(
        db,
        library_id=library_id,
        candidate=projection_candidate,
        relation_type_id=relation.relation_type_id,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence_rows=[evidence],
        source=source,
        expected_logical_fact=expected_logical_fact,
        expected_assertion=expected_assertion,
        candidate_adapter=adapt,
    )
    if not preflight.is_resolved:
        assert preflight.decision is not None
        return GraphRelationFactResolutionResult(
            "REJECT" if preflight.decision.status == "rejected" else "PENDING",
            preflight.decision,
            None,
            None,
        )
    if adapted is None or adapted.candidate is None:
        raise RuntimeError("resolved RawClaim preflight is missing its adapter input")
    materialization = await materialize_resolved_graph_relation_fact(
        db,
        library_id=library_id,
        candidate=adapted.candidate,
        relation=relation,
        source_entity=source_entity,
        preflight=preflight,
        source=source,
    )
    return await finalize_resolved_graph_relation_fact(
        db,
        library_id=library_id,
        candidate=adapted.candidate,
        relation=relation,
        relation_evidence=relation_evidence,
        preflight=preflight,
        materialization=materialization,
        source=source,
    )
