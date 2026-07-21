from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.services.graph_candidate_routing import (
    ConfidencePolicyError,
    compute_final_confidence_v1,
    load_confidence_policy_v1,
    route_entity_candidate_v1,
    route_relation_candidate_v1,
)


def _policy_snapshot():
    return {
        "entity_materialization_threshold": 0.85,
        "relation_draft_threshold": 0.85,
        "confidence_weights": {
            "model": 0.25,
            "evidence": 0.35,
            "schema": 0.25,
            "normalization": 0.15,
        },
        "evidence_score_map": {"direct_statement": 1.0, "table_cell": 0.95},
        "schema_score_map": {
            "valid": 1.0,
            "warning": 0.6,
            "boundary_unclear": 0.4,
            "invalid": 0.0,
        },
        "normalization_score_map": {
            "exact_normalized_match": 1.0,
            "exact_alias_match": 0.95,
            "new_entity": 0.9,
            "ambiguous": 0.0,
        },
        "evidence_group_policy": "all_claims_valid",
        "unrelated_frozen_policy_field": "allowed",
    }


def test_confidence_formula_is_exact_and_rejects_invalid_components():
    assert compute_final_confidence_v1(
        model=0.8, evidence=1.0, schema=0.6, normalization=0.9
    ) == pytest.approx(0.835)
    for value in (-0.1, 1.1, math.nan, math.inf):
        with pytest.raises(ConfidencePolicyError):
            compute_final_confidence_v1(
                model=value, evidence=1.0, schema=1.0, normalization=1.0
            )


def test_policy_loader_requires_the_frozen_v1_values_but_allows_other_fields():
    job = SimpleNamespace(
        confidence_policy_version="v1", policy_config_snapshot=_policy_snapshot()
    )
    policy = load_confidence_policy_v1(job)
    assert policy.entity_materialization_threshold == 0.85

    broken = _policy_snapshot()
    broken["confidence_weights"]["model"] = 0.3
    with pytest.raises(ConfidencePolicyError) as exc_info:
        load_confidence_policy_v1(
            SimpleNamespace(confidence_policy_version="v1", policy_config_snapshot=broken)
        )
    assert exc_info.value.code == "invalid_confidence_policy"


def test_entity_hard_routes_precede_score_and_matching_threshold():
    base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        evidence_ambiguous=False,
        has_open_conflict=False,
        normalization_method="new_entity",
        matched_entity_id=None,
        final_confidence=1.0,
    )
    assert route_entity_candidate_v1(**(base | {"schema_invalid": True})).status == "rejected"
    invalid = route_entity_candidate_v1(
        **(base | {"evidence_invalid": True, "evidence_ambiguous": True})
    )
    assert invalid.status == "rejected"
    assert invalid.review_reason == "evidence_invalid"
    assert route_entity_candidate_v1(
        **(base | {"evidence_ambiguous": True})
    ).review_reason == "evidence_ambiguous"
    assert route_entity_candidate_v1(
        **(base | {"has_open_conflict": True})
    ).review_reason == "conflict_open"
    assert route_entity_candidate_v1(
        **(base | {"normalization_method": "ambiguous"})
    ).review_reason == "entity_match_ambiguous"
    assert route_entity_candidate_v1(
        **(
            base
            | {
                "normalization_method": "ambiguous",
                "has_open_conflict": True,
            }
        )
    ).review_reason == "entity_match_ambiguous"

    matched = route_entity_candidate_v1(
        **(base | {"matched_entity_id": "existing", "final_confidence": 0.1})
    )
    assert matched.status == "validated"
    assert route_entity_candidate_v1(
        **(base | {"final_confidence": 0.849999})
    ).review_reason == "low_confidence"
    assert route_entity_candidate_v1(
        **(base | {"final_confidence": 0.85})
    ).status == "validated"


def test_relation_hard_route_precedence_and_evidence_group_rule():
    base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        endpoint_rejected=False,
        evidence_ambiguous=False,
        schema_boundary_unclear=False,
        has_open_conflict=False,
        endpoint_pending_review=False,
        evidence_support_mode="single_evidence",
        final_confidence=0.1,
    )
    assert route_relation_candidate_v1(
        **(base | {"schema_invalid": True, "evidence_ambiguous": True})
    ).review_reason == "schema_invalid"
    assert route_relation_candidate_v1(
        **(base | {"evidence_invalid": True, "endpoint_rejected": True})
    ).review_reason == "evidence_invalid"
    assert route_relation_candidate_v1(
        **(base | {"endpoint_rejected": True})
    ).review_reason == "endpoint_rejected"
    assert route_relation_candidate_v1(
        **(base | {"evidence_ambiguous": True})
    ).review_reason == "evidence_ambiguous"
    assert route_relation_candidate_v1(
        **(base | {"schema_boundary_unclear": True})
    ).review_reason == "schema_boundary_unclear"
    assert route_relation_candidate_v1(
        **(base | {"has_open_conflict": True})
    ).review_reason == "conflict_open"
    assert route_relation_candidate_v1(
        **(base | {"endpoint_pending_review": True})
    ).review_reason == "endpoint_pending_review"
    assert route_relation_candidate_v1(
        **(base | {"evidence_support_mode": "evidence_group", "final_confidence": 1.0})
    ).review_reason == "evidence_group"
    assert route_relation_candidate_v1(**base).status == "validated"
