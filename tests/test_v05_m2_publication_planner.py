from __future__ import annotations

import asyncio
import uuid

import pytest

from app.config import Settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    GraphPublicationPlanResult,
    plan_graph_publication,
    plan_initial_publication,
)


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
PERSON_TYPE_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
TEAM_TYPE_ID = uuid.UUID("30000000-0000-0000-0000-000000000002")
RELATION_TYPE_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
PERSON_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
TEAM_ID = uuid.UUID("50000000-0000-0000-0000-000000000002")
RELATION_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("70000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("80000000-0000-0000-0000-000000000001")
EVIDENCE_ID = uuid.UUID("90000000-0000-0000-0000-000000000001")
MENTION_ID = uuid.UUID("90000000-0000-0000-0000-000000000002")


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, *, objects=None, query_rows=None):
        self.objects = objects or {}
        self.query_rows = list(query_rows or [])
        self.added = []
        self.statements = []
        self.flushed = 0

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.query_rows, "unexpected database query"
        value = self.query_rows.pop(0)
        return value if isinstance(value, _Result) else _Result(value)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1


def _config(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _library() -> Library:
    return Library(
        id=LIB_ID,
        slug="v05",
        name="v05",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="v05",
    )


def _ontology() -> OntologyVersion:
    return OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIB_ID,
        version_key="default",
        version_no=1,
        status="active",
    )


def _entity_types() -> list[EntityType]:
    return [
        EntityType(
            id=PERSON_TYPE_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="person",
            label="Person",
            properties_schema={"properties": {"level": {"type": "integer"}}},
            status="active",
        ),
        EntityType(
            id=TEAM_TYPE_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="team",
            label="Team",
            properties_schema={},
            status="active",
        ),
    ]


def _relation_types() -> list[RelationType]:
    return [
        RelationType(
            id=RELATION_TYPE_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="member_of",
            label="Member of",
            direction="directed",
            requires_evidence=True,
            default_review_policy="auto_active",
            properties_schema={"properties": {"since": {"type": "string"}}},
            status="active",
        )
    ]


def _relation_constraints(*, requires_review=False, status="active") -> list[RelationTypeConstraint]:
    return [
        RelationTypeConstraint(
            id=uuid.UUID("41000000-0000-0000-0000-000000000001"),
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            relation_type_id=RELATION_TYPE_ID,
            source_entity_type_id=PERSON_TYPE_ID,
            target_entity_type_id=TEAM_TYPE_ID,
            cardinality="many_to_one",
            requires_review=requires_review,
            status=status,
        )
    ]


def _entities(*, person_status="active", team_status="active", confidence=0.93) -> list[Entity]:
    return [
        Entity(
            id=PERSON_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            entity_type_id=PERSON_TYPE_ID,
            canonical_name="Alice",
            normalized_name="alice",
            properties={"level": 3},
            status=person_status,
            source_type="extracted",
            confidence=confidence,
        ),
        Entity(
            id=TEAM_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            entity_type_id=TEAM_TYPE_ID,
            canonical_name="Platform",
            normalized_name="platform",
            properties={},
            status=team_status,
            source_type="manual",
            confidence=None,
        ),
    ]


def _relations(*, status="active", review_status="approved", confidence=0.91) -> list[KnowledgeRelation]:
    return [
        KnowledgeRelation(
            id=RELATION_ID,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            relation_type_id=RELATION_TYPE_ID,
            source_entity_id=PERSON_ID,
            target_entity_id=TEAM_ID,
            properties={"since": "2026"},
            status=status,
            review_status=review_status,
            source_type="extracted",
            confidence=confidence,
        )
    ]


def _mention() -> EntityMention:
    return EntityMention(
        id=uuid.UUID("91000000-0000-0000-0000-000000000001"),
        library_id=LIB_ID,
        entity_id=PERSON_ID,
        evidence_id=MENTION_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        mention_text="Alice",
        status="active",
        source_type="extracted",
    )


def _relation_evidence(*, support_type="supports", evidence_id=EVIDENCE_ID) -> RelationEvidence:
    return RelationEvidence(
        id=uuid.UUID("92000000-0000-0000-0000-000000000001"),
        library_id=LIB_ID,
        relation_id=RELATION_ID,
        evidence_id=evidence_id,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        support_type=support_type,
        status="active",
    )


def _scope_objects() -> dict[tuple[type, uuid.UUID], object]:
    document = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        content_hash="hash",
        current_revision_id=REV_ID,
        status="ready",
        deleted_at=None,
    )
    revision = DocumentRevision(
        id=REV_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        revision_no=1,
        content_hash="hash",
        parser_name="plain",
        parser_version="v1",
        chunking_strategy="fixed",
        chunking_strategy_version="v1",
        status="ready",
    )
    return {
        (OntologyVersion, ONTOLOGY_ID): _ontology(),
        (Document, DOC_ID): document,
        (DocumentRevision, REV_ID): revision,
        (
            EvidenceUnit,
            EVIDENCE_ID,
        ): EvidenceUnit(
            id=EVIDENCE_ID,
            library_id=LIB_ID,
            document_id=DOC_ID,
            document_revision_id=REV_ID,
            evidence_kind="text",
            status="active",
        ),
        (
            EvidenceUnit,
            MENTION_ID,
        ): EvidenceUnit(
            id=MENTION_ID,
            library_id=LIB_ID,
            document_id=DOC_ID,
            document_revision_id=REV_ID,
            evidence_kind="text",
            status="active",
        ),
    }


def _query_rows(
    *,
    parent=(),
    entities=None,
    relations=None,
    constraints=None,
    mentions=None,
    evidence=None,
    lock=(),
    idempotent=(),
    reusable=(),
    include_lock_query=True,
    include_idempotency_query=True,
    include_reuse_query=False,
):
    rows = []
    if include_lock_query:
        rows.append(lock)
    if include_idempotency_query:
        rows.append(idempotent)
    rows.extend(
        [
            parent,
            _entity_types(),
            _relation_types(),
            _relation_constraints() if constraints is None else constraints,
            _entities() if entities is None else entities,
            _relations() if relations is None else relations,
            [_mention()] if mentions is None else mentions,
            [_relation_evidence()] if evidence is None else evidence,
        ]
    )
    if include_reuse_query:
        rows.append(reusable)
    return rows


def _plan(db: FakeDB, **kwargs) -> GraphPublicationPlanResult:
    return asyncio.run(
        plan_graph_publication(
            db,
            _library(),
            ontology_version_id=ONTOLOGY_ID,
            idempotency_key=kwargs.pop("idempotency_key", "key-1"),
            config=kwargs.pop("config", _config()),
            **kwargs,
        )
    )


def test_dry_run_planner_is_deterministic_and_persists_nothing():
    db1 = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            include_reuse_query=False,
        ),
    )
    result1 = _plan(db1, dry_run=True)

    db2 = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            entities=list(reversed(_entities())),
            include_reuse_query=False,
        ),
    )
    result2 = _plan(db2, dry_run=True)

    assert result1.dry_run is True
    assert result1.manifest_hash == result2.manifest_hash
    assert result1.publication.entity_count == 2
    assert result1.publication.relation_count == 1
    assert len(result1.items) == 3
    assert db1.added == []


def test_planner_enforces_configured_item_limit_by_default():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            include_reuse_query=False,
        ),
    )

    with pytest.raises(GraphPublicationPlanError) as exc_info:
        _plan(
            db,
            dry_run=True,
            config=_config(graph_publication_max_items_per_run=2),
        )

    assert exc_info.value.code == "publication_item_limit_exceeded"


def test_persisted_plan_adds_publication_and_membership_items():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(include_reuse_query=True),
    )

    result = _plan(db)

    assert result.reused is False
    assert result.publication in db.added
    assert len([item for item in db.added if isinstance(item, GraphPublicationItem)]) == 3
    assert result.publication.source_mode == "manual_plan"
    assert result.publication.include_drafts is False
    assert result.publication.plan_options == {"include_drafts": False, "dry_run": False}
    assert result.publication.blocked_counts == {}
    assert all(item.publication_id == result.publication.id for item in result.items)
    assert all("quote_text" not in item.fact_snapshot for item in result.items)


def test_initial_seed_can_include_draft_facts_and_records_blocked_counts():
    entities = _entities(person_status="draft", team_status="pending_review")
    relations = _relations(status="draft", review_status=None)
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=[
            (),
            _entity_types(),
            _relation_types(),
            _relation_constraints(),
            entities,
            relations,
            [_mention()],
            [_relation_evidence()],
        ],
    )

    result = asyncio.run(
        plan_initial_publication(
            db,
            _library(),
            ontology_version_id=ONTOLOGY_ID,
            include_drafts=True,
            dry_run=True,
            idempotency_key="seed-1",
            config=_config(),
        )
    )

    assert result.publication.source_mode == "initial_seed"
    assert result.publication.include_drafts is True
    assert result.publication.entity_count == 1
    assert result.publication.relation_count == 0
    assert result.blocked_counts["entity_status_pending_review"] == 1
    assert result.blocked_counts["relation_endpoint_not_published"] == 1


def test_auto_active_relation_does_not_require_review_status():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            relations=_relations(status="draft", review_status=None),
        ),
    )

    result = _plan(db, dry_run=True, include_drafts=True)

    assert result.publication.entity_count == 2
    assert result.publication.relation_count == 1
    assert "relation_review_blocked" not in result.blocked_counts


def test_idempotency_replay_returns_existing_planned_publication():
    existing = GraphPublication(
        id=uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002"),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        status="planned",
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={"policy_version": "v1"},
        manifest_hash="b" * 64,
        idempotency_key="same-key",
        include_drafts=False,
        plan_options={},
        entity_count=2,
        relation_count=1,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
    )
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=[(), [existing]],
    )

    result = _plan(db, idempotency_key="same-key")

    assert result.reused is True
    assert result.publication is existing
    assert result.items == ()
    assert db.added == []


def test_manifest_reuse_returns_existing_publication_without_duplicate_rows():
    existing = GraphPublication(
        id=uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        status="planned",
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="placeholder",
        idempotency_key="old-key",
        include_drafts=False,
        plan_options={},
        entity_count=2,
        relation_count=1,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
    )
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(include_reuse_query=True, reusable=[existing]),
    )

    result = _plan(db, idempotency_key="new-key")

    assert result.reused is True
    assert result.publication is existing
    assert db.added == []


def test_policy_change_changes_manifest_hash():
    db1 = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            include_reuse_query=False,
        ),
    )
    result1 = _plan(db1, dry_run=True, config=_config())
    db2 = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            include_reuse_query=False,
        ),
    )
    result2 = _plan(
        db2,
        dry_run=True,
        config=_config(graph_publication_extracted_relation_min_confidence=0.9),
    )

    assert result1.policy_snapshot_hash != result2.policy_snapshot_hash
    assert result1.manifest_hash != result2.manifest_hash


def test_relation_with_contradicting_evidence_is_blocked():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            evidence=[
                _relation_evidence(),
                _relation_evidence(
                    support_type="contradicts",
                    evidence_id=uuid.UUID("90000000-0000-0000-0000-000000000003"),
                ),
            ],
        ),
    )
    db.objects[
        (
            EvidenceUnit,
            uuid.UUID("90000000-0000-0000-0000-000000000003"),
        )
    ] = EvidenceUnit(
        id=uuid.UUID("90000000-0000-0000-0000-000000000003"),
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        evidence_kind="text",
        status="active",
    )

    result = _plan(db, dry_run=True)

    assert result.publication.entity_count == 2
    assert result.publication.relation_count == 0
    assert result.blocked_counts["relation_contradicted"] == 1


def test_current_revision_unsafe_relation_evidence_blocks_relation():
    objects = _scope_objects()
    objects[(EvidenceUnit, EVIDENCE_ID)].status = "stale"
    db = FakeDB(
        objects=objects,
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
        ),
    )

    result = _plan(
        db,
        dry_run=True,
        config=_config(graph_publication_require_entity_evidence=False),
    )

    assert result.publication.entity_count == 2
    assert result.publication.relation_count == 0
    assert result.blocked_counts["relation_evidence_incomplete"] == 1


def test_missing_relation_constraint_blocks_relation_from_snapshot():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            constraints=[],
        ),
    )

    result = _plan(db, dry_run=True)

    assert result.publication.entity_count == 2
    assert result.publication.relation_count == 0
    assert result.blocked_counts["relation_schema_invalid"] == 1


def test_review_required_relation_constraint_requires_approved_review_status():
    db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            constraints=_relation_constraints(requires_review=True),
            relations=_relations(review_status="not_required"),
        ),
    )

    result = _plan(db, dry_run=True)

    assert result.publication.entity_count == 2
    assert result.publication.relation_count == 0
    assert result.blocked_counts["relation_review_blocked"] == 1


def test_missing_extracted_confidence_blocks_entity_and_relation():
    entity_db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            entities=_entities(confidence=None),
        ),
    )

    entity_result = _plan(entity_db, dry_run=True)

    assert entity_result.publication.entity_count == 1
    assert entity_result.publication.relation_count == 0
    assert entity_result.blocked_counts["entity_low_confidence"] == 1
    assert entity_result.blocked_counts["relation_endpoint_not_published"] == 1

    relation_db = FakeDB(
        objects=_scope_objects(),
        query_rows=_query_rows(
            include_lock_query=False,
            include_idempotency_query=False,
            relations=_relations(confidence=None),
        ),
    )

    relation_result = _plan(relation_db, dry_run=True)

    assert relation_result.publication.entity_count == 2
    assert relation_result.publication.relation_count == 0
    assert relation_result.blocked_counts["relation_low_confidence"] == 1


def test_expected_parent_fence_rejects_changed_current_before_snapshot_build():
    current = GraphPublication(
        id=uuid.UUID("aaaaaaaa-0000-0000-0000-000000000010"),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        status="active",
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="d" * 64,
        idempotency_key="current-key",
        include_drafts=False,
        plan_options={},
        entity_count=0,
        relation_count=0,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
    )
    db = FakeDB(objects=_scope_objects(), query_rows=[[current]])

    with pytest.raises(GraphPublicationPlanError) as exc_info:
        _plan(
            db,
            dry_run=True,
            expected_parent_publication_id=uuid.UUID(
                "aaaaaaaa-0000-0000-0000-000000000011"
            ),
        )

    assert exc_info.value.code == "expected_parent_mismatch"
    assert db.added == []
