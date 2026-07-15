from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.attribute_definition import AttributeDefinition
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity_type import EntityType
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_candidate_validation import load_ontology_rule_set_v1
from app.services.graph_extraction_jobs import (
    GraphExtractionJobError,
    GraphExtractionUnitPlan,
    build_full_rerun_idempotency_key,
    build_ontology_rule_snapshot,
    cancel_graph_extraction_job,
    create_graph_extraction_job,
    plan_graph_extraction_units,
    retry_graph_extraction_job,
)


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")


class _Result:
    def __init__(self, rows=(), *, rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

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
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()


def _library(**changes):
    values = {
        "id": LIB_ID,
        "slug": "m5_jobs",
        "name": "M5 Jobs",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "qdrant_collection": "m5_jobs",
        "graph_extraction_enabled": True,
        "external_llm_enabled": True,
        "graph_extraction_allowed_security_levels": ["internal"],
        "deleted_at": None,
    }
    values.update(changes)
    return Library(**values)


def _document(**changes):
    values = {
        "id": DOC_ID,
        "library_id": LIB_ID,
        "content_hash": "document-hash",
        "current_revision_id": REV_ID,
        "security_level": "internal",
        "status": "ready",
        "deleted_at": None,
    }
    values.update(changes)
    return Document(**values)


def _revision(**changes):
    values = {
        "id": REV_ID,
        "library_id": LIB_ID,
        "document_id": DOC_ID,
        "revision_no": 1,
        "content_hash": "revision-hash",
        "parser_name": "plain",
        "parser_version": "parser-v1",
        "chunking_strategy": "fixed",
        "chunking_strategy_version": "chunk-v1",
        "security_level": "internal",
        "status": "ready",
    }
    values.update(changes)
    return DocumentRevision(**values)


def _ontology_rows():
    person_id = uuid.UUID("50000000-0000-0000-0000-000000000001")
    team_id = uuid.UUID("50000000-0000-0000-0000-000000000002")
    relation_id = uuid.UUID("60000000-0000-0000-0000-000000000001")
    ontology = OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIB_ID,
        version_key="default",
        version_no=1,
        status="active",
    )
    entity_types = [
        EntityType(
            id=person_id,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="person",
            label="Person",
            properties_schema={"properties": {"name": {"type": "string"}}},
            status="active",
        ),
        EntityType(
            id=team_id,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="team",
            label="Team",
            status="active",
        ),
    ]
    relation_types = [
        RelationType(
            id=relation_id,
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            key="member_of",
            label="Member of",
            direction="directed",
            requires_evidence=True,
            default_review_policy="auto_active",
            status="active",
        )
    ]
    constraints = [
        RelationTypeConstraint(
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            relation_type_id=relation_id,
            source_entity_type_id=person_id,
            target_entity_type_id=team_id,
            cardinality="many_to_one",
            requires_review=False,
            status="active",
        )
    ]
    attributes = [
        AttributeDefinition(
            library_id=LIB_ID,
            ontology_version_id=ONTOLOGY_ID,
            owner_kind="entity_type",
            owner_type_id=person_id,
            key="level",
            label="Level",
            value_type="integer",
            required=False,
            status="active",
        )
    ]
    return ontology, entity_types, relation_types, constraints, attributes


def test_ontology_snapshot_matches_the_strict_m4_contract():
    ontology, entities, relations, constraints, attributes = _ontology_rows()
    db = FakeDB(
        objects={(OntologyVersion, ONTOLOGY_ID): ontology},
        query_rows=[entities, relations, constraints, attributes],
    )

    snapshot, snapshot_hash = asyncio.run(
        build_ontology_rule_snapshot(
            db,
            library=_library(),
            ontology_version_id=ONTOLOGY_ID,
        )
    )

    assert list(snapshot) == [
        "ontology_version_id",
        "entity_types",
        "relation_types",
        "relation_constraints",
    ]
    assert [row["key"] for row in snapshot["entity_types"]] == ["person", "team"]
    assert snapshot["entity_types"][0]["active_attribute_definitions"][0] == {
        "key": "level",
        "value_type": "integer",
        "required": False,
        "enum_values": None,
        "validation_schema": None,
    }
    rules = load_ontology_rule_set_v1(
        SimpleNamespace(
            ontology_version_id=ONTOLOGY_ID,
            ontology_snapshot=snapshot,
            ontology_snapshot_hash=snapshot_hash,
            normalization_rule_version="normalization_v1",
            confidence_policy_version="v1",
        )
    )
    assert set(rules.entity_types_by_key) == {"person", "team"}


def test_ontology_snapshot_rejects_inactive_scope_and_malformed_schema():
    ontology, entities, relations, constraints, attributes = _ontology_rows()
    ontology.status = "draft"
    db = FakeDB(objects={(OntologyVersion, ONTOLOGY_ID): ontology})
    with pytest.raises(GraphExtractionJobError) as exc_info:
        asyncio.run(
            build_ontology_rule_snapshot(
                db,
                library=_library(),
                ontology_version_id=ONTOLOGY_ID,
            )
        )
    assert exc_info.value.code == "active_ontology_required"

    ontology.status = "active"
    entities[0].properties_schema = {"required": "name"}
    db = FakeDB(
        objects={(OntologyVersion, ONTOLOGY_ID): ontology},
        query_rows=[entities, relations, constraints, attributes],
    )
    with pytest.raises(GraphExtractionJobError) as exc_info:
        asyncio.run(
            build_ontology_rule_snapshot(
                db,
                library=_library(),
                ontology_version_id=ONTOLOGY_ID,
            )
        )
    assert exc_info.value.code == "invalid_ontology_snapshot"


def test_unit_planning_is_stable_and_uses_direct_then_linked_active_evidence():
    first_id = uuid.UUID("70000000-0000-0000-0000-000000000001")
    second_id = uuid.UUID("70000000-0000-0000-0000-000000000002")
    direct_id = uuid.UUID("80000000-0000-0000-0000-000000000001")
    linked_id = uuid.UUID("80000000-0000-0000-0000-000000000002")
    inactive_id = uuid.UUID("80000000-0000-0000-0000-000000000003")
    chunks = [
        SimpleNamespace(
            id=second_id,
            seq=2,
            text="second",
            evidence_id=None,
        ),
        SimpleNamespace(
            id=first_id,
            seq=1,
            text="first",
            evidence_id=direct_id,
        ),
    ]
    links = [
        SimpleNamespace(
            chunk_id=second_id,
            evidence_id=inactive_id,
            document_revision_id=REV_ID,
            seq=0,
        ),
        SimpleNamespace(
            chunk_id=second_id,
            evidence_id=linked_id,
            document_revision_id=REV_ID,
            seq=1,
        ),
    ]
    evidence = [
        SimpleNamespace(
            id=direct_id,
            library_id=LIB_ID,
            document_id=DOC_ID,
            document_revision_id=REV_ID,
            status="active",
        ),
        SimpleNamespace(
            id=linked_id,
            library_id=LIB_ID,
            document_id=DOC_ID,
            document_revision_id=REV_ID,
            status="active",
        ),
    ]
    db = FakeDB(query_rows=[chunks, links, evidence])

    plans = asyncio.run(
        plan_graph_extraction_units(
            db,
            library=_library(),
            document=_document(),
            revision=_revision(),
        )
    )

    assert [(plan.ordinal, plan.center_chunk_id) for plan in plans] == [
        (0, first_id),
        (1, second_id),
    ]
    assert [plan.center_evidence_id for plan in plans] == [direct_id, linked_id]
    assert len({plan.unit_fingerprint for plan in plans}) == 2


def test_unit_planning_rejects_revision_without_active_evidence():
    chunk = SimpleNamespace(id=uuid.uuid4(), seq=0, text="none", evidence_id=None)
    db = FakeDB(query_rows=[[chunk], [], []])
    with pytest.raises(GraphExtractionJobError) as exc_info:
        asyncio.run(
            plan_graph_extraction_units(
                db,
                library=_library(),
                document=_document(),
                revision=_revision(),
            )
        )
    assert exc_info.value.code == "no_eligible_units"


@pytest.mark.parametrize(
    ("library_changes", "document_changes", "revision_changes", "code"),
    [
        ({"graph_extraction_enabled": False}, {}, {}, "library_graph_disabled"),
        ({"external_llm_enabled": False}, {}, {}, "library_external_llm_disabled"),
        (
            {"graph_extraction_allowed_security_levels": []},
            {},
            {},
            "security_allowlist_empty",
        ),
        ({}, {"deleted_at": object()}, {}, "document_deleted"),
        ({}, {"current_revision_id": uuid.uuid4()}, {}, "historical_revision"),
        ({}, {}, {"status": "processing"}, "revision_not_ready"),
        ({}, {}, {"security_level": "restricted"}, "security_level_denied"),
    ],
)
def test_job_creation_fails_closed_before_database_work(
    monkeypatch,
    library_changes,
    document_changes,
    revision_changes,
    code,
):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    db = FakeDB()
    with pytest.raises(GraphExtractionJobError) as exc_info:
        asyncio.run(
            create_graph_extraction_job(
                db,
                library=_library(**library_changes),
                document=_document(**document_changes),
                revision=_revision(**revision_changes),
                trigger_type="manual",
                execution_mode="production",
                requested_by=None,
                idempotency_key=None,
            )
        )
    assert exc_info.value.code == code
    assert db.added == []


def test_job_creation_freezes_config_and_adds_deterministic_units(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    ontology = SimpleNamespace(id=ONTOLOGY_ID)
    snapshot = {
        "ontology_version_id": str(ONTOLOGY_ID),
        "entity_types": [],
        "relation_types": [],
        "relation_constraints": [],
    }
    plans = (
        GraphExtractionUnitPlan(0, uuid.uuid4(), uuid.uuid4(), "a" * 64),
        GraphExtractionUnitPlan(1, uuid.uuid4(), uuid.uuid4(), "b" * 64),
    )
    db = FakeDB(query_rows=[[], []])

    with (
        patch(
            "app.services.graph_extraction_jobs.select_active_ontology",
            new=AsyncMock(return_value=ontology),
        ),
        patch(
            "app.services.graph_extraction_jobs.build_ontology_rule_snapshot",
            new=AsyncMock(return_value=(snapshot, "c" * 64)),
        ),
        patch(
            "app.services.graph_extraction_jobs.plan_graph_extraction_units",
            new=AsyncMock(return_value=plans),
        ),
        patch(
            "app.services.graph_extraction_provider.OpenAICompatibleGraphExtractor.extract",
            new=AsyncMock(),
        ) as provider_call,
    ):
        job = asyncio.run(
            create_graph_extraction_job(
                db,
                library=_library(),
                document=_document(),
                revision=_revision(),
                trigger_type="manual",
                execution_mode="production",
                requested_by=None,
                idempotency_key=None,
            )
        )

    provider_call.assert_not_awaited()
    assert isinstance(job, GraphExtractionJob)
    assert job.model_provider == "deepseek"
    assert job.model_name == "deepseek-chat"
    assert job.model_config_snapshot["provider"] == "deepseek"
    assert job.model_config_snapshot["base_url"] == "https://api.deepseek.com/v1"
    assert job.model_config_snapshot["model"] == "deepseek-chat"
    assert job.idempotency_key == job.input_fingerprint
    assert job.counts == {
        "total": 2,
        "queued": 2,
        "processing": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }
    units = [value for value in db.added if isinstance(value, GraphExtractionUnit)]
    assert [unit.ordinal for unit in units] == [0, 1]
    assert [unit.unit_fingerprint for unit in units] == ["a" * 64, "b" * 64]


def test_duplicate_fingerprint_returns_existing_job_even_when_cancelled(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    existing = SimpleNamespace(id=uuid.uuid4(), status="cancelled")
    ontology = SimpleNamespace(id=ONTOLOGY_ID)
    snapshot = {
        "ontology_version_id": str(ONTOLOGY_ID),
        "entity_types": [],
        "relation_types": [],
        "relation_constraints": [],
    }
    plans = (GraphExtractionUnitPlan(0, uuid.uuid4(), uuid.uuid4(), "a" * 64),)
    db = FakeDB(query_rows=[[], [existing]])
    with (
        patch(
            "app.services.graph_extraction_jobs.select_active_ontology",
            new=AsyncMock(return_value=ontology),
        ),
        patch(
            "app.services.graph_extraction_jobs.build_ontology_rule_snapshot",
            new=AsyncMock(return_value=(snapshot, "c" * 64)),
        ),
        patch(
            "app.services.graph_extraction_jobs.plan_graph_extraction_units",
            new=AsyncMock(return_value=plans),
        ),
    ):
        result = asyncio.run(
            create_graph_extraction_job(
                db,
                library=_library(),
                document=_document(),
                revision=_revision(),
                trigger_type="manual",
                execution_mode="production",
                requested_by=None,
                idempotency_key="new-client-key",
            )
        )

    assert result is existing
    assert db.added == []


def test_full_rerun_idempotency_is_scoped_to_source_job(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    source_id = uuid.uuid4()
    first = build_full_rerun_idempotency_key(source_id, "client-rerun-key")
    assert first == build_full_rerun_idempotency_key(source_id, "client-rerun-key")
    assert first != build_full_rerun_idempotency_key(uuid.uuid4(), "client-rerun-key")

    existing = SimpleNamespace(id=uuid.uuid4())
    db = FakeDB(query_rows=[[existing]])
    result = asyncio.run(
        create_graph_extraction_job(
            db,
            library=_library(),
            document=_document(),
            revision=_revision(),
            trigger_type="full_rerun",
            execution_mode="production",
            requested_by=None,
            idempotency_key="client-rerun-key",
            rerun_of_job_id=source_id,
        )
    )
    assert result is existing
    assert db.added == []


def test_new_full_rerun_rejects_a_source_job_from_another_scope(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    source_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        library_id=uuid.uuid4(),
        document_id=DOC_ID,
        document_revision_id=REV_ID,
    )
    db = FakeDB(
        objects={(GraphExtractionJob, source_id): source},
        query_rows=[[]],
    )

    with pytest.raises(GraphExtractionJobError) as exc_info:
        asyncio.run(
            create_graph_extraction_job(
                db,
                library=_library(),
                document=_document(),
                revision=_revision(),
                trigger_type="full_rerun",
                execution_mode="production",
                requested_by=None,
                idempotency_key="client-rerun-key",
                rerun_of_job_id=source_id,
            )
        )
    assert exc_info.value.code == "rerun_source_not_found"


def test_ordinary_retry_requeues_only_failed_retryable_units_in_the_same_job():
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        status="failed",
        current_stage="finalizing",
        retry_generation=2,
        error_code="unit_failures",
        error_message=None,
        finished_at=object(),
        counts={},
    )
    unit = SimpleNamespace(
        status="failed",
        retryable=True,
        worker_id=None,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        error_code="provider_timeout",
        error_message=None,
        finished_at=object(),
    )
    db = FakeDB(query_rows=[[job], [unit], [("queued", 1)]])

    result = asyncio.run(
        retry_graph_extraction_job(
            db,
            library=_library(),
            job_id=job.id,
            max_attempts=3,
        )
    )

    assert result is job
    assert job.status == "queued"
    assert job.retry_generation == 3
    assert unit.status == "queued"
    assert unit.retryable is False
    assert job.counts["queued"] == 1
    assert not any(isinstance(row, GraphExtractionJob) for row in db.added)


def test_cancel_orders_attempt_then_unit_then_job_state_and_preserves_terminal_units():
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        status="processing",
        current_stage="extracting",
        error_code=None,
        error_message=None,
        finished_at=None,
        counts={},
    )
    db = FakeDB(
        query_rows=[
            [job],
            _Result(rowcount=1),
            _Result(rowcount=2),
            [("cancelled", 2), ("succeeded", 1)],
        ]
    )

    result = asyncio.run(
        cancel_graph_extraction_job(
            db,
            library=_library(),
            job_id=job.id,
        )
    )

    assert result is job
    assert "update extraction_raw_output_attempts" in str(db.statements[1]).lower()
    assert "update graph_extraction_units" in str(db.statements[2]).lower()
    assert ["queued", "processing"] in db.statements[2].compile().params.values()
    assert job.status == "cancelled"
    assert job.counts == {
        "total": 3,
        "queued": 0,
        "processing": 0,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 2,
    }
