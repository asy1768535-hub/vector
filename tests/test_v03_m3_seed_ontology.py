from __future__ import annotations

import asyncio
import operator
import uuid
from types import SimpleNamespace

import pytest


LIB_ID = uuid.uuid4()
ONTOLOGY_ID = uuid.uuid4()


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeDB:
    def __init__(self, *objects):
        self.objects = {}
        self.added = []
        self.flush_count = 0
        self.child_add_parent_statuses = []
        for obj in objects:
            self.add_existing(obj)

    def add_existing(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.objects[(type(obj), obj.id)] = obj

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, stmt):
        model = stmt.column_descriptions[0]["entity"]
        rows = [obj for (obj_model, _), obj in self.objects.items() if obj_model is model]
        for criterion in stmt._where_criteria:
            if criterion.operator is not operator.eq:
                raise AssertionError(f"unsupported fake criterion: {criterion}")
            column_name = criterion.left.name
            expected = criterion.right.value
            rows = [row for row in rows if getattr(row, column_name) == expected]
        return FakeResult(rows)

    def add(self, obj) -> None:
        self.added.append(obj)
        parent_status = self._parent_ontology_status(obj)
        if parent_status is not None:
            self.child_add_parent_statuses.append(parent_status)
        self.objects[(type(obj), getattr(obj, "id", None))] = obj

    async def flush(self) -> None:
        self.flush_count += 1
        for obj in list(self.added):
            if getattr(obj, "id", None) is None:
                self.objects.pop((type(obj), None), None)
                obj.id = uuid.uuid4()
                self.objects[(type(obj), obj.id)] = obj

    def rows(self, model):
        return [obj for (obj_model, _), obj in self.objects.items() if obj_model is model]

    def _parent_ontology_status(self, obj) -> str | None:
        from app.models.attribute_definition import AttributeDefinition
        from app.models.entity_type import EntityType
        from app.models.ontology_version import OntologyVersion
        from app.models.relation_type import RelationType
        from app.models.relation_type_constraint import RelationTypeConstraint

        if not isinstance(obj, (AttributeDefinition, EntityType, RelationType, RelationTypeConstraint)):
            return None
        ontology = self.objects.get((OntologyVersion, obj.ontology_version_id))
        return None if ontology is None else ontology.status


def _lib():
    return SimpleNamespace(id=LIB_ID)


def _counts(db: FakeDB) -> dict[str, int]:
    from app.models.attribute_definition import AttributeDefinition
    from app.models.entity_type import EntityType
    from app.models.ontology_version import OntologyVersion
    from app.models.relation_type import RelationType
    from app.models.relation_type_constraint import RelationTypeConstraint

    return {
        "ontology_versions": len(db.rows(OntologyVersion)),
        "entity_types": len(db.rows(EntityType)),
        "relation_types": len(db.rows(RelationType)),
        "relation_type_constraints": len(db.rows(RelationTypeConstraint)),
        "attribute_definitions": len(db.rows(AttributeDefinition)),
    }


def _active_ontology():
    from app.models.ontology_version import OntologyVersion

    return OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIB_ID,
        version_key="enterprise",
        version_no=1,
        status="active",
    )


def _populate_complete_seed(db: FakeDB, *, related_to_requires_evidence: bool = True) -> None:
    from app.models.attribute_definition import AttributeDefinition
    from app.models.entity_type import EntityType
    from app.models.relation_type import RelationType
    from app.models.relation_type_constraint import RelationTypeConstraint
    from app.services import graph_seed

    ontology = _active_ontology()
    db.add_existing(ontology)

    entity_types = {}
    for spec in graph_seed.DEFAULT_ENTITY_TYPES:
        row = EntityType(
            id=uuid.uuid4(),
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key=spec.key,
            label=spec.label,
            description=spec.description,
            properties_schema=spec.properties_schema,
            is_seeded=True,
            status="active",
        )
        db.add_existing(row)
        entity_types[spec.key] = row

    relation_types = {}
    for spec in graph_seed.DEFAULT_RELATION_TYPES:
        requires_evidence = related_to_requires_evidence if spec.key == "related_to" else spec.requires_evidence
        row = RelationType(
            id=uuid.uuid4(),
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key=spec.key,
            label=spec.label,
            description=spec.description,
            direction=spec.direction,
            requires_evidence=requires_evidence,
            default_review_policy=spec.default_review_policy,
            properties_schema=spec.properties_schema,
            is_seeded=True,
            status="active",
        )
        db.add_existing(row)
        relation_types[spec.key] = row

    for spec in graph_seed.expanded_default_relation_constraints():
        db.add_existing(
            RelationTypeConstraint(
                id=uuid.uuid4(),
                library_id=LIB_ID,
                ontology_version_id=ONTOLOGY_ID,
                relation_type_id=relation_types[spec.relation_type_key].id,
                source_entity_type_id=entity_types[spec.source_entity_type_key].id,
                target_entity_type_id=entity_types[spec.target_entity_type_key].id,
                cardinality=spec.cardinality,
                requires_review=spec.requires_review,
                status="active",
            )
        )

    for spec in graph_seed.DEFAULT_ATTRIBUTE_DEFINITIONS:
        owner = entity_types[spec.owner_type_key]
        db.add_existing(
            AttributeDefinition(
                id=uuid.uuid4(),
                library_id=LIB_ID,
                ontology_version_id=ONTOLOGY_ID,
                owner_kind=spec.owner_kind,
                owner_type_id=owner.id,
                key=spec.key,
                label=spec.label,
                value_type=spec.value_type,
                required=spec.required,
                enum_values=spec.enum_values,
                validation_schema=spec.validation_schema,
                indexed=spec.indexed,
                status="active",
            )
        )


def test_default_seed_data_matches_m3_scope_and_related_to_policy():
    from app.models.relation_type import REVIEW_POLICY_AUTO_ACTIVE, REVIEW_POLICY_PENDING_REVIEW
    from app.services import graph_seed

    entity_keys = {spec.key for spec in graph_seed.DEFAULT_ENTITY_TYPES}
    assert {
        "person",
        "department",
        "position",
        "policy",
        "process",
        "project",
        "product",
        "customer",
    } <= entity_keys

    relation_specs = {spec.key: spec for spec in graph_seed.DEFAULT_RELATION_TYPES}
    assert {
        "belongs_to",
        "responsible_for",
        "applies_to",
        "constrains",
        "references",
        "approves",
        "depends_on",
        "contains",
        "related_to",
    } <= set(relation_specs)

    related_to = relation_specs["related_to"]
    assert related_to.requires_evidence is True
    assert related_to.default_review_policy == REVIEW_POLICY_PENDING_REVIEW
    assert related_to.default_review_policy != REVIEW_POLICY_AUTO_ACTIVE

    contains = relation_specs["contains"]
    assert contains.requires_evidence is True
    assert contains.default_review_policy == REVIEW_POLICY_AUTO_ACTIVE


def test_seed_creates_draft_ontology_children_before_activating():
    from app.models.attribute_definition import AttributeDefinition
    from app.models.entity_type import EntityType
    from app.models.relation_type import RelationType
    from app.models.relation_type_constraint import RelationTypeConstraint
    from app.services import graph_seed

    db = FakeDB()

    result = asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))

    assert result.ontology_version.status == "active"
    assert result.ontology_version.published_at is not None
    assert db.child_add_parent_statuses
    assert set(db.child_add_parent_statuses) == {"draft"}

    assert {row.key for row in db.rows(EntityType)} >= {"person", "department", "position"}
    assert {row.key for row in db.rows(RelationType)} >= {"belongs_to", "related_to"}
    assert len(db.rows(RelationTypeConstraint)) == len(graph_seed.expanded_default_relation_constraints())
    assert len(db.rows(AttributeDefinition)) == len(graph_seed.DEFAULT_ATTRIBUTE_DEFINITIONS)

    assert all(row.status == "active" and row.is_seeded for row in db.rows(EntityType))
    assert all(row.status == "active" and row.is_seeded for row in db.rows(RelationType))
    assert result.created_counts["ontology_versions"] == 1
    assert result.created_counts["entity_types"] == len(graph_seed.DEFAULT_ENTITY_TYPES)


def test_seed_is_noop_when_active_enterprise_ontology_is_complete():
    from app.services import graph_seed

    db = FakeDB()
    _populate_complete_seed(db)
    before_counts = _counts(db)

    result = asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))

    assert result.ontology_version.id == ONTOLOGY_ID
    assert _counts(db) == before_counts
    assert db.added == []
    assert result.created_counts == {
        "ontology_versions": 0,
        "entity_types": 0,
        "relation_types": 0,
        "relation_type_constraints": 0,
        "attribute_definitions": 0,
    }


def test_seed_second_run_after_creation_is_idempotent():
    from app.services import graph_seed

    db = FakeDB()
    first = asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))
    counts_after_first = _counts(db)
    added_after_first = len(db.added)

    second = asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))

    assert second.ontology_version.id == first.ontology_version.id
    assert _counts(db) == counts_after_first
    assert len(db.added) == added_after_first
    assert all(value == 0 for value in second.created_counts.values())


def test_active_enterprise_ontology_missing_seed_rows_raises_without_backfill():
    from app.services import graph_seed

    db = FakeDB(_active_ontology())

    with pytest.raises(ValueError, match="active enterprise ontology is incomplete"):
        asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))

    assert db.added == []
    assert _counts(db) == {
        "ontology_versions": 1,
        "entity_types": 0,
        "relation_types": 0,
        "relation_type_constraints": 0,
        "attribute_definitions": 0,
    }


def test_active_enterprise_ontology_with_conflicting_seed_row_raises_without_fixing():
    from app.models.relation_type import RelationType
    from app.services import graph_seed

    db = FakeDB()
    _populate_complete_seed(db, related_to_requires_evidence=False)
    related_to = next(row for row in db.rows(RelationType) if row.key == "related_to")

    with pytest.raises(ValueError, match="seeded relation type conflict"):
        asyncio.run(graph_seed.seed_enterprise_ontology(db, _lib()))

    assert related_to.requires_evidence is False
    assert db.added == []
