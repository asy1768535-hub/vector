from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.services.graph_candidate_routing import (
    CandidateRoute,
    ConfidencePolicyError,
    _apply_concept_relation_gate,
    _has_generic_entity_name,
    _valid_evidence_chunk_count,
    compute_final_confidence_v1,
    load_confidence_policy_v1,
    route_entity_candidate_v1,
    route_relation_candidate_v1,
)


def _policy_snapshot(*, candidate_review_policy=None):
    snapshot = {
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
    if candidate_review_policy is not None:
        snapshot["candidate_review_policy"] = candidate_review_policy
    return snapshot


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
    assert policy.candidate_review_policy == "manual_review"

    automatic = load_confidence_policy_v1(
        SimpleNamespace(
            confidence_policy_version="v1",
            policy_config_snapshot=_policy_snapshot(
                candidate_review_policy="precision_first_auto"
            ),
        )
    )
    assert automatic.candidate_review_policy == "precision_first_auto"

    legacy_entity = route_entity_candidate_v1(
        schema_invalid=False,
        evidence_invalid=False,
        evidence_ambiguous=True,
        has_open_conflict=False,
        normalization_method="new_entity",
        matched_entity_id=None,
        final_confidence=0.95,
    )
    legacy_relation = route_relation_candidate_v1(
        schema_invalid=False,
        evidence_invalid=False,
        endpoint_rejected=False,
        evidence_ambiguous=False,
        schema_boundary_unclear=False,
        has_open_conflict=False,
        endpoint_pending_review=False,
        evidence_support_mode="evidence_group",
        final_confidence=0.95,
    )
    assert legacy_entity.status == "pending_review"
    assert legacy_relation.status == "pending_review"

    broken = _policy_snapshot()
    broken["confidence_weights"]["model"] = 0.3
    with pytest.raises(ConfidencePolicyError) as exc_info:
        load_confidence_policy_v1(
            SimpleNamespace(confidence_policy_version="v1", policy_config_snapshot=broken)
        )
    assert exc_info.value.code == "invalid_confidence_policy"

    unsupported = _policy_snapshot(candidate_review_policy="approve_everything")
    with pytest.raises(ConfidencePolicyError) as exc_info:
        load_confidence_policy_v1(
            SimpleNamespace(
                confidence_policy_version="v1",
                policy_config_snapshot=unsupported,
            )
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
        automatic=True,
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
    assert matched == route_entity_candidate_v1(
        **(base | {"matched_entity_id": None, "final_confidence": 0.1})
    )
    assert matched.status == "rejected"
    low_confidence = route_entity_candidate_v1(
        **(base | {"final_confidence": 0.849999})
    )
    assert low_confidence.status == "rejected"
    assert low_confidence.review_reason == "low_confidence"
    assert route_entity_candidate_v1(
        **(base | {"final_confidence": 0.85})
    ).status == "validated"


def test_relation_hard_route_precedence_threshold_and_evidence_group_rule():
    base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        endpoint_rejected=False,
        evidence_ambiguous=False,
        schema_boundary_unclear=False,
        has_open_conflict=False,
        endpoint_pending_review=False,
        evidence_support_mode="single_evidence",
        final_confidence=0.95,
        automatic=True,
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
        **(base | {"self_relation": True})
    ) == CandidateRoute("rejected", "self_relation")
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
    evidence_group = route_relation_candidate_v1(
        **(base | {"evidence_support_mode": "evidence_group", "final_confidence": 1.0})
    )
    assert evidence_group.status == "rejected"
    assert evidence_group.review_reason == "evidence_group_unsupported"
    low_confidence = route_relation_candidate_v1(
        **(base | {"final_confidence": 0.849999})
    )
    assert low_confidence.status == "rejected"
    assert low_confidence.review_reason == "low_confidence"
    assert route_relation_candidate_v1(**base).status == "validated"


def test_automatic_routing_has_no_pending_review_outcomes():
    entity_base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        evidence_ambiguous=False,
        has_open_conflict=False,
        normalization_method="new_entity",
        matched_entity_id=None,
        final_confidence=0.95,
        automatic=True,
    )
    entity_variants = [
        {},
        {"schema_invalid": True},
        {"evidence_invalid": True},
        {"evidence_ambiguous": True},
        {"has_open_conflict": True},
        {"normalization_method": "ambiguous"},
        {"final_confidence": 0.1},
    ]
    assert all(
        route_entity_candidate_v1(**(entity_base | changes)).status
        != "pending_review"
        for changes in entity_variants
    )

    relation_base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        endpoint_rejected=False,
        evidence_ambiguous=False,
        schema_boundary_unclear=False,
        has_open_conflict=False,
        endpoint_pending_review=False,
        evidence_support_mode="single_evidence",
        final_confidence=0.95,
        automatic=True,
    )
    relation_variants = [
        {},
        {"schema_invalid": True},
        {"evidence_invalid": True},
        {"endpoint_rejected": True},
        {"evidence_ambiguous": True},
        {"schema_boundary_unclear": True},
        {"has_open_conflict": True},
        {"endpoint_pending_review": True},
        {"evidence_support_mode": "evidence_group"},
        {"final_confidence": 0.1},
    ]
    assert all(
        route_relation_candidate_v1(**(relation_base | changes)).status
        != "pending_review"
        for changes in relation_variants
    )


def test_generic_names_are_rejected_and_concepts_require_publishable_support():
    base = dict(
        schema_invalid=False,
        evidence_invalid=False,
        evidence_ambiguous=False,
        has_open_conflict=False,
        normalization_method="new_entity",
        matched_entity_id=None,
        final_confidence=0.95,
        automatic=True,
    )
    assert route_entity_candidate_v1(
        **base, generic_name=True
    ) == CandidateRoute("rejected", "generic_entity_name")

    isolated = route_entity_candidate_v1(
        **base, conceptual=True, repeated_evidence=False
    )
    assert isolated == CandidateRoute("pending_review", "concept_single_occurrence")
    assert route_entity_candidate_v1(
        **(base | {"automatic": False, "matched_entity_id": "existing"}),
        conceptual=True,
        repeated_evidence=False,
    ) == isolated

    assert route_entity_candidate_v1(
        **base,
        conceptual=True,
        repeated_evidence=True,
    ).status == "validated"


@pytest.mark.parametrize(
    "name",
    [
        "2026年05月12日",
        "2026-05-12",
        "10.0.10.2",
        "10.0.10.2:8113",
        "https://example.test/docs",
        "/api/v1/jobs",
    ],
)
def test_structural_values_are_not_entities(name):
    assert _has_generic_entity_name(
        SimpleNamespace(normalized_name=name, canonical_name=name)
    )


@pytest.mark.parametrize(
    "name",
    [
        "10.1 \u8be6\u7ec6\u8bbe\u8ba1\u4e0e\u7f16\u7801\u8854\u63a5",
        "\u64cd\u4f5c\u5b89\u6392",
    ],
)
def test_section_headings_and_generic_actions_are_not_entities(name):
    assert _has_generic_entity_name(
        SimpleNamespace(normalized_name=name, canonical_name=name)
    )


def test_named_objects_containing_digits_remain_entities():
    assert not _has_generic_entity_name(
        SimpleNamespace(
            normalized_name="2026年度安全管理制度",
            canonical_name="2026年度安全管理制度",
        )
    )


def test_concept_evidence_counts_distinct_valid_chunks_only():
    first_chunk = object()
    rows = [
        SimpleNamespace(validation_status="valid", resolved_chunk_id=first_chunk),
        SimpleNamespace(validation_status="valid", resolved_chunk_id=first_chunk),
        SimpleNamespace(validation_status="ambiguous", resolved_chunk_id=object()),
        SimpleNamespace(validation_status="valid", resolved_chunk_id=object()),
    ]
    assert _valid_evidence_chunk_count(rows) == 2


def test_concept_without_validated_relation_returns_to_pending_review():
    concept = SimpleNamespace(
        id="concept",
        entity_type_key="term",
        status="validated",
        review_reason=None,
        validation_errors=[],
    )
    concrete = SimpleNamespace(
        id="organization",
        entity_type_key="department",
        status="validated",
        review_reason=None,
        validation_errors=[],
    )
    rejected_relation = SimpleNamespace(
        source_candidate_id=concept.id,
        target_candidate_id=concrete.id,
        status="rejected",
    )

    _apply_concept_relation_gate([concept, concrete], [rejected_relation])

    assert concept.status == "pending_review"
    assert concept.review_reason == "concept_missing_valid_relation"
    assert concrete.status == "validated"


def test_concept_with_validated_relation_remains_publishable():
    concept = SimpleNamespace(
        id="concept",
        entity_type_key="concept",
        status="validated",
        review_reason=None,
        validation_errors=[],
    )
    concrete = SimpleNamespace(
        id="system", entity_type_key="product", status="validated"
    )
    relation = SimpleNamespace(
        source_candidate_id=concept.id,
        target_candidate_id=concrete.id,
        status="validated",
    )

    _apply_concept_relation_gate([concept, concrete], [relation])

    assert concept.status == "validated"
