from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.config import Settings
from app.models.audit import AuditLog
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.services import graph_evidence
from app.services import graph_publication_reconcile as reconcile
from app.services.graph_publication_planner import (
    GraphPublicationSnapshot,
    _sha256_json,
    build_publication_policy_snapshot,
)


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000005")
OTHER_LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000006")
ONTOLOGY_ID = uuid.UUID("20000000-0000-0000-0000-000000000005")
PUBLICATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000005")
ENTITY_ID = uuid.UUID("40000000-0000-0000-0000-000000000005")
SOURCE_ID = uuid.UUID("40000000-0000-0000-0000-000000000006")
TARGET_ID = uuid.UUID("40000000-0000-0000-0000-000000000007")
RELATION_ID = uuid.UUID("50000000-0000-0000-0000-000000000005")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000005")
DOCUMENT_ID = uuid.UUID("70000000-0000-0000-0000-000000000005")
REVISION_ID = uuid.UUID("80000000-0000-0000-0000-000000000005")


class _Result:
    def __init__(self, rows=(), *, scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def scalar_one(self):
        return self.scalar


class FakeDB:
    def __init__(self, *, objects=None, query_results=None):
        self.objects = objects or {}
        self.query_results = list(query_results or [])
        self.added = []
        self.statements = []
        self.flushes = 0
        self.commits = 0

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.query_results, "unexpected database query"
        result = self.query_results.pop(0)
        return result if isinstance(result, _Result) else _Result(result)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1


def _config(**overrides) -> Settings:
    return Settings(_env_file=None, graph_publication_enabled=True, **overrides)


def _library(library_id: uuid.UUID = LIBRARY_ID) -> Library:
    return Library(
        id=library_id,
        slug=f"v05-m5-{str(library_id)[-4:]}",
        name="v0.5 M5",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"v05_m5_{str(library_id)[-4:]}",
    )


def _ontology(*, status="active") -> OntologyVersion:
    return OntologyVersion(
        id=ONTOLOGY_ID,
        library_id=LIBRARY_ID,
        version_key="default",
        version_no=1,
        status=status,
    )


def _entity_item(*, entity_id=ENTITY_ID, item_hash="a" * 64, status="active"):
    return GraphPublicationItem(
        id=uuid.uuid4(),
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        item_kind="entity",
        entity_id=entity_id,
        item_hash=item_hash,
        status=status,
        support_evidence_ids=[],
        support_counts={"active_mentions": 0},
        fact_snapshot={"item_kind": "entity", "entity_id": str(entity_id)},
    )


def _relation_item(*, item_hash="b" * 64, status="active"):
    return GraphPublicationItem(
        id=uuid.uuid4(),
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        item_kind="relation",
        relation_id=RELATION_ID,
        item_hash=item_hash,
        status=status,
        support_evidence_ids=[str(EVIDENCE_ID)],
        support_counts={"supports": 1, "contradicts": 0},
        fact_snapshot={"item_kind": "relation", "relation_id": str(RELATION_ID)},
    )


def _publication(
    items,
    *,
    status="active",
    config=None,
    policy_snapshot=None,
    blocked_counts=None,
) -> GraphPublication:
    config = config or _config()
    policy = policy_snapshot or build_publication_policy_snapshot(config)
    return GraphPublication(
        id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        status=status,
        source_mode="manual_plan",
        manifest_version=config.graph_publication_manifest_version,
        policy_version=config.graph_publication_policy_version,
        policy_snapshot=policy,
        manifest_hash="c" * 64,
        idempotency_key="m5-publication",
        include_drafts=False,
        plan_options={},
        entity_count=sum(item.item_kind == "entity" for item in items),
        relation_count=sum(item.item_kind == "relation" for item in items),
        blocked_counts=blocked_counts or {},
        blocked_diagnostics={},
        item_hashes_summary={},
    )


def _snapshot(items, config=None) -> GraphPublicationSnapshot:
    config = config or _config()
    policy = build_publication_policy_snapshot(config)
    return GraphPublicationSnapshot(
        items=tuple(items),
        blocked_counts={},
        policy_snapshot=policy,
        policy_snapshot_hash=_sha256_json(policy),
    )


def _patch_core(
    monkeypatch,
    *,
    publication,
    items,
    entity_state,
    relation_state,
    candidate,
    ontology=None,
    capture=None,
):
    db = FakeDB(objects={(OntologyVersion, ONTOLOGY_ID): ontology or _ontology()})

    async def lock_scope(_db, publication_id):
        assert _db is db
        assert publication_id == publication.id
        return _library(), publication

    async def locked_items(_db, current):
        assert _db is db
        assert current is publication
        return items

    async def formal_state(_db, current, current_items):
        assert _db is db
        assert current is publication
        assert current_items is items
        return entity_state, relation_state

    async def build_snapshot(*args, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return candidate

    monkeypatch.setattr(reconcile, "_lock_scope", lock_scope)
    monkeypatch.setattr(reconcile, "_locked_items", locked_items)
    monkeypatch.setattr(reconcile, "_formal_state", formal_state)
    monkeypatch.setattr(reconcile, "build_graph_publication_snapshot", build_snapshot)
    return db


@pytest.mark.parametrize(
    "formal_status",
    ["stale", "disabled", "deleted", "rejected", "pending_review"],
)
def test_entity_formal_status_drift_degrades_item(monkeypatch, formal_status):
    item = _entity_item()
    publication = _publication([item])
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={ENTITY_ID: formal_status},
        relation_state={},
        candidate=_snapshot([_entity_item(item_hash=item.item_hash)]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.status == "degraded"
    assert result.reason_counts == {"formal_status_not_active": 1}
    assert item.status == "degraded"
    assert publication.last_reconciled_at is not None


def test_entity_drift_cascades_to_dependent_relation_and_audit_is_idempotent(monkeypatch):
    entity_item = _entity_item(entity_id=SOURCE_ID)
    relation_item = _relation_item()
    items = [entity_item, relation_item]
    publication = _publication(items)
    candidate = _snapshot(
        [
            _entity_item(entity_id=SOURCE_ID, item_hash=entity_item.item_hash),
            _relation_item(item_hash=relation_item.item_hash),
        ]
    )
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=items,
        entity_state={SOURCE_ID: "disabled", TARGET_ID: "active"},
        relation_state={RELATION_ID: ("active", SOURCE_ID, TARGET_ID)},
        candidate=candidate,
    )

    first = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))
    second = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert first.transitioned_to_degraded is True
    assert first.newly_degraded_item_count == 2
    assert first.reason_counts == {
        "formal_status_not_active": 1,
        "relation_endpoint_not_active": 1,
    }
    assert second.transitioned_to_degraded is False
    assert second.newly_degraded_item_count == 0
    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert [row.action for row in audits] == ["graph_publication.degraded"]
    assert set(audits[0].target) == {
        "publication_id",
        "library_id",
        "ontology_version_id",
        "degraded_item_count",
        "reason_counts",
    }
    assert "Alice" not in str(audits[0].target)


@pytest.mark.parametrize(
    "formal_status",
    ["stale", "disabled", "deleted", "rejected", "pending_review"],
)
def test_relation_formal_status_drift_degrades_item(monkeypatch, formal_status):
    item = _relation_item()
    publication = _publication([item])
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={SOURCE_ID: "active", TARGET_ID: "active"},
        relation_state={RELATION_ID: (formal_status, SOURCE_ID, TARGET_ID)},
        candidate=_snapshot([_relation_item(item_hash=item.item_hash)]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.reason_counts == {"formal_status_not_active": 1}
    assert publication.status == "degraded"
    assert item.status == "degraded"


@pytest.mark.parametrize(
    ("change", "item_factory"),
    [
        ("support_lost", _relation_item),
        ("contradiction_active", _relation_item),
        ("entity_mention_lost", _entity_item),
        ("entity_type_disabled", _entity_item),
        ("relation_type_disabled", _relation_item),
        ("constraint_disabled", _relation_item),
        ("properties_schema_changed", _entity_item),
    ],
)
def test_shared_eligibility_recheck_degrades_items_omitted_from_candidate(
    monkeypatch,
    change,
    item_factory,
):
    item = item_factory()
    publication = _publication([item])
    is_entity = item.item_kind == "entity"
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state=(
            {item.entity_id: "active"}
            if is_entity
            else {SOURCE_ID: "active", TARGET_ID: "active"}
        ),
        relation_state=(
            {} if is_entity else {RELATION_ID: ("active", SOURCE_ID, TARGET_ID)}
        ),
        candidate=_snapshot([]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert change
    assert result.reason_counts == {"eligibility_changed": 1}
    assert item.status == "degraded"


def test_item_hash_drift_degrades_changed_fact(monkeypatch):
    item = _entity_item(item_hash="a" * 64)
    publication = _publication([item])
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={ENTITY_ID: "active"},
        relation_state={},
        candidate=_snapshot([_entity_item(item_hash="f" * 64)]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.reason_counts == {"item_hash_changed": 1}
    assert item.status == "degraded"


def test_inactive_ontology_degrades_all_items_without_building_candidate(monkeypatch):
    items = [_entity_item(), _relation_item()]
    publication = _publication(items)
    candidate_builder = AsyncMock()
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=items,
        entity_state={ENTITY_ID: "active", SOURCE_ID: "active", TARGET_ID: "active"},
        relation_state={RELATION_ID: ("active", SOURCE_ID, TARGET_ID)},
        candidate=_snapshot(items),
        ontology=_ontology(status="disabled"),
    )
    monkeypatch.setattr(reconcile, "build_graph_publication_snapshot", candidate_builder)

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.reason_counts == {"ontology_not_active": 2}
    assert all(item.status == "degraded" for item in items)
    candidate_builder.assert_not_awaited()


def test_recheck_uses_frozen_policy_and_ignores_planner_item_limit(monkeypatch):
    frozen = _config(
        graph_publication_require_entity_evidence=False,
        graph_publication_extracted_entity_min_confidence=0.91,
        graph_publication_extracted_relation_min_confidence=0.92,
        graph_publication_max_items_per_run=1,
        graph_publication_policy_version="frozen-v2",
        graph_publication_manifest_version="frozen-v3",
    )
    mutable_defaults = _config(
        graph_publication_require_entity_evidence=True,
        graph_publication_extracted_entity_min_confidence=0.2,
        graph_publication_extracted_relation_min_confidence=0.3,
        graph_publication_max_items_per_run=999,
    )
    item = _entity_item()
    publication = _publication([item], config=frozen)
    capture = {}
    candidate_items = [_entity_item(item_hash=item.item_hash), _entity_item(entity_id=uuid.uuid4())]
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={ENTITY_ID: "active"},
        relation_state={},
        candidate=_snapshot(candidate_items, frozen),
        capture=capture,
    )

    result = asyncio.run(
        reconcile.reconcile_current_publication(db, publication.id, config=mutable_defaults)
    )

    recheck_config = capture["config"]
    assert recheck_config.graph_publication_require_entity_evidence is False
    assert recheck_config.graph_publication_extracted_entity_min_confidence == 0.91
    assert recheck_config.graph_publication_extracted_relation_min_confidence == 0.92
    assert recheck_config.graph_publication_max_items_per_run == 1
    assert capture["enforce_item_limit"] is False
    assert result.status == "active"
    assert result.degraded_item_count == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda policy: policy.pop("require_entity_evidence"),
        lambda policy: policy.update(max_items_per_run=0),
        lambda policy: policy.update(extracted_entity_min_confidence="nan"),
        lambda policy: policy.update(manifest_version="unexpected"),
        lambda policy: policy.update(policy_version="unexpected"),
    ],
)
def test_invalid_frozen_policy_fails_closed(monkeypatch, mutate):
    config = _config()
    policy = build_publication_policy_snapshot(config)
    mutate(policy)
    item = _entity_item()
    publication = _publication([item], config=config, policy_snapshot=policy)
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={ENTITY_ID: "active"},
        relation_state={},
        candidate=_snapshot([item]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.reason_counts == {"recheck_failed": 1}
    assert publication.status == "degraded"


def test_degraded_publication_never_auto_recovers(monkeypatch):
    item = _entity_item(status="degraded")
    publication = _publication([item], status="degraded")
    db = _patch_core(
        monkeypatch,
        publication=publication,
        items=[item],
        entity_state={ENTITY_ID: "active"},
        relation_state={},
        candidate=_snapshot([_entity_item(item_hash=item.item_hash)]),
    )

    result = asyncio.run(reconcile.reconcile_current_publication(db, publication.id))

    assert result.status == "degraded"
    assert result.transitioned_to_degraded is False
    assert result.newly_degraded_item_count == 0
    assert item.status == "degraded"
    assert not [row for row in db.added if isinstance(row, AuditLog)]


def _reconcile_result(publication_id):
    return reconcile.PublicationReconciliationResult(
        publication_id=publication_id,
        status="active",
        checked_item_count=1,
        newly_degraded_item_count=0,
        degraded_item_count=0,
        transitioned_to_degraded=False,
        reason_counts={},
    )


def test_bounded_scanner_returns_resume_cursor_and_preserves_scope(monkeypatch):
    ids = [
        uuid.UUID("30000000-0000-0000-0000-000000000011"),
        uuid.UUID("30000000-0000-0000-0000-000000000012"),
        uuid.UUID("30000000-0000-0000-0000-000000000013"),
    ]
    db = FakeDB(query_results=[ids])
    worker = AsyncMock(side_effect=[_reconcile_result(ids[0]), _reconcile_result(ids[1])])
    monkeypatch.setattr(reconcile, "reconcile_current_publication", worker)

    batch = asyncio.run(
        reconcile.reconcile_graph_publications_batch(
            db,
            after_publication_id=PUBLICATION_ID,
            library_id=LIBRARY_ID,
            limit=2,
        )
    )

    assert [result.publication_id for result in batch.results] == ids[:2]
    assert batch.next_cursor == ids[1]
    assert [call.args[1] for call in worker.await_args_list] == ids[:2]
    sql = str(db.statements[0])
    assert "graph_publications.library_id" in sql
    assert "graph_publications.id >" in sql


@pytest.mark.parametrize("limit", [0, 101])
def test_bounded_scanner_rejects_invalid_limit(limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        asyncio.run(reconcile.reconcile_graph_publications_batch(FakeDB(), limit=limit))


def test_current_health_is_count_only_and_includes_review_blocks(monkeypatch):
    reconciled_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    publication = _publication(
        [],
        status="degraded",
        blocked_counts={
            "entity_status_pending_review": 2,
            "relation_review_blocked": 3,
            "entity_schema_invalid": 7,
        },
    )
    publication.last_reconciled_at = reconciled_at
    monkeypatch.setattr(
        reconcile,
        "list_current_graph_publication",
        AsyncMock(return_value=publication),
    )
    db = FakeDB(query_results=[_Result(scalar=4)])

    health = asyncio.run(reconcile.get_current_publication_health(db, _library(), ONTOLOGY_ID))

    assert health == reconcile.CurrentPublicationHealth(
        publication_id=publication.id,
        current_publication_status="degraded",
        degraded_item_count=4,
        blocked_pending_review_count=5,
        last_reconciled_at=reconciled_at,
    )


@pytest.mark.parametrize(
    ("function_name", "keyword", "value"),
    [
        ("mark_document_revision_graph_evidence_stale", "document_revision_id", REVISION_ID),
        ("mark_document_graph_evidence_stale", "document_id", DOCUMENT_ID),
        ("mark_evidence_unit_graph_evidence_stale", "evidence_id", EVIDENCE_ID),
    ],
)
def test_stale_lifecycle_hooks_reconcile_after_flush(
    monkeypatch,
    function_name,
    keyword,
    value,
):
    events = []
    mention = SimpleNamespace(status="active")
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        evidence_kind="text",
        status="deleted",
    )

    class HookDB:
        async def get(self, model, object_id):
            if model is EvidenceUnit and object_id == EVIDENCE_ID:
                return evidence
            return None

        async def flush(self):
            events.append("flush")

    async def do_reconcile(db, library):
        assert mention.status == "stale"
        events.append("reconcile")

    monkeypatch.setattr(graph_evidence, "_list_entity_mentions", AsyncMock(return_value=[mention]))
    monkeypatch.setattr(graph_evidence, "_list_relation_evidence", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        graph_evidence,
        "_mark_relations_without_active_evidence_stale",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(graph_evidence, "_reconcile_publications", do_reconcile)

    function = getattr(graph_evidence, function_name)
    result = asyncio.run(function(HookDB(), _library(), **{keyword: value}))

    assert result.stale_entity_mentions == 1
    assert events == ["flush", "reconcile"]


def test_public_relation_stale_helper_reconciles_in_same_transaction(monkeypatch):
    db = SimpleNamespace()
    worker = AsyncMock(return_value=1)
    hook = AsyncMock()
    monkeypatch.setattr(
        graph_evidence,
        "_mark_relations_without_active_evidence_stale",
        worker,
    )
    monkeypatch.setattr(graph_evidence, "_reconcile_publications", hook)

    result = asyncio.run(
        graph_evidence.mark_relations_without_active_evidence_stale(
            db,
            _library(),
            [RELATION_ID],
        )
    )

    assert result == 1
    hook.assert_awaited_once_with(db, ANY)


@pytest.mark.parametrize(
    ("status", "expected_calls"),
    [("active", 1), ("stale", 0), ("deleted", 0)],
)
def test_only_active_contradictory_evidence_triggers_reconciliation(
    monkeypatch,
    status,
    expected_calls,
):
    relation = KnowledgeRelation(
        id=RELATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=uuid.uuid4(),
        source_entity_id=SOURCE_ID,
        target_entity_id=TARGET_ID,
        status="active",
        review_status="approved",
        source_type="manual",
    )
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        evidence_kind="text",
        text_quote="sensitive evidence",
        status="active",
    )

    class HookDB:
        def __init__(self):
            self.added = []

        async def get(self, model, object_id):
            return {
                (EvidenceUnit, EVIDENCE_ID): evidence,
                (KnowledgeRelation, RELATION_ID): relation,
            }.get((model, object_id))

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            return None

    db = HookDB()
    hook = AsyncMock()
    monkeypatch.setattr(graph_evidence, "_reconcile_publications", hook)

    row = asyncio.run(
        graph_evidence.create_relation_evidence(
            db,
            _library(),
            relation_id=RELATION_ID,
            evidence_id=EVIDENCE_ID,
            support_type="contradicts",
            status=status,
        )
    )

    assert row.support_type == "contradicts"
    assert hook.await_count == expected_calls


def test_publication_flag_disables_hook_without_committing(monkeypatch):
    from app.services import graph_publication_reconcile

    db = SimpleNamespace(commit=AsyncMock())
    worker = AsyncMock()
    monkeypatch.setattr(graph_evidence.settings, "graph_publication_enabled", False)
    monkeypatch.setattr(graph_publication_reconcile, "reconcile_library_current_publications", worker)

    asyncio.run(graph_evidence._reconcile_publications(db, _library()))

    worker.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_publication_hook_uses_callers_transaction_without_commit(monkeypatch):
    from app.services import graph_publication_reconcile

    db = SimpleNamespace(commit=AsyncMock())
    worker = AsyncMock(return_value=())
    library = _library(OTHER_LIBRARY_ID)
    monkeypatch.setattr(graph_evidence.settings, "graph_publication_enabled", True)
    monkeypatch.setattr(graph_publication_reconcile, "reconcile_library_current_publications", worker)

    asyncio.run(graph_evidence._reconcile_publications(db, library))

    worker.assert_awaited_once_with(db, library)
    db.commit.assert_not_awaited()
