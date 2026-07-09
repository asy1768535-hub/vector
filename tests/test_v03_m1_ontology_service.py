from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest


LIB_ID = uuid.uuid4()
OTHER_LIB_ID = uuid.uuid4()
ONTOLOGY_ID = uuid.uuid4()
ACTIVE_ONTOLOGY_ID = uuid.uuid4()
NEW_ONTOLOGY_ID = uuid.uuid4()
ENTITY_TYPE_ID = uuid.uuid4()
RELATION_TYPE_ID = uuid.uuid4()
SOURCE_TYPE_ID = uuid.uuid4()
TARGET_TYPE_ID = uuid.uuid4()
ATTRIBUTE_ID = uuid.uuid4()


class FakeDB:
    def __init__(self, *objects):
        self.objects = {(type(obj), obj.id): obj for obj in objects}
        self.added = []
        self.flush_count = 0

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    def add(self, obj) -> None:
        self.added.append(obj)
        self.objects[(type(obj), obj.id)] = obj

    async def flush(self) -> None:
        self.flush_count += 1


def _lib(library_id: uuid.UUID = LIB_ID):
    return SimpleNamespace(id=library_id)


def _ontology(*, ontology_id: uuid.UUID = ONTOLOGY_ID, library_id: uuid.UUID = LIB_ID, status: str = "draft"):
    from app.models.ontology_version import OntologyVersion

    return OntologyVersion(
        id=ontology_id,
        library_id=library_id,
        version_key="enterprise",
        version_no=1,
        status=status,
    )


def _entity_type(
    *,
    entity_type_id: uuid.UUID = ENTITY_TYPE_ID,
    ontology_version_id: uuid.UUID = ONTOLOGY_ID,
    library_id: uuid.UUID = LIB_ID,
    status: str = "draft",
):
    from app.models.entity_type import EntityType

    return EntityType(
        id=entity_type_id,
        library_id=library_id,
        ontology_version_id=ontology_version_id,
        key="person",
        label="Person",
        status=status,
    )


def _relation_type(
    *,
    relation_type_id: uuid.UUID = RELATION_TYPE_ID,
    ontology_version_id: uuid.UUID = ONTOLOGY_ID,
    library_id: uuid.UUID = LIB_ID,
    status: str = "draft",
):
    from app.models.relation_type import RelationType

    return RelationType(
        id=relation_type_id,
        library_id=library_id,
        ontology_version_id=ontology_version_id,
        key="belongs_to",
        label="Belongs To",
        direction="directed",
        requires_evidence=True,
        default_review_policy="pending_review",
        status=status,
    )


def _attribute_definition(
    *,
    attribute_id: uuid.UUID = ATTRIBUTE_ID,
    ontology_version_id: uuid.UUID = ONTOLOGY_ID,
    owner_type_id: uuid.UUID = ENTITY_TYPE_ID,
    library_id: uuid.UUID = LIB_ID,
    status: str = "draft",
):
    from app.models.attribute_definition import AttributeDefinition

    return AttributeDefinition(
        id=attribute_id,
        library_id=library_id,
        ontology_version_id=ontology_version_id,
        owner_kind="entity_type",
        owner_type_id=owner_type_id,
        key="employee_no",
        label="Employee No",
        value_type="string",
        status=status,
    )


def test_create_entity_type_defaults_to_draft_under_draft_ontology():
    from app.services import ontology

    version = _ontology()
    db = FakeDB(version)

    row = asyncio.run(
        ontology.create_entity_type(
            db,
            _lib(),
            ONTOLOGY_ID,
            key="department",
            label="Department",
        )
    )

    assert row.status == "draft"
    assert row.library_id == LIB_ID
    assert row.ontology_version_id == ONTOLOGY_ID
    assert db.added == [row]
    assert db.flush_count == 1


def test_update_entity_type_allows_draft_child_under_draft_ontology():
    from app.services import ontology

    version = _ontology(status="draft")
    row = _entity_type(status="draft")
    db = FakeDB(version, row)

    updated = asyncio.run(ontology.update_entity_type(db, _lib(), ENTITY_TYPE_ID, label="People"))

    assert updated.label == "People"
    assert db.flush_count == 1


def test_update_ontology_version_rejects_active_ontology():
    from app.services import ontology

    version = _ontology(status="active")
    db = FakeDB(version)

    with pytest.raises(ValueError, match="active ontology"):
        asyncio.run(ontology.update_ontology_version(db, _lib(), ONTOLOGY_ID, description="changed"))

    assert db.flush_count == 0


def test_create_child_rejects_active_parent_ontology():
    from app.services import ontology

    version = _ontology(status="active")
    db = FakeDB(version)

    with pytest.raises(ValueError, match="active ontology"):
        asyncio.run(
            ontology.create_entity_type(
                db,
                _lib(),
                ONTOLOGY_ID,
                key="project",
                label="Project",
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_update_child_rejects_active_child_even_under_draft_ontology():
    from app.services import ontology

    version = _ontology(status="draft")
    row = _entity_type(status="active")
    db = FakeDB(version, row)

    with pytest.raises(ValueError, match="active schema"):
        asyncio.run(ontology.update_entity_type(db, _lib(), ENTITY_TYPE_ID, label="People"))

    assert row.label == "Person"
    assert db.flush_count == 0


def test_delete_child_rejects_active_child_even_under_draft_ontology():
    from app.services import ontology

    version = _ontology(status="draft")
    row = _attribute_definition(status="active")
    db = FakeDB(version, row)

    with pytest.raises(ValueError, match="active schema"):
        asyncio.run(ontology.delete_attribute_definition(db, _lib(), ATTRIBUTE_ID))

    assert row.status == "active"
    assert db.flush_count == 0


def test_update_child_rejects_active_parent_ontology():
    from app.services import ontology

    version = _ontology(status="active")
    row = _entity_type(status="draft")
    db = FakeDB(version, row)

    with pytest.raises(ValueError, match="active ontology"):
        asyncio.run(ontology.update_entity_type(db, _lib(), ENTITY_TYPE_ID, label="People"))

    assert row.label == "Person"
    assert db.flush_count == 0


def test_create_new_draft_ontology_can_reference_same_library_active_parent():
    from app.services import ontology

    parent = _ontology(ontology_id=ACTIVE_ONTOLOGY_ID, status="active")
    db = FakeDB(parent)

    child = asyncio.run(
        ontology.create_ontology_version(
            db,
            _lib(),
            version_key="enterprise",
            version_no=2,
            parent_version_id=ACTIVE_ONTOLOGY_ID,
            description="schema update",
        )
    )

    assert child.status == "draft"
    assert child.parent_version_id == ACTIVE_ONTOLOGY_ID
    assert child.library_id == LIB_ID
    assert db.added == [child]
    assert db.flush_count == 1


def test_relation_type_constraint_rejects_cross_ontology_types():
    from app.services import ontology

    version = _ontology(status="draft")
    relation_type = _relation_type()
    source = _entity_type(entity_type_id=SOURCE_TYPE_ID)
    target = _entity_type(entity_type_id=TARGET_TYPE_ID, ontology_version_id=NEW_ONTOLOGY_ID)
    db = FakeDB(version, relation_type, source, target)

    with pytest.raises(ValueError, match="same ontology"):
        asyncio.run(
            ontology.create_relation_type_constraint(
                db,
                _lib(),
                ONTOLOGY_ID,
                relation_type_id=RELATION_TYPE_ID,
                source_entity_type_id=SOURCE_TYPE_ID,
                target_entity_type_id=TARGET_TYPE_ID,
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_attribute_definition_rejects_cross_library_entity_owner():
    from app.services import ontology

    version = _ontology(status="draft")
    owner = _entity_type(library_id=OTHER_LIB_ID)
    db = FakeDB(version, owner)

    with pytest.raises(ValueError, match="same library"):
        asyncio.run(
            ontology.create_attribute_definition(
                db,
                _lib(),
                ONTOLOGY_ID,
                owner_kind="entity_type",
                owner_type_id=ENTITY_TYPE_ID,
                key="employee_no",
                label="Employee No",
                value_type="string",
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_attribute_definition_accepts_relation_type_owner_in_same_scope():
    from app.services import ontology

    version = _ontology(status="draft")
    owner = _relation_type()
    db = FakeDB(version, owner)

    row = asyncio.run(
        ontology.create_attribute_definition(
            db,
            _lib(),
            ONTOLOGY_ID,
            owner_kind="relation_type",
            owner_type_id=RELATION_TYPE_ID,
            key="since",
            label="Since",
            value_type="date",
        )
    )

    assert row.owner_kind == "relation_type"
    assert row.owner_type_id == RELATION_TYPE_ID
    assert row.status == "draft"
    assert db.added == [row]
    assert db.flush_count == 1
