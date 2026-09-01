from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.graph_candidate_validation import (
    EntityMatchInput,
    OntologySnapshotError,
    classify_entity_matches,
    load_ontology_rule_set_v1,
)


ONTOLOGY_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PERSON_TYPE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
DEPARTMENT_TYPE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
RELATION_TYPE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _snapshot():
    return {
        "ontology_version_id": str(ONTOLOGY_ID),
        "entity_types": [
            {
                "id": str(DEPARTMENT_TYPE_ID),
                "key": "department",
                "properties_schema": None,
                "active_attribute_definitions": [],
            },
            {
                "id": str(PERSON_TYPE_ID),
                "key": "person",
                "properties_schema": {"properties": {"name": {"type": "string"}}},
                "active_attribute_definitions": [
                    {
                        "key": "level",
                        "value_type": "integer",
                        "required": False,
                        "enum_values": None,
                        "validation_schema": None,
                    }
                ],
            },
        ],
        "relation_types": [
            {
                "id": str(RELATION_TYPE_ID),
                "key": "belongs_to",
                "direction": "directed",
                "requires_evidence": True,
                "default_review_policy": "auto_active",
                "properties_schema": None,
                "active_attribute_definitions": [],
            }
        ],
        "relation_constraints": [
            {
                "relation_type_id": str(RELATION_TYPE_ID),
                "source_entity_type_id": str(PERSON_TYPE_ID),
                "target_entity_type_id": str(DEPARTMENT_TYPE_ID),
                "cardinality": "many_to_one",
                "requires_review": False,
            }
        ],
    }


def _job(snapshot=None):
    value = _snapshot() if snapshot is None else snapshot
    return SimpleNamespace(
        ontology_version_id=ONTOLOGY_ID,
        ontology_snapshot=value,
        ontology_snapshot_hash=canonical_graph_value_hash_v1(value),
        normalization_rule_version="normalization_v1",
        confidence_policy_version="v1",
    )


def test_snapshot_parser_builds_shared_rules_and_constraints():
    rules = load_ontology_rule_set_v1(_job())
    assert list(rules.entity_types_by_key) == ["department", "person"]
    assert rules.entity_types_by_key["person"].active_attribute_definitions[0].key == "level"
    relation = rules.relation_types_by_key["belongs_to"]
    constraint = rules.constraints[(relation.id, PERSON_TYPE_ID, DEPARTMENT_TYPE_ID)]
    assert constraint.cardinality == "many_to_one"
    assert constraint.requires_review is False


def test_snapshot_parser_allows_ai_discovery_metadata():
    snapshot = {
        **_snapshot(),
        "schema_state": "ai_draft",
        "confirmed": False,
        "origin": "ai_discovery",
        "source_hash": "a" * 64,
    }

    rules = load_ontology_rule_set_v1(_job(snapshot))

    assert list(rules.relation_types_by_key) == ["belongs_to"]


def test_snapshot_parser_fails_closed_on_hash_scope_shape_order_and_references():
    bad_hash_job = _job()
    bad_hash_job.ontology_snapshot_hash = "0" * 64
    with pytest.raises(OntologySnapshotError) as exc_info:
        load_ontology_rule_set_v1(bad_hash_job)
    assert exc_info.value.code == "ontology_snapshot_hash_mismatch"

    wrong_scope = _job()
    wrong_scope.ontology_version_id = uuid.uuid4()
    with pytest.raises(OntologySnapshotError) as exc_info:
        load_ontology_rule_set_v1(wrong_scope)
    assert exc_info.value.code == "ontology_snapshot_scope_mismatch"

    unsorted = _snapshot()
    unsorted["entity_types"].reverse()
    with pytest.raises(OntologySnapshotError, match="sorted"):
        load_ontology_rule_set_v1(_job(unsorted))

    unknown_reference = _snapshot()
    unknown_reference["relation_constraints"][0]["source_entity_type_id"] = str(uuid.uuid4())
    with pytest.raises(OntologySnapshotError, match="unknown Type"):
        load_ontology_rule_set_v1(_job(unknown_reference))

    extra = copy.deepcopy(_snapshot())
    extra["extra"] = True
    with pytest.raises(OntologySnapshotError, match="exactly"):
        load_ontology_rule_set_v1(_job(extra))


def test_exact_name_wins_only_when_name_and_alias_target_the_same_entity():
    entity_id = uuid.uuid4()
    decision = classify_entity_matches(
        required_entity_type_id=PERSON_TYPE_ID,
        matches=[
            EntityMatchInput(entity_id, PERSON_TYPE_ID, "active", via_normalized_name=True),
            EntityMatchInput(entity_id, PERSON_TYPE_ID, "active", via_active_alias=True),
        ],
    )
    assert decision.matched_entity_id == entity_id
    assert decision.normalization_method == "exact_normalized_match"
    assert decision.normalization_score == 1.0


def test_unique_alias_and_new_entity_decisions_have_frozen_scores():
    entity_id = uuid.uuid4()
    alias = classify_entity_matches(
        required_entity_type_id=PERSON_TYPE_ID,
        matches=[EntityMatchInput(entity_id, PERSON_TYPE_ID, "draft", via_active_alias=True)],
    )
    assert alias.matched_entity_id == entity_id
    assert alias.normalization_method == "exact_alias_match"
    assert alias.normalization_score == 0.95

    new = classify_entity_matches(required_entity_type_id=PERSON_TYPE_ID, matches=[])
    assert new.matched_entity_id is None
    assert new.normalization_method == "new_entity"
    assert new.normalization_score == 0.9


def test_multiple_wrong_type_and_ineligible_matches_are_never_reused():
    first = uuid.uuid4()
    second = uuid.uuid4()
    multiple = classify_entity_matches(
        required_entity_type_id=PERSON_TYPE_ID,
        matches=[
            EntityMatchInput(first, PERSON_TYPE_ID, "active", via_normalized_name=True),
            EntityMatchInput(second, PERSON_TYPE_ID, "active", via_active_alias=True),
        ],
    )
    assert multiple.matched_entity_id is None
    assert multiple.ambiguity_reason == "entity_merge_ambiguity"
    assert multiple.normalization_score == 0.0

    wrong_type = classify_entity_matches(
        required_entity_type_id=PERSON_TYPE_ID,
        matches=[EntityMatchInput(first, DEPARTMENT_TYPE_ID, "active", via_active_alias=True)],
    )
    assert wrong_type.ambiguity_reason == "entity_type_mismatch"

    ineligible = classify_entity_matches(
        required_entity_type_id=PERSON_TYPE_ID,
        matches=[EntityMatchInput(first, PERSON_TYPE_ID, "stale", via_normalized_name=True)],
    )
    assert ineligible.ambiguity_reason == "entity_status_conflict"
    assert ineligible.ineligible_statuses == ((first, "stale"),)
