from __future__ import annotations

import asyncio
import operator
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators


LIB_ID = uuid.uuid4()
OTHER_LIB_ID = uuid.uuid4()
ONTOLOGY_ID = uuid.uuid4()
OTHER_ONTOLOGY_ID = uuid.uuid4()
PERSON_TYPE_ID = uuid.uuid4()
DEPARTMENT_TYPE_ID = uuid.uuid4()
POLICY_TYPE_ID = uuid.uuid4()
PROCESS_TYPE_ID = uuid.uuid4()
BELONGS_TO_TYPE_ID = uuid.uuid4()
APPLIES_TO_TYPE_ID = uuid.uuid4()
CONSTRAINS_TYPE_ID = uuid.uuid4()
RELATED_TO_TYPE_ID = uuid.uuid4()
PERSON_ID = uuid.uuid4()
DEPARTMENT_ID = uuid.uuid4()
POLICY_ID = uuid.uuid4()
PROCESS_ID = uuid.uuid4()
RELATION_ID = uuid.uuid4()
RELATION_EVIDENCE_ID = uuid.uuid4()
EVIDENCE_ID = uuid.uuid4()


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
            column_name = criterion.left.name
            expected = criterion.right.value
            if criterion.operator is operator.eq:
                rows = [row for row in rows if getattr(row, column_name) == expected]
            elif criterion.operator is operators.in_op:
                rows = [row for row in rows if getattr(row, column_name) in expected]
            else:
                raise AssertionError(f"unsupported fake criterion: {criterion}")
        return FakeResult(rows)

    def add(self, obj) -> None:
        self.added.append(obj)
        self.add_existing(obj)

    async def flush(self) -> None:
        self.flush_count += 1


def _lib(library_id: uuid.UUID = LIB_ID):
    return SimpleNamespace(id=library_id)


def _ontology(*, library_id: uuid.UUID = LIB_ID, ontology_id: uuid.UUID = ONTOLOGY_ID, status: str = "active"):
    from app.models.ontology_version import OntologyVersion

    return OntologyVersion(
        id=ontology_id,
        library_id=library_id,
        version_key="enterprise",
        version_no=1,
        status=status,
    )


def _entity_type(
    key: str,
    *,
    entity_type_id: uuid.UUID,
    properties_schema: dict | None = None,
    status: str = "active",
):
    from app.models.entity_type import EntityType

    return EntityType(
        id=entity_type_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        key=key,
        label=key.title(),
        properties_schema=properties_schema,
        is_seeded=True,
        status=status,
    )


def _relation_type(
    key: str,
    *,
    relation_type_id: uuid.UUID,
    default_review_policy: str = "auto_active",
    requires_evidence: bool = True,
    properties_schema: dict | None = None,
    status: str = "active",
):
    from app.models.relation_type import RelationType

    return RelationType(
        id=relation_type_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        key=key,
        label=key.replace("_", " ").title(),
        direction="directed",
        requires_evidence=requires_evidence,
        default_review_policy=default_review_policy,
        properties_schema=properties_schema,
        is_seeded=True,
        status=status,
    )


def _constraint(
    *,
    relation_type_id: uuid.UUID,
    source_entity_type_id: uuid.UUID,
    target_entity_type_id: uuid.UUID,
    requires_review: bool = False,
    status: str = "active",
):
    from app.models.relation_type_constraint import RelationTypeConstraint

    return RelationTypeConstraint(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=relation_type_id,
        source_entity_type_id=source_entity_type_id,
        target_entity_type_id=target_entity_type_id,
        cardinality="many_to_many",
        requires_review=requires_review,
        status=status,
    )


def _attribute_definition(
    *,
    owner_kind: str,
    owner_type_id: uuid.UUID,
    key: str,
    value_type: str,
    required: bool = False,
    enum_values: list | None = None,
    status: str = "active",
):
    from app.models.attribute_definition import AttributeDefinition

    return AttributeDefinition(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        owner_kind=owner_kind,
        owner_type_id=owner_type_id,
        key=key,
        label=key.replace("_", " ").title(),
        value_type=value_type,
        required=required,
        enum_values=enum_values,
        indexed=False,
        status=status,
    )


def _entity(
    *,
    entity_id: uuid.UUID,
    entity_type_id: uuid.UUID,
    canonical_name: str,
    status: str = "active",
):
    from app.models.entity import Entity

    return Entity(
        id=entity_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=entity_type_id,
        canonical_name=canonical_name,
        normalized_name=" ".join(canonical_name.strip().casefold().split()),
        properties={},
        status=status,
        source_type="manual",
    )


def _relation_evidence(*, status: str = "active"):
    from app.models.relation_evidence import RelationEvidence

    return RelationEvidence(
        id=RELATION_EVIDENCE_ID,
        library_id=LIB_ID,
        relation_id=RELATION_ID,
        evidence_id=EVIDENCE_ID,
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        support_type="supports",
        status=status,
    )


def _base_schema_rows():
    return [
        _ontology(),
        _entity_type("person", entity_type_id=PERSON_TYPE_ID),
        _entity_type("department", entity_type_id=DEPARTMENT_TYPE_ID),
        _entity_type("policy", entity_type_id=POLICY_TYPE_ID),
        _entity_type("process", entity_type_id=PROCESS_TYPE_ID),
        _relation_type("belongs_to", relation_type_id=BELONGS_TO_TYPE_ID),
        _relation_type("applies_to", relation_type_id=APPLIES_TO_TYPE_ID),
        _relation_type("constrains", relation_type_id=CONSTRAINS_TYPE_ID),
        _relation_type(
            "related_to",
            relation_type_id=RELATED_TO_TYPE_ID,
            default_review_policy="pending_review",
        ),
        _constraint(
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_type_id=PERSON_TYPE_ID,
            target_entity_type_id=DEPARTMENT_TYPE_ID,
        ),
        _constraint(
            relation_type_id=APPLIES_TO_TYPE_ID,
            source_entity_type_id=POLICY_TYPE_ID,
            target_entity_type_id=DEPARTMENT_TYPE_ID,
        ),
        _constraint(
            relation_type_id=CONSTRAINS_TYPE_ID,
            source_entity_type_id=POLICY_TYPE_ID,
            target_entity_type_id=PROCESS_TYPE_ID,
        ),
        _constraint(
            relation_type_id=RELATED_TO_TYPE_ID,
            source_entity_type_id=PERSON_TYPE_ID,
            target_entity_type_id=POLICY_TYPE_ID,
            requires_review=True,
        ),
    ]


def _base_graph_rows(*, person_status: str = "active", department_status: str = "active"):
    return [
        _entity(entity_id=PERSON_ID, entity_type_id=PERSON_TYPE_ID, canonical_name="Alice", status=person_status),
        _entity(
            entity_id=DEPARTMENT_ID,
            entity_type_id=DEPARTMENT_TYPE_ID,
            canonical_name="Engineering",
            status=department_status,
        ),
        _entity(entity_id=POLICY_ID, entity_type_id=POLICY_TYPE_ID, canonical_name="Travel Policy"),
        _entity(entity_id=PROCESS_ID, entity_type_id=PROCESS_TYPE_ID, canonical_name="Expense Process"),
    ]


def test_entity_validation_normalizes_name_and_does_not_write():
    from app.services import graph_schema_validator

    db = FakeDB(*_base_schema_rows())

    result = asyncio.run(
        graph_schema_validator.validate_entity_write(
            db,
            _lib(),
            ontology_version_id=ONTOLOGY_ID,
            entity_type_id=PERSON_TYPE_ID,
            canonical_name="  Alice   Zhang  ",
            properties={},
            requested_status="active",
            source_type="manual",
        )
    )

    assert result.status == "active"
    assert result.normalized_name == "alice zhang"
    assert result.entity_type.key == "person"
    assert db.added == []
    assert db.flush_count == 0


def test_entity_validation_rejects_missing_required_wrong_type_and_bad_enum_attributes():
    from app.services import graph_schema_validator

    db = FakeDB(
        *_base_schema_rows(),
        _attribute_definition(
            owner_kind="entity_type",
            owner_type_id=PERSON_TYPE_ID,
            key="employee_id",
            value_type="string",
            required=True,
        ),
        _attribute_definition(
            owner_kind="entity_type",
            owner_type_id=PERSON_TYPE_ID,
            key="level",
            value_type="integer",
        ),
        _attribute_definition(
            owner_kind="entity_type",
            owner_type_id=PERSON_TYPE_ID,
            key="employment_type",
            value_type="enum",
            enum_values=["full_time", "contractor"],
        ),
    )

    with pytest.raises(ValueError, match="missing required attribute: employee_id"):
        asyncio.run(
            graph_schema_validator.validate_entity_write(
                db,
                _lib(),
                ontology_version_id=ONTOLOGY_ID,
                entity_type_id=PERSON_TYPE_ID,
                canonical_name="Alice",
                properties={},
            )
        )

    with pytest.raises(ValueError, match="attribute level must be integer"):
        asyncio.run(
            graph_schema_validator.validate_entity_write(
                db,
                _lib(),
                ontology_version_id=ONTOLOGY_ID,
                entity_type_id=PERSON_TYPE_ID,
                canonical_name="Alice",
                properties={"employee_id": "E-1", "level": "senior"},
            )
        )

    with pytest.raises(ValueError, match="attribute employment_type must be one of"):
        asyncio.run(
            graph_schema_validator.validate_entity_write(
                db,
                _lib(),
                ontology_version_id=ONTOLOGY_ID,
                entity_type_id=PERSON_TYPE_ID,
                canonical_name="Alice",
                properties={"employee_id": "E-1", "employment_type": "vendor"},
            )
        )


def test_entity_validation_rejects_duplicate_active_same_name_same_type():
    from app.services import graph_schema_validator

    db = FakeDB(
        *_base_schema_rows(),
        _entity(entity_id=PERSON_ID, entity_type_id=PERSON_TYPE_ID, canonical_name="Alice"),
    )

    with pytest.raises(ValueError, match="duplicate active entity"):
        asyncio.run(
            graph_schema_validator.validate_entity_write(
                db,
                _lib(),
                ontology_version_id=ONTOLOGY_ID,
                entity_type_id=PERSON_TYPE_ID,
                canonical_name="  ALICE ",
                properties={},
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_relation_validation_allows_seed_constraints_to_be_active_when_evidence_exists():
    from app.services import graph_schema_validator

    db = FakeDB(*_base_schema_rows(), *_base_graph_rows())

    belongs_to = asyncio.run(
        graph_schema_validator.validate_relation_write(
            db,
            _lib(),
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            active_evidence_count=1,
        )
    )
    applies_to = asyncio.run(
        graph_schema_validator.validate_relation_write(
            db,
            _lib(),
            relation_type_id=APPLIES_TO_TYPE_ID,
            source_entity_id=POLICY_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            active_evidence_count=1,
        )
    )

    assert belongs_to.status == "active"
    assert belongs_to.review_status == "not_required"
    assert applies_to.status == "active"
    assert db.added == []
    assert db.flush_count == 0


def test_relation_validation_rejects_invalid_source_target_constraint():
    from app.services import graph_schema_validator

    db = FakeDB(*_base_schema_rows(), *_base_graph_rows())

    with pytest.raises(ValueError, match="active relation_type_constraint"):
        asyncio.run(
            graph_schema_validator.validate_relation_write(
                db,
                _lib(),
                relation_type_id=APPLIES_TO_TYPE_ID,
                source_entity_id=PERSON_ID,
                target_entity_id=POLICY_ID,
                requested_status="active",
                active_evidence_count=1,
            )
        )


def test_relation_validation_requires_evidence_for_active_create_and_query_for_activation():
    from app.services import graph_schema_validator

    db_without_evidence = FakeDB(*_base_schema_rows(), *_base_graph_rows())
    create_result = asyncio.run(
        graph_schema_validator.validate_relation_write(
            db_without_evidence,
            _lib(),
            relation_type_id=CONSTRAINS_TYPE_ID,
            source_entity_id=POLICY_ID,
            target_entity_id=PROCESS_ID,
            requested_status="active",
        )
    )

    db_with_evidence = FakeDB(*_base_schema_rows(), *_base_graph_rows(), _relation_evidence(status="active"))
    activate_result = asyncio.run(
        graph_schema_validator.validate_relation_write(
            db_with_evidence,
            _lib(),
            relation_type_id=CONSTRAINS_TYPE_ID,
            source_entity_id=POLICY_ID,
            target_entity_id=PROCESS_ID,
            requested_status="active",
            write_mode="activate",
            relation_id=RELATION_ID,
        )
    )

    assert create_result.status == "pending_review"
    assert "requires active evidence" in create_result.reasons
    assert activate_result.status == "active"
    assert activate_result.active_evidence_count == 1


def test_relation_validation_forces_related_to_pending_review_even_with_evidence():
    from app.services import graph_schema_validator

    db = FakeDB(*_base_schema_rows(), *_base_graph_rows())

    result = asyncio.run(
        graph_schema_validator.validate_relation_write(
            db,
            _lib(),
            relation_type_id=RELATED_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=POLICY_ID,
            requested_status="active",
            active_evidence_count=1,
        )
    )

    assert result.status == "pending_review"
    assert result.review_status == "pending_review"
    assert "related_to cannot be automatically active" in result.reasons


def test_relation_validation_downgrades_active_for_low_confidence_unclear_schema_or_non_active_entities():
    from app.services import graph_schema_validator

    low_confidence_db = FakeDB(*_base_schema_rows(), *_base_graph_rows())
    low_confidence = asyncio.run(
        graph_schema_validator.validate_relation_write(
            low_confidence_db,
            _lib(),
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            confidence=0.42,
            active_evidence_count=1,
        )
    )

    unclear_schema = asyncio.run(
        graph_schema_validator.validate_relation_write(
            low_confidence_db,
            _lib(),
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            schema_boundary_clear=False,
            active_evidence_count=1,
        )
    )

    draft_entity_db = FakeDB(*_base_schema_rows(), *_base_graph_rows(person_status="draft"))
    draft_entity = asyncio.run(
        graph_schema_validator.validate_relation_write(
            draft_entity_db,
            _lib(),
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            active_evidence_count=1,
        )
    )

    stale_entity_db = FakeDB(*_base_schema_rows(), *_base_graph_rows(department_status="stale"))
    stale_entity = asyncio.run(
        graph_schema_validator.validate_relation_write(
            stale_entity_db,
            _lib(),
            relation_type_id=BELONGS_TO_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=DEPARTMENT_ID,
            requested_status="active",
            active_evidence_count=1,
        )
    )

    assert low_confidence.status == "pending_review"
    assert "low confidence cannot be active" in low_confidence.reasons
    assert unclear_schema.status == "pending_review"
    assert "schema boundary is unclear" in unclear_schema.reasons
    assert draft_entity.status == "pending_review"
    assert "non-active entity cannot produce active relation" in draft_entity.reasons
    assert stale_entity.status == "pending_review"
    assert "non-active entity cannot produce active relation" in stale_entity.reasons


def test_relation_validation_rejects_required_relation_attribute_type_errors():
    from app.services import graph_schema_validator

    db = FakeDB(
        *_base_schema_rows(),
        *_base_graph_rows(),
        _attribute_definition(
            owner_kind="relation_type",
            owner_type_id=BELONGS_TO_TYPE_ID,
            key="since",
            value_type="date",
            required=True,
        ),
        _attribute_definition(
            owner_kind="relation_type",
            owner_type_id=BELONGS_TO_TYPE_ID,
            key="weight",
            value_type="number",
        ),
    )

    with pytest.raises(ValueError, match="missing required attribute: since"):
        asyncio.run(
            graph_schema_validator.validate_relation_write(
                db,
                _lib(),
                relation_type_id=BELONGS_TO_TYPE_ID,
                source_entity_id=PERSON_ID,
                target_entity_id=DEPARTMENT_ID,
                properties={},
                requested_status="active",
                active_evidence_count=1,
            )
        )

    with pytest.raises(ValueError, match="attribute weight must be number"):
        asyncio.run(
            graph_schema_validator.validate_relation_write(
                db,
                _lib(),
                relation_type_id=BELONGS_TO_TYPE_ID,
                source_entity_id=PERSON_ID,
                target_entity_id=DEPARTMENT_ID,
                properties={"since": "2026-07-09", "weight": "high"},
                requested_status="active",
                active_evidence_count=1,
            )
        )
