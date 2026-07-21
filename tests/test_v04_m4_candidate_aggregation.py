from __future__ import annotations

import itertools

import pytest

from app.services.graph_candidate_aggregation import (
    CandidateAggregationError,
    EntityAggregateOccurrence,
    RelationAggregateOccurrence,
    aggregate_entity_occurrence_payloads,
    aggregate_relation_occurrence_payloads,
)


def _entity_occurrences():
    return [
        EntityAggregateOccurrence(
            unit_ordinal=2,
            local_ref="e2",
            model_confidence=0.95,
            raw_payload={
                "local_id": "e2",
                "name": "HUMAN   RESOURCES",
                "entity_type_key": "department",
                "aliases": ["HR", "People Team"],
                "properties": {"owner": "Bob", "floor": 3},
                "external_mapping_hints": [
                    {"system": "erp", "key": "D-1"},
                    {"system": "hris", "key": "HR"},
                ],
            },
        ),
        EntityAggregateOccurrence(
            unit_ordinal=1,
            local_ref="e1",
            model_confidence=0.8,
            raw_payload={
                "local_id": "e1",
                "name": " Human Resources ",
                "entity_type_key": "department",
                "aliases": ["hr", "People   Team", ""],
                "properties": {"owner": "Alice", "floor": 3},
                "external_mapping_hints": [
                    {"key": "D-1", "system": "erp"}
                ],
            },
        ),
    ]


def test_entity_aggregate_is_identical_for_every_input_permutation():
    results = [
        aggregate_entity_occurrence_payloads(permutation)
        for permutation in itertools.permutations(_entity_occurrences())
    ]
    assert all(result == results[0] for result in results)
    result = results[0]
    assert result.canonical_name == "Human Resources"
    assert result.normalized_name == "human resources"
    assert result.proposed_aliases == ["HR", "People   Team"]
    assert result.proposed_properties == {"floor": 3, "owner": "Alice"}
    assert result.external_mapping_hints == [
        {"key": "D-1", "system": "erp"},
        {"system": "hris", "key": "HR"},
    ]
    assert result.model_confidence == 0.95
    assert len(result.property_conflicts) == 1
    assert result.property_conflicts[0].field == "properties.owner"
    assert len(result.property_conflicts[0].value_hashes) == 2


def test_relation_aggregate_is_order_independent_and_uses_max_confidence():
    occurrences = [
        RelationAggregateOccurrence(
            unit_ordinal=9,
            ordinal=0,
            model_confidence=0.7,
            raw_payload={"properties": {"active": True, "priority": 1}},
        ),
        RelationAggregateOccurrence(
            unit_ordinal=2,
            ordinal=3,
            model_confidence=0.9,
            raw_payload={"properties": {"priority": 1, "active": True}},
        ),
    ]
    left = aggregate_relation_occurrence_payloads(occurrences)
    right = aggregate_relation_occurrence_payloads(reversed(occurrences))
    assert left == right
    assert left.proposed_properties == {"priority": 1, "active": True}
    assert left.model_confidence == 0.9


def test_relation_aggregate_rejects_properties_that_do_not_match_one_key():
    with pytest.raises(CandidateAggregationError) as exc_info:
        aggregate_relation_occurrence_payloads(
            [
                RelationAggregateOccurrence(0, 0, 0.5, {"properties": {"x": 1}}),
                RelationAggregateOccurrence(1, 0, 0.6, {"properties": {"x": 2}}),
            ]
        )
    assert exc_info.value.code == "relation_candidate_key_mismatch"


def test_aggregate_rejects_missing_or_purged_payload_input():
    with pytest.raises(CandidateAggregationError) as exc_info:
        aggregate_entity_occurrence_payloads([])
    assert exc_info.value.code == "missing_occurrences"

    with pytest.raises(CandidateAggregationError) as exc_info:
        aggregate_entity_occurrence_payloads(
            [EntityAggregateOccurrence(0, "e1", 0.5, None)]  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "invalid_occurrence_payload"
