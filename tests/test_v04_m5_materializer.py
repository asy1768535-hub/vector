from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.entity import Entity
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.knowledge_relation import KnowledgeRelation
from app.services.graph_extraction_materializer import (
    GraphExtractionMaterializationError,
    GraphExtractionMaterializationResult,
    _entity_candidate_eligible,
    _materialize_job_transaction,
    _relation_candidate_eligible,
    materialize_graph_extraction_job,
)


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
PERSON_TYPE_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
TEAM_TYPE_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
RELATION_TYPE_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")


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
    def __init__(self, *, results=(), objects=None):
        self.results = list(results)
        self.objects = objects or {}
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement):
        assert self.results, "unexpected materializer query"
        return self.results.pop(0)

    async def get(self, model, object_id, **_kwargs):
        return self.objects.get((model, object_id))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _entity_candidate(*, key: str, matched_entity_id=None, confidence=0.95):
    return SimpleNamespace(
        id=uuid.uuid4(),
        entity_type_key=key,
        canonical_name=f"{key} name",
        normalized_name=f"{key} name",
        proposed_properties={},
        matched_entity_id=matched_entity_id,
        materialized_entity_id=None,
        final_confidence=confidence,
        status="validated",
        purged_at=None,
    )


def _relation_candidate(source_id, target_id, **changes):
    values = {
        "id": uuid.uuid4(),
        "source_candidate_id": source_id,
        "target_candidate_id": target_id,
        "relation_type_key": "member_of",
        "proposed_properties": {},
        "materialized_relation_id": None,
        "evidence_support_mode": "single_evidence",
        "ontology_validation_status": "valid",
        "has_conflict": False,
        "final_confidence": 0.95,
        "status": "validated",
        "purged_at": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _candidate_evidence(candidate_id):
    return SimpleNamespace(
        candidate_id=candidate_id,
        purged_at=None,
        validation_status="valid",
        resolved_evidence_id=uuid.uuid4(),
        resolved_document_id=uuid.uuid4(),
        resolved_document_revision_id=uuid.uuid4(),
        resolved_chunk_id=uuid.uuid4(),
        resolved_source_span={"start": 0, "end": 4},
    )


def test_materialization_filters_are_fail_closed():
    entity = _entity_candidate(key="person")
    assert _entity_candidate_eligible(entity, 0.85)
    entity.final_confidence = 0.84
    assert not _entity_candidate_eligible(entity, 0.85)
    entity.final_confidence = 0.95
    entity.status = "pending_review"
    assert not _entity_candidate_eligible(entity, 0.85)

    relation = _relation_candidate(uuid.uuid4(), uuid.uuid4())
    assert _relation_candidate_eligible(relation, 0.85)
    relation.evidence_support_mode = "evidence_group"
    assert not _relation_candidate_eligible(relation, 0.85)
    relation.evidence_support_mode = "single_evidence"
    relation.has_conflict = True
    assert not _relation_candidate_eligible(relation, 0.85)


def test_materializer_creates_only_draft_facts_and_active_support_rows():
    matched_id = uuid.uuid4()
    person = _entity_candidate(key="person")
    team = _entity_candidate(key="team", matched_entity_id=matched_id)
    relation = _relation_candidate(person.id, team.id)
    evidence_group = _relation_candidate(
        person.id,
        team.id,
        evidence_support_mode="evidence_group",
    )
    person_evidence = _candidate_evidence(person.id)
    team_evidence = _candidate_evidence(team.id)
    relation_evidence = _candidate_evidence(relation.id)

    job = SimpleNamespace(
        id=JOB_ID,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        status="processing",
        current_stage="materializing",
        statistics={},
        error_code=None,
        error_message=None,
        finished_at=None,
    )
    library = SimpleNamespace(id=LIB_ID)
    matched = Entity(
        id=matched_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=TEAM_TYPE_ID,
        canonical_name="Existing team",
        normalized_name="existing team",
        status="active",
        source_type="manual",
    )
    new_entity = Entity(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=PERSON_TYPE_ID,
        canonical_name="person name",
        normalized_name="person name",
        status="draft",
        source_type="extracted",
    )
    mention_rows = [
        SimpleNamespace(status="active", created_by_job_id=None, extraction_key=None),
        SimpleNamespace(status="active", created_by_job_id=None, extraction_key=None),
    ]
    draft_relation = KnowledgeRelation(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=RELATION_TYPE_ID,
        source_entity_id=new_entity.id,
        target_entity_id=matched.id,
        status="draft",
        source_type="extracted",
    )
    formal_evidence = SimpleNamespace(status="active", created_by_job_id=None)
    db = FakeDB(
        results=[
            _Result([person, team]),
            _Result([relation, evidence_group]),
            _Result([person_evidence, team_evidence]),
            _Result([relation_evidence]),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
        ],
        objects={(Entity, matched_id): matched},
    )
    rules = SimpleNamespace(
        entity_types_by_key={
            "person": SimpleNamespace(id=PERSON_TYPE_ID),
            "team": SimpleNamespace(id=TEAM_TYPE_ID),
        },
        relation_types_by_key={
            "member_of": SimpleNamespace(id=RELATION_TYPE_ID),
        },
    )
    policy = SimpleNamespace(
        entity_materialization_threshold=0.85,
        relation_draft_threshold=0.85,
    )

    with (
        patch(
            "app.services.graph_extraction_materializer._load_materialization_scope",
            new=AsyncMock(return_value=(job, library, None, None, None)),
        ),
        patch(
            "app.services.graph_extraction_materializer.load_ontology_rule_set_v1",
            return_value=rules,
        ),
        patch(
            "app.services.graph_extraction_materializer.load_confidence_policy_v1",
            return_value=policy,
        ),
        patch(
            "app.services.graph_extraction_materializer.graph_entities.create_entity",
            new=AsyncMock(return_value=new_entity),
        ) as create_entity,
        patch(
            "app.services.graph_extraction_materializer.graph_evidence.create_entity_mention",
            new=AsyncMock(side_effect=mention_rows),
        ) as create_mention,
        patch(
            "app.services.graph_extraction_materializer.graph_relations.create_relation",
            new=AsyncMock(return_value=draft_relation),
        ) as create_relation,
        patch(
            "app.services.graph_extraction_materializer.graph_evidence.create_relation_evidence",
            new=AsyncMock(return_value=formal_evidence),
        ) as create_relation_evidence,
    ):
        result = asyncio.run(_materialize_job_transaction(db, job_id=JOB_ID))

    assert result == GraphExtractionMaterializationResult(1, 2, 1, 1)
    create_entity.assert_awaited_once()
    assert create_entity.await_args.args[2].status == "draft"
    create_relation.assert_awaited_once()
    assert create_relation.await_args.args[2].status == "draft"
    assert all(row.status == "active" for row in mention_rows)
    assert formal_evidence.status == "active"
    assert create_mention.await_count == 2
    create_relation_evidence.assert_awaited_once()
    assert person.status == "materialized"
    assert team.status == "materialized"
    assert relation.status == "materialized"
    assert evidence_group.status == "validated"
    assert job.status == "succeeded"


class _Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True
        return False


class TransactionSession:
    def __init__(self, *, job=None):
        self.job = job
        self.committed = False
        self.rolled_back = False
        self.flush_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _Transaction(self)

    async def get(self, model, object_id, **_kwargs):
        if model is GraphExtractionJob and self.job is not None and self.job.id == object_id:
            return self.job
        return None

    async def flush(self):
        self.flush_count += 1


class TransactionFactory:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.used = []

    def __call__(self):
        session = self.sessions.pop(0)
        self.used.append(session)
        return session


def test_materialization_failure_rolls_back_all_formal_writes_then_marks_job_failed():
    job = SimpleNamespace(
        id=JOB_ID,
        status="processing",
        current_stage="materializing",
        error_code=None,
        error_message="sensitive detail",
        finished_at=None,
    )
    materialization_session = TransactionSession()
    failure_session = TransactionSession(job=job)
    factory = TransactionFactory([materialization_session, failure_session])

    with patch(
        "app.services.graph_extraction_materializer._materialize_job_transaction",
        new=AsyncMock(side_effect=RuntimeError("formal write failed")),
    ):
        with pytest.raises(GraphExtractionMaterializationError) as exc_info:
            asyncio.run(
                materialize_graph_extraction_job(
                    factory,
                    job_id=JOB_ID,
                )
            )

    assert exc_info.value.code == "materialization_failed"
    assert materialization_session.rolled_back is True
    assert materialization_session.committed is False
    assert failure_session.committed is True
    assert job.status == "failed"
    assert job.error_code == "materialization_failed"
    assert job.error_message is None
