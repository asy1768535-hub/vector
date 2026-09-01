from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from app.models.canonical_entity import CanonicalEntity
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_CREATE_NEW,
    ENTITY_RESOLUTION_PENDING_REVIEW,
    ENTITY_RESOLUTION_REJECTED,
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    ENTITY_RESOLUTION_STATUS_SUPERSEDED,
    EntityResolutionDecision,
)
from app.models.entity_type import EntityType
from app.models.graph_candidates import GraphEntityCandidate
from app.models.library import Library
from app.services.canonical_entity_resolution import (
    EntityResolutionInput,
    ExplicitIdentifier,
    resolve_canonical_entity,
)
from app.services.graph_normalization import normalize_graph_name_v1


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalar_one_or_none(self):
        if len(self.rows) > 1:
            raise AssertionError("fixture query unexpectedly returned multiple rows")
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _Nested:
    def __init__(self, db):
        self.db = db
        self.pending_at_start = list(db.pending)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self.db.pending[:] = self.pending_at_start
        return False


class _Db:
    def __init__(self, *rows):
        self.rows = {
            Library: {},
            Entity: {},
            EntityType: {},
            EntityAlias: {},
            CanonicalEntity: {},
            EntityResolutionDecision: {},
            GraphEntityCandidate: {},
        }
        self.pending = []
        for row in rows:
            self._store(row)

    def _store(self, row):
        self.rows[type(row)][row.id] = row

    async def get(self, model, key):
        return self.rows.get(model, {}).get(key)

    async def execute(self, statement, params=None):
        if not getattr(statement, "column_descriptions", None):
            return _Result(())
        entity = statement.column_descriptions[0]["entity"]
        params = params or statement.compile().params
        rows = list(self.rows.get(entity, {}).values())
        for key, value in params.items():
            if key.startswith("library_id"):
                rows = [row for row in rows if getattr(row, "library_id", None) == value]
            elif key.startswith("id"):
                rows = [row for row in rows if getattr(row, "id", None) == value]
            elif key.startswith("decision_fingerprint"):
                rows = [
                    row
                    for row in rows
                    if getattr(row, "decision_fingerprint", None) == value
                ]
            elif key.startswith("subject_fingerprint"):
                rows = [
                    row
                    for row in rows
                    if getattr(row, "subject_fingerprint", None) == value
                ]
            elif key.startswith("lifecycle_status"):
                rows = [
                    row
                    for row in rows
                    if getattr(row, "lifecycle_status", None) == value
                ]
        return _Result(rows)

    def add(self, row):
        self.pending.append(row)

    def begin_nested(self):
        return _Nested(self)

    async def flush(self):
        pending = list(self.pending)
        self.pending.clear()
        for row in pending:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()
            self._store(row)


def _run(coro):
    return asyncio.run(coro)


def _library(library_id):
    return Library(id=library_id, slug=f"lib-{library_id.hex[:8]}", name="Test")


def _type(library_id, *, key="company", entity_type_id=None):
    return EntityType(
        id=entity_type_id or uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=uuid.uuid4(),
        key=key,
        label=key,
        status="active",
    )


def _canonical(library_id, *, name="贵州振华风光", normalized=None, canonical_id=None):
    return CanonicalEntity(
        id=canonical_id or uuid.uuid4(),
        library_id=library_id,
        canonical_name=name,
        normalized_name=normalized or normalize_graph_name_v1(name),
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _entity(library_id, entity_type, canonical, *, entity_id=None, name=None):
    return Entity(
        id=entity_id or uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=entity_type.ontology_version_id,
        entity_type_id=entity_type.id,
        canonical_entity_id=canonical.id if canonical else None,
        canonical_name=name or canonical.canonical_name,
        normalized_name=canonical.normalized_name if canonical else "project a",
        status="active",
        source_type="extracted",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _input(
    library_id,
    *,
    name="贵州振华风光",
    source="source-1",
    type_id=None,
    type_key=None,
    existing_entity_id=None,
    identifiers=(),
    evidence=None,
    properties=None,
    context=None,
    candidate_id=None,
):
    return EntityResolutionInput(
        library_id=library_id,
        observed_name=name,
        observed_normalized_name=normalize_graph_name_v1(name),
        observed_entity_type_id=type_id,
        observed_entity_type_key=type_key,
        existing_entity_id=existing_entity_id,
        explicit_identifiers=tuple(identifiers),
        observed_properties=properties,
        context=context,
        evidence_refs=(
            ({"document_revision_id": "rev-1", "mention_id": "m-1"},)
            if evidence is None
            else tuple(evidence)
        ),
        graph_entity_candidate_id=candidate_id,
        source_fingerprint=source,
    )


def test_existing_mapping_auto_links_and_replays_idempotently():
    library_id = uuid.uuid4()
    entity_type = _type(library_id)
    canonical = _canonical(library_id)
    entity = _entity(library_id, entity_type, canonical)
    db = _Db(_library(library_id), entity_type, canonical, entity)

    first = _run(resolve_canonical_entity(db, _input(library_id, existing_entity_id=entity.id)))
    second = _run(resolve_canonical_entity(db, _input(library_id, existing_entity_id=entity.id)))

    assert first.decision.decision_kind == "link_existing"
    assert first.decision.method == "existing_mapping"
    assert first.canonical_entity.id == canonical.id
    assert second.decision.id == first.decision.id
    assert second.decision_created is False
    assert len(db.rows[EntityResolutionDecision]) == 1


def test_cross_library_existing_entity_is_rejected_without_new_canonical():
    library_id = uuid.uuid4()
    other_library_id = uuid.uuid4()
    entity_type = _type(other_library_id)
    canonical = _canonical(other_library_id)
    entity = _entity(other_library_id, entity_type, canonical)
    db = _Db(_library(library_id), _library(other_library_id), entity_type, canonical, entity)

    result = _run(
        resolve_canonical_entity(
            db,
            _input(library_id, existing_entity_id=entity.id, name="项目A"),
        )
    )

    assert result.decision.decision_kind == ENTITY_RESOLUTION_REJECTED
    assert result.decision.reason_code == "entity_scope_mismatch"
    assert result.canonical_entity is None
    assert not db.rows[CanonicalEntity].get(library_id)


def test_name_only_exact_match_is_pending_even_with_one_candidate():
    library_id = uuid.uuid4()
    canonical = _canonical(library_id)
    db = _Db(_library(library_id), canonical)

    result = _run(resolve_canonical_entity(db, _input(library_id)))

    assert result.decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW
    assert result.decision.canonical_entity_id is None
    assert result.decision.reason_code == "weak_identity_evidence"
    assert result.decision.candidate_snapshot[0]["included"] is True


def test_alias_exact_match_is_pending_and_does_not_auto_link():
    library_id = uuid.uuid4()
    entity_type = _type(library_id)
    canonical = _canonical(library_id)
    entity = _entity(library_id, entity_type, canonical)
    alias = EntityAlias(
        id=uuid.uuid4(),
        library_id=library_id,
        entity_id=entity.id,
        alias="振华风光",
        normalized_alias=normalize_graph_name_v1("振华风光"),
        status="active",
        source_type="manual",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db = _Db(_library(library_id), entity_type, canonical, entity, alias)

    result = _run(
        resolve_canonical_entity(
            db,
            _input(library_id, name="振华风光"),
        )
    )

    assert result.decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW
    assert "entity_alias" in result.decision.candidate_snapshot[0]["signals"]


def test_two_same_name_candidates_are_pending():
    library_id = uuid.uuid4()
    first = _canonical(library_id)
    second = _canonical(library_id)
    db = _Db(_library(library_id), first, second)

    result = _run(resolve_canonical_entity(db, _input(library_id)))

    assert result.decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW
    assert result.decision.reason_code == "multiple_candidates"
    assert len(result.decision.candidate_snapshot) == 2


def test_no_candidate_creates_one_canonical_and_replays_without_duplicate():
    library_id = uuid.uuid4()
    db = _Db(_library(library_id))
    request = _input(library_id, name="项目A", source="source-project-a")

    first = _run(resolve_canonical_entity(db, request))
    second = _run(resolve_canonical_entity(db, request))

    assert first.decision.decision_kind == ENTITY_RESOLUTION_CREATE_NEW
    assert first.canonical_created is True
    assert second.decision.id == first.decision.id
    assert second.canonical_entity.id == first.canonical_entity.id
    assert second.canonical_created is False
    assert len(db.rows[CanonicalEntity]) == 1
    assert len(db.rows[EntityResolutionDecision]) == 1


def test_incompatible_type_excludes_candidate_but_creates_new_entity():
    library_id = uuid.uuid4()
    company_type = _type(library_id, key="company")
    canonical = _canonical(library_id, name="项目A", normalized="项目a")
    projection = _entity(library_id, company_type, canonical, name="项目A")
    db = _Db(_library(library_id), company_type, canonical, projection)

    result = _run(
        resolve_canonical_entity(
            db,
            _input(library_id, name="项目A", type_key="project", source="project-source"),
        )
    )

    assert result.decision.decision_kind == ENTITY_RESOLUTION_CREATE_NEW
    assert result.decision.reason_code == "no_compatible_candidate"
    assert result.decision.candidate_snapshot[0]["included"] is False
    assert result.decision.candidate_snapshot[0]["excluded_reason"] == "type_incompatible"


def test_invalid_name_or_evidence_is_rejected_without_canonical():
    library_id = uuid.uuid4()
    db = _Db(_library(library_id))

    empty_name = _input(library_id, name=" ")
    invalid_evidence = _input(library_id, evidence=())
    first = _run(resolve_canonical_entity(db, empty_name))
    second = _run(resolve_canonical_entity(db, invalid_evidence))

    assert first.decision.decision_kind == ENTITY_RESOLUTION_REJECTED
    assert second.decision.decision_kind == ENTITY_RESOLUTION_REJECTED
    assert len(db.rows[CanonicalEntity]) == 0


def test_repeated_subject_can_supersede_active_decision_append_only():
    library_id = uuid.uuid4()
    db = _Db(_library(library_id))
    first = _run(
        resolve_canonical_entity(
            db,
            _input(library_id, name="项目A", source="same-subject", context={"stage": 1}),
        )
    )
    candidate = _canonical(library_id, name="项目A", normalized="项目a")
    db._store(candidate)
    second = _run(
        resolve_canonical_entity(
            db,
            _input(library_id, name="项目A", source="same-subject", context={"stage": 2}),
        )
    )

    assert second.decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW
    assert second.decision.supersedes_decision_id == first.decision.id
    assert db.rows[EntityResolutionDecision][first.decision.id].lifecycle_status == (
        ENTITY_RESOLUTION_STATUS_SUPERSEDED
    )
    assert second.decision.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE


def test_historical_decision_reappearing_creates_new_active_generation_once():
    library_id = uuid.uuid4()
    canonical = _canonical(library_id, name="项目A", normalized="项目a")
    db = _Db(_library(library_id), canonical)
    request_a = _input(
        library_id,
        name="项目A",
        source="same-subject",
        context={"stage": "a"},
    )
    request_b = _input(
        library_id,
        name="项目A",
        source="same-subject",
        context={"stage": "b"},
    )

    first = _run(resolve_canonical_entity(db, request_a))
    second = _run(resolve_canonical_entity(db, request_b))
    third = _run(resolve_canonical_entity(db, request_a))
    replay = _run(resolve_canonical_entity(db, request_a))

    assert third.decision.id != first.decision.id
    assert third.decision.supersedes_decision_id == second.decision.id
    assert first.decision.lifecycle_status == ENTITY_RESOLUTION_STATUS_SUPERSEDED
    assert second.decision.lifecycle_status == ENTITY_RESOLUTION_STATUS_SUPERSEDED
    assert third.decision.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    assert replay.decision.id == third.decision.id
    assert len(db.rows[EntityResolutionDecision]) == 3


def test_resolution_subject_lock_key_is_stable_and_subject_scoped():
    from app.services import canonical_entity_resolution as resolution

    library_id = uuid.uuid4()

    assert resolution._resolution_subject_lock_key(
        library_id, "a" * 64
    ) == resolution._resolution_subject_lock_key(
        library_id, "a" * 64
    )
    assert resolution._resolution_subject_lock_key(
        library_id, "a" * 64
    ) != resolution._resolution_subject_lock_key(
        library_id, "b" * 64
    )


def test_explicit_identifier_is_snapshotted_but_not_used_as_unregistered_auto_link():
    library_id = uuid.uuid4()
    canonical = _canonical(library_id, name="项目A", normalized="项目a")
    db = _Db(_library(library_id), canonical)

    result = _run(
        resolve_canonical_entity(
            db,
            _input(
                library_id,
                name="项目A",
                identifiers=(ExplicitIdentifier(namespace="project_code", value="P-001"),),
            ),
        )
    )

    assert result.decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW
    assert result.decision.identifier_snapshot == {
        "identifiers": [
            {
                "identity_semantics": "explicit",
                "issuer": None,
                "namespace": "project_code",
                "value": "P-001",
            }
        ]
    }


def test_input_fingerprint_is_stable_for_mapping_order():
    library_id = uuid.uuid4()
    db = _Db(_library(library_id))
    first_request = _input(
        library_id,
        name="项目A",
        source="stable-source",
        properties={"b": 2, "a": 1},
        context={"z": "last", "a": "first"},
    )
    second_request = _input(
        library_id,
        name="项目A",
        source="stable-source",
        properties={"a": 1, "b": 2},
        context={"a": "first", "z": "last"},
    )

    first = _run(resolve_canonical_entity(db, first_request))
    second = _run(resolve_canonical_entity(db, second_request))

    assert second.decision.id == first.decision.id
    assert second.decision_created is False


@pytest.mark.skipif(
    not __import__("os").environ.get("VECTOR_KB_PG_TEST_DSN"),
    reason="真实 PostgreSQL CREATE NEW 并发测试需要 VECTOR_KB_PG_TEST_DSN",
)
def test_create_new_concurrency_requires_postgresql_integration_fixture():
    pytest.fail("由独立 PostgreSQL 集成测试覆盖")
