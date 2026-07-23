from __future__ import annotations

import uuid
from copy import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.schema_lifecycle_actions import clone_schema_bundle_rows
from app.services.schema_lifecycle_contracts import SchemaLifecycleError
from app.services.schema_lifecycle_impact import schema_bundle_diff
from app.services.schema_lifecycle_read import (
    SchemaVersionBundle,
    schema_version_detail,
    schema_version_state_hash,
)
from app.services.schema_lifecycle_validation import validate_schema_draft_bundle


LIBRARY_ID = uuid.uuid4()
SOURCE_VERSION_ID = uuid.uuid4()
DRAFT_VERSION_ID = uuid.uuid4()
ENTITY_A_ID = uuid.uuid4()
ENTITY_B_ID = uuid.uuid4()
RELATION_ID = uuid.uuid4()
ATTRIBUTE_ID = uuid.uuid4()
CONSTRAINT_ID = uuid.uuid4()


def _source_bundle() -> SchemaVersionBundle:
    version = OntologyVersion(
        id=SOURCE_VERSION_ID,
        library_id=LIBRARY_ID,
        version_key="enterprise",
        version_no=1,
        status="active",
        description="Active enterprise schema",
    )
    company = EntityType(
        id=ENTITY_A_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=SOURCE_VERSION_ID,
        key="company",
        label="Company",
        properties_schema={"type": "object"},
        is_seeded=True,
        status="active",
    )
    project = EntityType(
        id=ENTITY_B_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=SOURCE_VERSION_ID,
        key="project",
        label="Project",
        properties_schema=None,
        is_seeded=False,
        status="active",
    )
    relation = RelationType(
        id=RELATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=SOURCE_VERSION_ID,
        key="invests_in",
        label="Invests In",
        description=None,
        direction="directed",
        requires_evidence=True,
        default_review_policy="pending_review",
        properties_schema={"type": "object"},
        is_seeded=False,
        status="active",
    )
    attribute = AttributeDefinition(
        id=ATTRIBUTE_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=SOURCE_VERSION_ID,
        owner_kind="entity_type",
        owner_type_id=ENTITY_A_ID,
        key="registration_no",
        label="Registration Number",
        value_type="string",
        required=True,
        enum_values=None,
        validation_schema={"minLength": 1},
        indexed=True,
        status="active",
    )
    constraint = RelationTypeConstraint(
        id=CONSTRAINT_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=SOURCE_VERSION_ID,
        relation_type_id=RELATION_ID,
        source_entity_type_id=ENTITY_A_ID,
        target_entity_type_id=ENTITY_B_ID,
        cardinality="many_to_many",
        requires_review=True,
        status="active",
    )
    return SchemaVersionBundle(
        version=version,
        entity_types=(company, project),
        relation_types=(relation,),
        attributes=(attribute,),
        constraints=(constraint,),
    )


def test_version_projection_is_stable_and_retains_scope_identity():
    bundle = _source_bundle()
    library = SimpleNamespace(id=LIBRARY_ID, slug="enterprise-kb")
    detail = schema_version_detail(library, bundle)
    assert detail.library_id == LIBRARY_ID
    assert detail.library_slug == "enterprise-kb"
    assert detail.id == SOURCE_VERSION_ID
    assert detail.state_hash == schema_version_state_hash(bundle)
    assert detail.entity_type_count == 2
    assert detail.relation_type_count == 1
    assert detail.attribute_count == 1
    assert detail.constraint_count == 1
    assert detail.constraints[0].relation_type_key == "invests_in"
    assert detail.constraints[0].source_entity_type_key == "company"
    assert detail.constraints[0].target_entity_type_key == "project"

    changed_type = copy(bundle.entity_types[0])
    changed_type.label = "Legal Entity"
    changed = replace(
        bundle,
        entity_types=(changed_type, *bundle.entity_types[1:]),
    )
    assert schema_version_state_hash(changed) != detail.state_hash


def test_clone_rows_are_complete_deterministic_and_independent():
    source = _source_bundle()
    first = clone_schema_bundle_rows(
        source,
        draft_version_id=DRAFT_VERSION_ID,
        version_no=2,
        description="Draft schema",
    )
    second = clone_schema_bundle_rows(
        source,
        draft_version_id=DRAFT_VERSION_ID,
        version_no=2,
        description="Draft schema",
    )
    assert first.version.id == DRAFT_VERSION_ID
    assert first.version.parent_version_id == SOURCE_VERSION_ID
    assert first.version.status == "draft"
    assert first.version.version_no == 2
    assert [row.id for row in first.entity_types] == [
        row.id for row in second.entity_types
    ]
    assert first.relation_types[0].id == second.relation_types[0].id
    assert first.attributes[0].id == second.attributes[0].id
    assert first.constraints[0].id == second.constraints[0].id

    cloned_entity_ids = {row.id for row in first.entity_types}
    assert cloned_entity_ids.isdisjoint({ENTITY_A_ID, ENTITY_B_ID})
    assert first.relation_types[0].id != RELATION_ID
    assert first.attributes[0].owner_type_id in cloned_entity_ids
    assert first.attributes[0].owner_type_id != ENTITY_A_ID
    assert first.constraints[0].relation_type_id == first.relation_types[0].id
    assert first.constraints[0].source_entity_type_id in cloned_entity_ids
    assert first.constraints[0].target_entity_type_id in cloned_entity_ids
    assert all(row.status == "draft" for row in first.entity_types)
    assert all(row.status == "draft" for row in first.relation_types)
    assert source.entity_types[0].status == "active"
    assert source.attributes[0].owner_type_id == ENTITY_A_ID


def test_clone_fails_closed_when_a_reference_cannot_be_remapped():
    source = _source_bundle()
    broken_attribute = copy(source.attributes[0])
    broken_attribute.owner_type_id = uuid.uuid4()
    broken = replace(source, attributes=(broken_attribute,))
    with pytest.raises(SchemaLifecycleError) as error:
        clone_schema_bundle_rows(
            broken,
            draft_version_id=DRAFT_VERSION_ID,
            version_no=2,
            description=None,
        )
    assert error.value.code == "schema_lifecycle_unavailable"


def test_complete_draft_validation_reports_bounded_stable_references():
    draft = clone_schema_bundle_rows(
        _source_bundle(),
        draft_version_id=DRAFT_VERSION_ID,
        version_no=2,
        description="Draft",
    )
    library = SimpleNamespace(id=LIBRARY_ID, slug="enterprise-kb")
    valid = validate_schema_draft_bundle(library, draft)
    assert valid.valid
    assert valid.issues == []
    assert valid.version_state_hash == schema_version_state_hash(draft)

    broken_owner = copy(draft.attributes[0])
    broken_owner.owner_type_id = uuid.uuid4()
    broken_enum = copy(draft.attributes[0])
    broken_enum.id = uuid.uuid4()
    broken_enum.key = "kind"
    broken_enum.value_type = "enum"
    broken_enum.enum_values = None
    broken = replace(draft, attributes=(broken_owner, broken_enum))
    validation = validate_schema_draft_bundle(library, broken)
    assert not validation.valid
    assert [(item.code, item.item_kind, item.field) for item in validation.issues] == [
        ("attribute_enum_values_required", "attribute", "enum_values"),
        ("attribute_owner_missing", "attribute", "owner_type_id"),
    ]


def test_schema_diff_uses_semantic_keys_not_cloned_row_ids():
    source = _source_bundle()
    draft = clone_schema_bundle_rows(
        source,
        draft_version_id=DRAFT_VERSION_ID,
        version_no=2,
        description="Draft",
    )
    unchanged = schema_bundle_diff(draft, source)
    assert all(not group.added for group in unchanged.values())
    assert all(not group.removed for group in unchanged.values())
    assert all(not group.changed for group in unchanged.values())

    changed_company = copy(draft.entity_types[0])
    changed_company.label = "Legal Entity"
    new_type = EntityType(
        id=uuid.uuid4(),
        library_id=LIBRARY_ID,
        ontology_version_id=DRAFT_VERSION_ID,
        key="person",
        label="Person",
        status="draft",
    )
    changed = replace(
        draft,
        entity_types=(changed_company, draft.entity_types[1], new_type),
    )
    diff = schema_bundle_diff(changed, source)
    assert diff["entity_types"].added == ["person"]
    assert diff["entity_types"].removed == []
    assert diff["entity_types"].changed == ["company"]
