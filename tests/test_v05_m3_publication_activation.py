from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.sql.dml import Update

from app.config import Settings
from app.models.audit import AuditLog
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.services import graph_publication_activation as activation
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
    plan_graph_publication_rollback,
)
from app.services.graph_publication_planner import (
    GraphPublicationSnapshot,
    _manifest_hash,
    _sha256_json,
    build_publication_policy_snapshot,
)


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ENTITY_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
PREVIOUS_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
PUBLICATION_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
TARGET_ID = uuid.UUID("40000000-0000-0000-0000-000000000003")


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
        self.select_statements = []
        self.update_statements = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def in_transaction(self):
        return False

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, statement):
        if isinstance(statement, Update):
            self.update_statements.append(statement)
            return _Result()
        self.select_statements.append(statement)
        assert self.query_rows, "unexpected database query"
        rows = self.query_rows.pop(0)
        return rows if isinstance(rows, _Result) else _Result(rows)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _config(**overrides) -> Settings:
    values = {"graph_publication_enabled": True, **overrides}
    return Settings(_env_file=None, **values)


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="v05-m3",
        name="v0.5 M3",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="v05_m3",
    )


def _ontology() -> OntologyVersion:
    return OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIBRARY_ID,
        version_key="default",
        version_no=1,
        status="active",
    )


def _entity_item(publication_id: uuid.UUID, *, item_hash="a" * 64, status="planned"):
    return GraphPublicationItem(
        id=uuid.uuid4(),
        publication_id=publication_id,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        item_kind="entity",
        entity_id=ENTITY_ID,
        item_hash=item_hash,
        status=status,
        support_evidence_ids=[],
        support_counts={"active_mentions": 0},
        fact_snapshot={"entity_id": str(ENTITY_ID), "item_kind": "entity"},
    )


def _publication(
    config: Settings,
    *,
    publication_id=PUBLICATION_ID,
    status="planned",
    source_mode="manual_plan",
    parent_publication_id=PREVIOUS_ID,
    item=None,
) -> tuple[GraphPublication, GraphPublicationItem]:
    item = item or _entity_item(publication_id)
    policy = build_publication_policy_snapshot(config)
    publication = GraphPublication(
        id=publication_id,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        status=status,
        source_mode=source_mode,
        manifest_version=config.graph_publication_manifest_version,
        policy_version=config.graph_publication_policy_version,
        policy_snapshot=policy,
        manifest_hash="",
        idempotency_key=f"key:{publication_id}",
        include_drafts=False,
        plan_options={},
        parent_publication_id=parent_publication_id,
        entity_count=1,
        relation_count=0,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={"entity_hashes": [item.item_hash], "relation_hashes": []},
    )
    publication.manifest_hash = _manifest_hash(
        config=config,
        policy_snapshot_hash=_sha256_json(policy),
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        source_mode=source_mode,
        parent_publication_id=parent_publication_id,
        include_drafts=False,
        items=[item],
        blocked_counts={},
    )
    return publication, item


def _current(*, status="active", publication_id=PREVIOUS_ID) -> GraphPublication:
    return GraphPublication(
        id=publication_id,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        status=status,
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="b" * 64,
        idempotency_key=f"current:{publication_id}",
        include_drafts=False,
        plan_options={},
        entity_count=1,
        relation_count=0,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
    )


def _candidate(config: Settings, item: GraphPublicationItem) -> GraphPublicationSnapshot:
    policy = build_publication_policy_snapshot(config)
    candidate_item = _entity_item(PUBLICATION_ID, item_hash=item.item_hash)
    candidate_item.support_counts = dict(item.support_counts)
    candidate_item.fact_snapshot = dict(item.fact_snapshot)
    return GraphPublicationSnapshot(
        items=(candidate_item,),
        blocked_counts={},
        policy_snapshot=policy,
        policy_snapshot_hash=_sha256_json(policy),
    )


def _patch_candidate(monkeypatch, candidate: GraphPublicationSnapshot):
    async def build(*args, **kwargs):
        return candidate

    monkeypatch.setattr(activation, "build_graph_publication_snapshot", build)


@pytest.mark.parametrize("previous_status", ["active", "degraded"])
def test_activation_switches_snapshot_atomically_and_supersedes_previous(monkeypatch, previous_status):
    config = _config()
    publication, item = _publication(config)
    previous = _current(status=previous_status)
    previous_item = _entity_item(previous.id, status=previous_status)
    _patch_candidate(monkeypatch, _candidate(config, item))
    db = FakeDB(
        objects={(GraphPublication, publication.id): publication},
        query_rows=[
            [_library()],
            [publication],
            [_ontology()],
            [previous],
            [item],
            [previous_item],
        ],
    )

    result = asyncio.run(activate_graph_publication(db, publication.id, config=config))

    assert result.publication is publication
    assert result.previous_publication_id == previous.id
    assert result.idempotent is False
    assert publication.status == "active"
    assert item.status == "active"
    assert previous.status == "superseded"
    assert previous.superseded_by_publication_id == publication.id
    assert previous_item.status == "superseded"
    assert db.commits == 1
    assert db.rollbacks == 0
    assert len(db.update_statements) == 3
    assert [row.action for row in db.added if isinstance(row, AuditLog)] == [
        "graph_publication.superseded",
        "graph_publication.active",
    ]


def test_activation_failure_rolls_back_switch_then_persists_sanitized_failed_state(monkeypatch):
    config = _config()
    publication, item = _publication(config)
    previous = _current()
    changed = _entity_item(PUBLICATION_ID, item_hash="c" * 64)
    _patch_candidate(monkeypatch, _candidate(config, changed))
    db = FakeDB(
        objects={(GraphPublication, publication.id): publication},
        query_rows=[
            [_library()],
            [publication],
            [_ontology()],
            [previous],
            [item],
            [_library()],
            [publication],
        ],
    )

    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(activate_graph_publication(db, publication.id, config=config))

    assert exc_info.value.code == "publication_item_changed"
    assert publication.status == "failed"
    assert publication.error_code == "publication_item_changed"
    assert len(publication.error_message) <= 255
    assert previous.status == "active"
    assert db.update_statements == []
    assert db.rollbacks == 1
    assert db.commits == 1
    failure_audit = [row for row in db.added if isinstance(row, AuditLog)]
    assert [row.action for row in failure_audit] == ["graph_publication.failed"]
    assert set(failure_audit[0].target) == {
        "publication_id",
        "library_id",
        "ontology_version_id",
        "error_code",
    }


def test_reactivating_active_publication_is_idempotent(monkeypatch):
    config = _config()
    publication, _ = _publication(config, status="active")
    db = FakeDB(
        objects={(GraphPublication, publication.id): publication},
        query_rows=[[_library()], [publication]],
    )

    result = asyncio.run(activate_graph_publication(db, publication.id, config=config))

    assert result.idempotent is True
    assert result.publication.status == "active"
    assert db.commits == 1
    assert db.update_statements == []
    assert db.added == []


def test_disabled_activation_fails_closed_and_records_failure():
    config = _config(graph_publication_enabled=False)
    publication, _ = _publication(config)
    db = FakeDB(
        objects={(GraphPublication, publication.id): publication},
        query_rows=[[_library()], [publication], [_library()], [publication]],
    )

    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(activate_graph_publication(db, publication.id, config=config))

    assert exc_info.value.code == "publication_disabled"
    assert publication.status == "failed"
    assert db.rollbacks == 1
    assert db.commits == 1


def test_expected_manifest_mismatch_does_not_poison_planned_publication():
    config = _config()
    publication, _ = _publication(config)
    db = FakeDB(
        objects={(GraphPublication, publication.id): publication},
        query_rows=[[_library()], [publication]],
    )

    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(
            activate_graph_publication(
                db,
                publication.id,
                expected_manifest_hash="f" * 64,
                config=config,
            )
        )

    assert exc_info.value.code == "expected_manifest_mismatch"
    assert publication.status == "planned"
    assert publication.error_code is None
    assert db.rollbacks == 1
    assert db.added == []


def test_rollback_plan_copies_eligible_historical_membership(monkeypatch):
    config = _config()
    target, target_item = _publication(
        config,
        publication_id=TARGET_ID,
        status="superseded",
        parent_publication_id=None,
    )
    target_item.status = "superseded"
    current = _current()
    candidate_item = _entity_item(PUBLICATION_ID, item_hash=target_item.item_hash)
    candidate_item.support_counts = dict(target_item.support_counts)
    candidate_item.fact_snapshot = dict(target_item.fact_snapshot)
    _patch_candidate(
        monkeypatch,
        GraphPublicationSnapshot(
            items=(candidate_item,),
            blocked_counts={},
            policy_snapshot=build_publication_policy_snapshot(config),
            policy_snapshot_hash=_sha256_json(build_publication_policy_snapshot(config)),
        ),
    )
    db = FakeDB(
        objects={(GraphPublication, target.id): target},
        query_rows=[
            [_library()],
            [target],
            [_ontology()],
            [current],
            [],
            [target_item],
            [],
        ],
    )

    result = asyncio.run(
        plan_graph_publication_rollback(
            db,
            target.id,
            idempotency_key="rollback:key-1",
            config=config,
        )
    )

    assert result.publication.source_mode == "rollback"
    assert result.publication.status == "planned"
    assert result.publication.parent_publication_id == current.id
    assert result.publication.rollback_target_publication_id == target.id
    assert len(result.items) == 1
    assert result.items[0].status == "planned"
    assert result.items[0].id != target_item.id
    assert result.items[0].item_hash == target_item.item_hash


def test_rollback_plan_rejects_historical_item_that_is_no_longer_eligible(monkeypatch):
    config = _config()
    target, target_item = _publication(
        config,
        publication_id=TARGET_ID,
        status="superseded",
        parent_publication_id=None,
    )
    current = _current()
    _patch_candidate(
        monkeypatch,
        GraphPublicationSnapshot(
            items=(),
            blocked_counts={"entity_evidence_incomplete": 1},
            policy_snapshot=build_publication_policy_snapshot(config),
            policy_snapshot_hash=_sha256_json(build_publication_policy_snapshot(config)),
        ),
    )
    db = FakeDB(
        objects={(GraphPublication, target.id): target},
        query_rows=[
            [_library()],
            [target],
            [_ontology()],
            [current],
            [],
            [target_item],
        ],
    )

    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(
            plan_graph_publication_rollback(
                db,
                target.id,
                idempotency_key="rollback:key-2",
                config=config,
            )
        )

    assert exc_info.value.code == "rollback_item_ineligible"
    assert not any(isinstance(row, GraphPublication) for row in db.added)


def test_rollback_dry_run_builds_copy_without_persisting(monkeypatch):
    config = _config()
    target, target_item = _publication(
        config,
        publication_id=TARGET_ID,
        status="superseded",
        parent_publication_id=None,
    )
    target_item.status = "superseded"
    current = _current()
    candidate_item = _entity_item(PUBLICATION_ID, item_hash=target_item.item_hash)
    candidate_item.support_counts = dict(target_item.support_counts)
    candidate_item.fact_snapshot = dict(target_item.fact_snapshot)
    _patch_candidate(
        monkeypatch,
        GraphPublicationSnapshot(
            items=(candidate_item,),
            blocked_counts={},
            policy_snapshot=build_publication_policy_snapshot(config),
            policy_snapshot_hash=_sha256_json(build_publication_policy_snapshot(config)),
        ),
    )
    db = FakeDB(
        objects={(GraphPublication, target.id): target},
        query_rows=[
            [_library()],
            [target],
            [_ontology()],
            [current],
            [target_item],
        ],
    )

    result = asyncio.run(
        plan_graph_publication_rollback(
            db,
            target.id,
            idempotency_key="rollback:dry-run",
            dry_run=True,
            config=config,
        )
    )

    assert result.dry_run is True
    assert len(result.items) == 1
    assert result.publication.rollback_target_publication_id == target.id
    assert db.added == []


def test_rollback_command_replay_returns_existing_planned_copy():
    config = _config()
    target, _ = _publication(
        config,
        publication_id=TARGET_ID,
        status="superseded",
        parent_publication_id=None,
    )
    current = _current()
    replay, _ = _publication(
        config,
        publication_id=uuid.UUID("40000000-0000-0000-0000-000000000004"),
        status="planned",
        source_mode="rollback",
        parent_publication_id=current.id,
    )
    replay.rollback_target_publication_id = target.id
    replay.idempotency_key = "rollback:replay"
    db = FakeDB(
        objects={(GraphPublication, target.id): target},
        query_rows=[
            [_library()],
            [target],
            [_ontology()],
            [current],
            [replay],
        ],
    )

    result = asyncio.run(
        plan_graph_publication_rollback(
            db,
            target.id,
            idempotency_key="rollback:replay",
            config=config,
        )
    )

    assert result.reused is True
    assert result.publication is replay
    assert result.items == ()
    assert db.added == []
