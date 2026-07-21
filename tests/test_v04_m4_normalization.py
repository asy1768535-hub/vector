from __future__ import annotations

import math
import uuid

import pytest

from app.services.graph_candidate_aggregation import (
    canonical_graph_json_v1,
    conflict_key_v1,
    entity_candidate_key_v1,
    merge_candidate_key_v1,
    relation_candidate_key_v1,
)
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.graph_schema_validator import normalize_graph_name


ONTOLOGY_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
JOB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ENTITY_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TARGET_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def test_normalization_v1_is_the_v03_strip_casefold_whitespace_rule():
    assert normalize_graph_name_v1("  Alice\tZHANG\n") == "alice zhang"
    assert normalize_graph_name("  Alice\tZHANG\n") == "alice zhang"
    assert normalize_graph_name_v1("Straße") == "strasse"
    assert normalize_graph_name_v1("\u3000Ａlice\u3000") == "ａlice"
    assert normalize_graph_name_v1("e\u0301") != normalize_graph_name_v1("é")
    assert normalize_graph_name_v1(" \t\n ") == ""


def test_normalization_v1_requires_a_string():
    with pytest.raises(TypeError, match="graph name must be a string"):
        normalize_graph_name_v1(None)  # type: ignore[arg-type]


def test_canonical_json_is_stable_and_rejects_non_json_values():
    assert canonical_graph_json_v1({"b": 2, "a": [True, "中文"]}) == (
        '{"a":[true,"中文"],"b":2}'
    )
    with pytest.raises(TypeError, match="unsupported JSON value tuple"):
        canonical_graph_json_v1((1, 2))
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_graph_json_v1({1: "value"})
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite JSON numbers"):
            canonical_graph_json_v1(value)


def test_candidate_and_review_keys_match_frozen_known_vectors():
    assert entity_candidate_key_v1(
        ontology_version_id=ONTOLOGY_ID,
        entity_type_key="department",
        normalized_name="human resources",
    ) == "e14ccba18cdefb86663e56b1d3b6435a7bd68dbd0578872aeb477c8559c874c3"

    assert relation_candidate_key_v1(
        ontology_version_id=ONTOLOGY_ID,
        source_candidate_key="a" * 64,
        relation_type_key="responsible_for",
        target_candidate_key="b" * 64,
        properties={"priority": 1, "active": True},
    ) == "224f4b201d8414eeb0547089ab0fdb05c1eca20b82bfcb42a7ad358571c921a2"

    assert merge_candidate_key_v1(
        job_id=JOB_ID,
        entity_candidate_id=ENTITY_ID,
        suggested_target_entity_id=TARGET_ID,
        reason="exact_alias",
    ) == "a782b873dd51691bcbe1b3e55832c043ce2ba82ea09906f943f40e19f81f2827"

    assert conflict_key_v1(
        job_id=JOB_ID,
        conflict_type="property_conflict",
        entity_candidate_ids=[
            uuid.UUID("55555555-5555-5555-5555-555555555555"),
            ENTITY_ID,
            ENTITY_ID,
        ],
        relation_candidate_ids=[
            uuid.UUID("66666666-6666-6666-6666-666666666666")
        ],
        conflicting_fields=[
            {"field": "properties.owner", "value_hashes": ["b" * 64, "a" * 64]},
            {"field": "properties.owner", "value_hashes": ["a" * 64]},
        ],
    ) == "8f42d8da5b36eec52b937e98feafa6d4ab0b94c8b1d036addd9b02401e7867e6"


def test_relation_key_is_independent_of_property_object_order():
    left = relation_candidate_key_v1(
        ontology_version_id=ONTOLOGY_ID,
        source_candidate_key="a" * 64,
        relation_type_key="responsible_for",
        target_candidate_key="b" * 64,
        properties={"nested": {"b": 2, "a": 1}, "enabled": True},
    )
    right = relation_candidate_key_v1(
        ontology_version_id=str(ONTOLOGY_ID),
        source_candidate_key="a" * 64,
        relation_type_key="responsible_for",
        target_candidate_key="b" * 64,
        properties={"enabled": True, "nested": {"a": 1, "b": 2}},
    )
    assert left == right
