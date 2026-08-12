from __future__ import annotations

import asyncio
import operator
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
PERSON_TYPE_ID = uuid.UUID("10000000-0000-0000-0000-000000000003")
DEPARTMENT_TYPE_ID = uuid.UUID("10000000-0000-0000-0000-000000000004")
POLICY_TYPE_ID = uuid.UUID("10000000-0000-0000-0000-000000000005")
BELONGS_TO_TYPE_ID = uuid.UUID("10000000-0000-0000-0000-000000000006")
APPLIES_TO_TYPE_ID = uuid.UUID("10000000-0000-0000-0000-000000000007")
PERSON_ID = uuid.UUID("10000000-0000-0000-0000-000000000008")
DEPARTMENT_ID = uuid.UUID("10000000-0000-0000-0000-000000000009")
POLICY_ID = uuid.UUID("10000000-0000-0000-0000-000000000010")
RELATION_ID = uuid.UUID("10000000-0000-0000-0000-000000000011")
EVIDENCE_ID = uuid.UUID("10000000-0000-0000-0000-000000000012")
DOC_ID = uuid.UUID("10000000-0000-0000-0000-000000000013")
REVISION_ID = uuid.UUID("10000000-0000-0000-0000-000000000014")


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


def _lib():
    return SimpleNamespace(id=LIB_ID, slug="lib")


def _ontology():
    from app.models.ontology_version import OntologyVersion

    return OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIB_ID,
        version_key="enterprise",
        version_no=1,
        status="active",
    )


def _entity_type(key: str, entity_type_id: uuid.UUID):
    from app.models.entity_type import EntityType

    return EntityType(
        id=entity_type_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        key=key,
        label=key.title(),
        is_seeded=True,
        status="active",
    )


def _relation_type(key: str, relation_type_id: uuid.UUID, *, requires_evidence: bool = True):
    from app.models.relation_type import RelationType

    return RelationType(
        id=relation_type_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        key=key,
        label=key.replace("_", " ").title(),
        direction="directed",
        requires_evidence=requires_evidence,
        default_review_policy="auto_active",
        is_seeded=True,
        status="active",
    )


def _constraint(relation_type_id: uuid.UUID, source_type_id: uuid.UUID, target_type_id: uuid.UUID):
    from app.models.relation_type_constraint import RelationTypeConstraint

    return RelationTypeConstraint(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=relation_type_id,
        source_entity_type_id=source_type_id,
        target_entity_type_id=target_type_id,
        cardinality="many_to_many",
        requires_review=False,
        status="active",
    )


def _entity(entity_id: uuid.UUID, entity_type_id: uuid.UUID, canonical_name: str):
    from app.models.entity import Entity

    return Entity(
        id=entity_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=entity_type_id,
        canonical_name=canonical_name,
        normalized_name=" ".join(canonical_name.strip().casefold().split()),
        status="active",
        source_type="manual",
    )


def _evidence():
    from app.models.evidence_unit import EvidenceUnit

    return EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REVISION_ID,
        evidence_kind="text",
        text_quote="Alice belongs to Engineering.",
        status="active",
    )


def _relation(status: str = "pending_review"):
    from app.models.knowledge_relation import KnowledgeRelation

    return KnowledgeRelation(
        id=RELATION_ID,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=BELONGS_TO_TYPE_ID,
        source_entity_id=PERSON_ID,
        target_entity_id=DEPARTMENT_ID,
        status=status,
        review_status="pending_review",
        source_type="manual",
    )


def _base_schema_rows():
    return [
        _ontology(),
        _entity_type("person", PERSON_TYPE_ID),
        _entity_type("department", DEPARTMENT_TYPE_ID),
        _entity_type("policy", POLICY_TYPE_ID),
        _relation_type("belongs_to", BELONGS_TO_TYPE_ID),
        _relation_type("applies_to", APPLIES_TO_TYPE_ID),
        _constraint(BELONGS_TO_TYPE_ID, PERSON_TYPE_ID, DEPARTMENT_TYPE_ID),
        _constraint(APPLIES_TO_TYPE_ID, POLICY_TYPE_ID, DEPARTMENT_TYPE_ID),
    ]


def test_create_entity_service_validates_and_writes_normalized_entity():
    from app.schemas.v03_graph import GraphEntityCreate
    from app.services import graph_entities

    db = FakeDB(*_base_schema_rows())

    row = asyncio.run(
        graph_entities.create_entity(
            db,
            _lib(),
            GraphEntityCreate(
                ontology_version_id=ONTOLOGY_ID,
                entity_type_id=PERSON_TYPE_ID,
                canonical_name="  Alice   Zhang  ",
                status="active",
                source_type="manual",
            ),
        )
    )

    assert row.library_id == LIB_ID
    assert row.ontology_version_id == ONTOLOGY_ID
    assert row.entity_type_id == PERSON_TYPE_ID
    assert row.canonical_name == "Alice   Zhang"
    assert row.normalized_name == "alice zhang"
    assert row.status == "active"
    assert db.added == [row]
    assert db.flush_count == 1


def test_create_relation_service_creates_only_draft_or_pending_review_relation():
    from app.schemas.v03_graph import GraphRelationCreate
    from app.services import graph_relations

    db = FakeDB(
        *_base_schema_rows(),
        _entity(PERSON_ID, PERSON_TYPE_ID, "Alice"),
        _entity(DEPARTMENT_ID, DEPARTMENT_TYPE_ID, "Engineering"),
    )

    row = asyncio.run(
        graph_relations.create_relation(
            db,
            _lib(),
            GraphRelationCreate(
                relation_type_id=BELONGS_TO_TYPE_ID,
                source_entity_id=PERSON_ID,
                target_entity_id=DEPARTMENT_ID,
                status="pending_review",
                source_type="manual",
            ),
        )
    )

    assert row.library_id == LIB_ID
    assert row.relation_type_id == BELONGS_TO_TYPE_ID
    assert row.source_entity_id == PERSON_ID
    assert row.target_entity_id == DEPARTMENT_ID
    assert row.status == "pending_review"
    assert row.review_status == "pending_review"
    assert db.added == [row]
    assert db.flush_count == 1


def test_create_relation_service_rejects_invalid_source_target_constraint_with_clear_error():
    from app.schemas.v03_graph import GraphRelationCreate
    from app.services import graph_relations

    db = FakeDB(
        *_base_schema_rows(),
        _entity(PERSON_ID, PERSON_TYPE_ID, "Alice"),
        _entity(POLICY_ID, POLICY_TYPE_ID, "Travel Policy"),
    )

    with pytest.raises(ValueError, match="active relation_type_constraint not found"):
        asyncio.run(
            graph_relations.create_relation(
                db,
                _lib(),
                GraphRelationCreate(
                    relation_type_id=APPLIES_TO_TYPE_ID,
                    source_entity_id=PERSON_ID,
                    target_entity_id=POLICY_ID,
                    status="pending_review",
                ),
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_create_relation_service_rejects_active_request_without_active_evidence():
    from app.schemas.v03_graph import GraphRelationCreate
    from app.services import graph_relations

    db = FakeDB(
        *_base_schema_rows(),
        _entity(PERSON_ID, PERSON_TYPE_ID, "Alice"),
        _entity(DEPARTMENT_ID, DEPARTMENT_TYPE_ID, "Engineering"),
    )

    with pytest.raises(ValueError, match="active relation requires active evidence"):
        asyncio.run(
            graph_relations.create_relation(
                db,
                _lib(),
                GraphRelationCreate(
                    relation_type_id=BELONGS_TO_TYPE_ID,
                    source_entity_id=PERSON_ID,
                    target_entity_id=DEPARTMENT_ID,
                    status="active",
                ),
            )
        )

    assert db.added == []
    assert db.flush_count == 0


def test_bind_relation_evidence_service_copies_v02_evidence_snapshot_fields_without_activating_relation():
    from app.schemas.v03_graph import RelationEvidenceCreate
    from app.services import graph_relations

    relation = _relation(status="pending_review")
    db = FakeDB(relation, _evidence())

    row = asyncio.run(
        graph_relations.bind_relation_evidence(
            db,
            _lib(),
            relation.id,
            RelationEvidenceCreate(evidence_id=EVIDENCE_ID, support_type="supports", confidence=0.92),
        )
    )

    assert row.library_id == LIB_ID
    assert row.relation_id == RELATION_ID
    assert row.evidence_id == EVIDENCE_ID
    assert row.document_id == DOC_ID
    assert row.document_revision_id == REVISION_ID
    assert row.evidence_text_snapshot == "Alice belongs to Engineering."
    assert relation.status == "pending_review"
    assert db.added == [row]
    assert db.flush_count == 1
