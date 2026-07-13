from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.services.graph_schema_validator import (
    AttributeDefinitionRule,
    EntityTypeRule,
    RelationConstraintRule,
    RelationTypeRule,
    validate_entity_shape,
    validate_relation_shape,
)


ONTOLOGY_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ENTITY_TYPE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TARGET_TYPE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
RELATION_TYPE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _entity_rule() -> EntityTypeRule:
    return EntityTypeRule(
        id=ENTITY_TYPE_ID,
        ontology_version_id=ONTOLOGY_ID,
        key="person",
        properties_schema={
            "required": ["display_name"],
            "properties": {"display_name": {"type": "string"}},
        },
        active_attribute_definitions=(
            AttributeDefinitionRule("level", "integer", False),
            AttributeDefinitionRule(
                "employment_type",
                "enum",
                False,
                ("employee", "contractor"),
            ),
        ),
    )


def _relation_rule(
    *,
    key: str = "belongs_to",
    review_policy: str = "auto_active",
) -> RelationTypeRule:
    return RelationTypeRule(
        id=RELATION_TYPE_ID,
        ontology_version_id=ONTOLOGY_ID,
        key=key,
        direction="directed",
        requires_evidence=True,
        default_review_policy=review_policy,
        properties_schema=None,
        active_attribute_definitions=(
            AttributeDefinitionRule("since", "date", True),
        ),
    )


def _constraint(*, requires_review: bool = False) -> RelationConstraintRule:
    return RelationConstraintRule(
        source_entity_type_id=ENTITY_TYPE_ID,
        target_entity_type_id=TARGET_TYPE_ID,
        cardinality="many_to_one",
        requires_review=requires_review,
    )


def test_entity_shape_normalizes_and_validates_properties_without_database():
    properties = {"display_name": "Alice", "level": 3, "employment_type": "employee"}
    result = validate_entity_shape(
        entity_type=_entity_rule(),
        canonical_name="  ALICE  ",
        properties=properties,
    )
    properties["level"] = 9
    assert result.normalized_name == "alice"
    assert result.validated_properties["level"] == 3
    assert result.reasons == ()


def test_entity_shape_rejects_empty_schema_and_attribute_errors():
    with pytest.raises(ValueError, match="canonical_name must be non-empty"):
        validate_entity_shape(
            entity_type=_entity_rule(), canonical_name=" \t ", properties={}
        )
    with pytest.raises(ValueError, match="missing required property: display_name"):
        validate_entity_shape(
            entity_type=_entity_rule(), canonical_name="Alice", properties={}
        )
    with pytest.raises(ValueError, match="attribute level must be integer"):
        validate_entity_shape(
            entity_type=_entity_rule(),
            canonical_name="Alice",
            properties={"display_name": "Alice", "level": "senior"},
        )
    with pytest.raises(ValueError, match="attribute employment_type must be one of"):
        validate_entity_shape(
            entity_type=_entity_rule(),
            canonical_name="Alice",
            properties={"display_name": "Alice", "employment_type": "vendor"},
        )


def test_relation_shape_classifies_constraint_and_review_boundaries():
    valid = validate_relation_shape(
        relation_type=_relation_rule(),
        constraint=_constraint(),
        source_entity_type_id=ENTITY_TYPE_ID,
        target_entity_type_id=TARGET_TYPE_ID,
        properties={"since": "2026-07-13"},
    )
    assert valid.valid is True
    assert valid.schema_boundary_clear is True
    assert valid.requires_review is False

    warning = validate_relation_shape(
        relation_type=_relation_rule(review_policy="pending_review"),
        constraint=_constraint(requires_review=True),
        source_entity_type_id=ENTITY_TYPE_ID,
        target_entity_type_id=TARGET_TYPE_ID,
        properties={"since": "2026-07-13"},
    )
    assert warning.valid is True
    assert warning.requires_review is True
    assert warning.reasons == (
        "relation type constraint requires review",
        "relation type review policy requires review",
    )


def test_relation_shape_handles_missing_and_mismatched_constraints():
    missing = validate_relation_shape(
        relation_type=_relation_rule(),
        constraint=None,
        source_entity_type_id=ENTITY_TYPE_ID,
        target_entity_type_id=TARGET_TYPE_ID,
        properties={"since": "2026-07-13"},
    )
    assert missing.valid is False
    assert missing.reasons == ("active relation_type_constraint not found",)

    mismatch = validate_relation_shape(
        relation_type=_relation_rule(),
        constraint=_constraint(),
        source_entity_type_id=TARGET_TYPE_ID,
        target_entity_type_id=ENTITY_TYPE_ID,
        properties={"since": "2026-07-13"},
    )
    assert mismatch.valid is False
    assert mismatch.reasons == ("relation endpoint types do not match constraint",)

    related_to = validate_relation_shape(
        relation_type=_relation_rule(key="related_to"),
        constraint=None,
        source_entity_type_id=ENTITY_TYPE_ID,
        target_entity_type_id=TARGET_TYPE_ID,
        properties={"since": "2026-07-13"},
    )
    assert related_to.valid is True
    assert related_to.schema_boundary_clear is False
    assert related_to.requires_review is True


def test_rule_dtos_are_frozen():
    rule = _entity_rule()
    with pytest.raises(FrozenInstanceError):
        rule.key = "changed"  # type: ignore[misc]
