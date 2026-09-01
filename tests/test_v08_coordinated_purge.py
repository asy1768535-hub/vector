from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint


MIGRATION = Path("alembic/versions/0029_v08_coordinated_purge.py")
NOW = datetime(2026, 7, 22, 15, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _QueryDB:
    def __init__(self, *rows):
        self.rows = list(rows)

    async def execute(self, statement):
        assert self.rows, "unexpected database query"
        return _Result(self.rows.pop(0))


def _constraint_sql(table, name: str) -> str:
    constraint = next(item for item in table.constraints if item.name == name)
    assert isinstance(constraint, CheckConstraint)
    return " ".join(str(constraint.sqltext).lower().split())


def test_coordinated_purge_orm_and_migration_contract_match():
    from app.models import RevisionPurgeOperation
    from app.models.graph_publication import GraphPublication

    columns = RevisionPurgeOperation.__table__.columns
    expected = {
        "library_id",
        "retention_record_id",
        "revision_file_id",
        "document_id",
        "document_revision_id",
        "source_publication_id",
        "replacement_publication_id",
        "status",
        "idempotency_key",
        "confirmation_hash",
        "impact_snapshot",
        "impact_hash",
        "source_manifest_hash",
        "replacement_manifest_hash",
        "requested_by_user_id",
        "attempt_count",
        "available_at",
        "worker_id",
        "claim_token",
        "claimed_at",
        "lease_expires_at",
        "finished_at",
        "last_error_code",
    }
    assert expected <= set(columns.keys())
    for name in (
        "ck_revision_purge_operations_status",
        "ck_revision_purge_operations_publications_differ",
        "ck_revision_purge_operations_attempt_count",
        "ck_revision_purge_operations_hashes",
        "ck_revision_purge_operations_impact",
        "ck_revision_purge_operations_claim_shape",
        "ck_revision_purge_operations_available_shape",
        "ck_revision_purge_operations_finished_shape",
        "ck_revision_purge_operations_error_shape",
    ):
        assert _constraint_sql(RevisionPurgeOperation.__table__, name)
    unique = {
        item.name
        for item in RevisionPurgeOperation.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert unique >= {
        "uq_revision_purge_operations_retention",
        "uq_revision_purge_operations_idempotency",
    }
    idempotency = next(
        item
        for item in RevisionPurgeOperation.__table__.constraints
        if item.name == "uq_revision_purge_operations_idempotency"
    )
    assert tuple(column.name for column in idempotency.columns) == (
        "library_id",
        "idempotency_key",
    )
    library_fk = next(
        item
        for item in RevisionPurgeOperation.__table__.foreign_keys
        if item.parent.name == "library_id"
    )
    assert library_fk.ondelete == "RESTRICT"
    assert "coordinated_purge" in _constraint_sql(
        GraphPublication.__table__, "ck_graph_publications_source_mode"
    )

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0029"' in migration
    assert 'down_revision: Union[str, None] = "0028"' in migration
    for name in expected:
        assert f'"{name}"' in migration
    assert "DELETE FROM" not in migration
    assert "UPDATE graph_publications" not in migration


def test_coordinated_purge_startup_is_default_off_and_dependencies_fail_closed():
    from app.config import Settings, validate_revision_coordinated_purge_startup

    default = Settings(_env_file=None)
    assert default.revision_coordinated_purge_enabled is False
    validate_revision_coordinated_purge_startup(
        Settings(
            _env_file=None,
            revision_coordinated_purge_enabled=False,
            revision_coordinated_purge_batch_size=0,
        )
    )
    with pytest.raises(RuntimeError, match="requires graph publication"):
        validate_revision_coordinated_purge_startup(
            Settings(_env_file=None, revision_coordinated_purge_enabled=True)
        )


def test_public_read_accepts_internal_mode_but_plan_request_rejects_it():
    from pydantic import ValidationError

    from app.schemas.v05_graph_publication import (
        GraphPublicationPlanRequest,
        GraphPublicationRead,
    )

    publication = GraphPublicationRead.model_validate(
        {
            "id": uuid.uuid4(),
            "library_id": uuid.uuid4(),
            "ontology_version_id": uuid.uuid4(),
            "status": "planned",
            "healthy": True,
            "publication_enabled": True,
            "source_mode": "coordinated_purge",
            "manifest_version": "v1",
            "policy_version": "v1",
            "manifest_hash": "a" * 64,
            "include_drafts": False,
            "entity_count": 0,
            "relation_count": 0,
            "blocked_counts": {},
        }
    )
    assert publication.source_mode == "coordinated_purge"
    with pytest.raises(ValidationError):
        GraphPublicationPlanRequest.model_validate(
            {"source_mode": "coordinated_purge"}
        )

    from app.services.graph_publication_planner import (
        GraphPublicationPlanError,
        plan_graph_publication,
    )

    with pytest.raises(GraphPublicationPlanError) as exc_info:
        asyncio.run(
            plan_graph_publication(
                object(),
                SimpleNamespace(id=uuid.uuid4()),
                source_mode="coordinated_purge",
            )
        )
    assert exc_info.value.code == "invalid_source_mode"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("revision_coordinated_purge_batch_size", 0, "batch size"),
        ("revision_coordinated_purge_lease_seconds", 29, "lease"),
        ("revision_coordinated_purge_max_attempts", 0, "max attempts"),
        ("revision_coordinated_purge_max_evidence", 100_001, "Evidence limit"),
        ("revision_coordinated_purge_max_affected_items", 10_001, "item limit"),
        ("revision_coordinated_purge_impact_sample", 101, "impact sample"),
    ],
)
def test_coordinated_purge_startup_bounds(field, value, message):
    from app.config import Settings, validate_revision_coordinated_purge_startup

    values = {
        "revision_coordinated_purge_enabled": True,
        "graph_publication_enabled": True,
        "revision_retention_enabled": True,
        "revision_file_storage_enabled": True,
        "revision_cleanup_enabled": True,
        field: value,
    }
    with pytest.raises(RuntimeError, match=message):
        validate_revision_coordinated_purge_startup(
            Settings(_env_file=None, **values)
        )


def _item(
    kind,
    item_id,
    *,
    item_hash,
    support=(),
    source_entity_id=None,
    target_entity_id=None,
):
    from app.models.graph_publication_item import GraphPublicationItem

    snapshot = {"item_kind": kind}
    if source_entity_id is not None:
        snapshot["source_entity_id"] = str(source_entity_id)
    if target_entity_id is not None:
        snapshot["target_entity_id"] = str(target_entity_id)
    return GraphPublicationItem(
        library_id=uuid.uuid4(),
        ontology_version_id=uuid.uuid4(),
        item_kind=kind,
        entity_id=item_id if kind == "entity" else None,
        relation_id=item_id if kind == "relation" else None,
        item_hash=item_hash,
        status="active",
        support_evidence_ids=[str(value) for value in support],
        support_counts={"supports": len(support)},
        fact_snapshot=snapshot,
    )


def _snapshot(items):
    from app.services.graph_publication_planner import GraphPublicationSnapshot

    return GraphPublicationSnapshot(
        items=tuple(items),
        blocked_counts={},
        policy_snapshot={"policy": "v1"},
        policy_snapshot_hash="a" * 64,
    )


def test_minimal_snapshot_recomputes_affected_and_ignores_candidate_only_facts():
    from app.services.revision_coordinated_purge import (
        build_minimal_replacement_snapshot,
    )

    target = uuid.uuid4()
    remaining = uuid.uuid4()
    unrelated_evidence = uuid.uuid4()
    entity_id = uuid.uuid4()
    relation_id = uuid.uuid4()
    candidate_only_id = uuid.uuid4()
    source_entity = _item(
        "entity", entity_id, item_hash="1" * 64, support=[unrelated_evidence]
    )
    source_relation = _item(
        "relation", relation_id, item_hash="2" * 64, support=[target, remaining]
    )
    candidate_entity = _item(
        "entity", entity_id, item_hash="1" * 64, support=[unrelated_evidence]
    )
    candidate_relation = _item(
        "relation", relation_id, item_hash="3" * 64, support=[remaining]
    )
    candidate_only = _item(
        "entity", candidate_only_id, item_hash="4" * 64, support=[remaining]
    )
    replacement, diff, affected = build_minimal_replacement_snapshot(
        source_items=[source_entity, source_relation],
        candidate=_snapshot([candidate_entity, candidate_relation, candidate_only]),
        target_evidence_ids={target},
        max_affected_items=10,
    )
    assert [(row.item_kind, row.entity_id, row.relation_id) for row in replacement.items] == [
        ("entity", entity_id, None),
        ("relation", None, relation_id),
    ]
    assert replacement.items[0].item_hash == source_entity.item_hash
    assert replacement.items[1].item_hash == candidate_relation.item_hash
    assert replacement.items[1].support_evidence_ids == [str(remaining)]
    assert diff["recomputed_item_count"] == 1
    assert diff["removed_item_count"] == 0
    assert diff["candidate_only_item_count"] == 1
    assert affected == (f"relation:{relation_id}",)


def test_affected_entity_closure_removes_endpoint_relation_without_new_support():
    from app.services.revision_coordinated_purge import (
        build_minimal_replacement_snapshot,
    )

    target = uuid.uuid4()
    entity_id = uuid.uuid4()
    other_entity_id = uuid.uuid4()
    relation_id = uuid.uuid4()
    source_entity = _item("entity", entity_id, item_hash="1" * 64, support=[target])
    source_relation = _item(
        "relation",
        relation_id,
        item_hash="2" * 64,
        support=[uuid.uuid4()],
        source_entity_id=entity_id,
        target_entity_id=other_entity_id,
    )
    replacement, diff, affected = build_minimal_replacement_snapshot(
        source_items=[source_entity, source_relation],
        candidate=_snapshot([]),
        target_evidence_ids={target},
        max_affected_items=10,
    )
    assert replacement.items == ()
    assert diff["removed_item_count"] == 2
    assert set(affected) == {f"entity:{entity_id}", f"relation:{relation_id}"}


def test_minimal_snapshot_rejects_unrelated_drift_and_target_evidence_reuse():
    from app.services.revision_coordinated_purge import (
        CoordinatedPurgeError,
        build_minimal_replacement_snapshot,
    )

    target = uuid.uuid4()
    affected_id = uuid.uuid4()
    unrelated_id = uuid.uuid4()
    affected = _item("entity", affected_id, item_hash="1" * 64, support=[target])
    unrelated = _item(
        "entity", unrelated_id, item_hash="2" * 64, support=[uuid.uuid4()]
    )
    changed_unrelated = _item(
        "entity", unrelated_id, item_hash="3" * 64, support=unrelated.support_evidence_ids
    )
    with pytest.raises(CoordinatedPurgeError) as exc_info:
        build_minimal_replacement_snapshot(
            source_items=[affected, unrelated],
            candidate=_snapshot([changed_unrelated]),
            target_evidence_ids={target},
            max_affected_items=10,
        )
    assert exc_info.value.code == "unrelated_publication_drift"

    still_target = _item("entity", affected_id, item_hash="4" * 64, support=[target])
    with pytest.raises(CoordinatedPurgeError) as exc_info:
        build_minimal_replacement_snapshot(
            source_items=[affected],
            candidate=_snapshot([still_target]),
            target_evidence_ids={target},
            max_affected_items=10,
        )
    assert exc_info.value.code == "target_evidence_still_eligible"


def test_disabled_preview_fails_before_database_access():
    from app.config import Settings
    from app.services.revision_coordinated_purge import (
        CoordinatedPurgeError,
        preview_coordinated_revision_purge,
    )

    class Db:
        async def get(self, *args, **kwargs):
            raise AssertionError("disabled preview must not query")

    with pytest.raises(CoordinatedPurgeError) as exc_info:
        asyncio.run(
            preview_coordinated_revision_purge(
                Db(),
                retention_record_id=uuid.uuid4(),
                config=Settings(
                    _env_file=None,
                    revision_coordinated_purge_enabled=False,
                ),
            )
        )
    assert exc_info.value.code == "coordinated_purge_disabled"


def test_disabled_plan_claim_runner_and_reconciler_do_no_database_work():
    from app.config import Settings
    from app.services import revision_coordinated_purge as service
    from app.services import revision_coordinated_purge_runner as runner

    config = Settings(_env_file=None, revision_coordinated_purge_enabled=False)

    class NoDB:
        async def get(self, *args, **kwargs):
            raise AssertionError("disabled coordinated purge must not query")

        async def execute(self, *args, **kwargs):
            raise AssertionError("disabled coordinated purge must not query")

    with pytest.raises(service.CoordinatedPurgeError) as exc_info:
        asyncio.run(
            service.plan_coordinated_revision_purge(
                NoDB(),
                retention_record_id=uuid.uuid4(),
                expected_confirmation_hash="a" * 64,
                idempotency_key="disabled-plan",
                config=config,
            )
        )
    assert exc_info.value.code == "coordinated_purge_disabled"
    assert (
        asyncio.run(
            service.claim_coordinated_purge_operation(
                NoDB(),
                operation_id=uuid.uuid4(),
                worker_id="disabled-worker",
                config=config,
            )
        )
        is None
    )
    assert (
        asyncio.run(
            service.reconcile_coordinated_purge_operations(
                NoDB(), config=config
            )
        )
        == ()
    )

    def no_session():
        raise AssertionError("disabled coordinated purge must not create a session")

    result = asyncio.run(
        runner.run_coordinated_purge_batch(
            no_session,
            worker_id="disabled-worker",
            config=config,
        )
    )
    assert result.cleanup_pending_ids == ()
    assert result.failed_ids == ()


def _preview_scope():
    from app.services.revision_cleanup import LockedCleanupScope

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    old_revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    revision_file_id = uuid.uuid4()
    record = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=old_revision_id,
        replacement_revision_id=replacement_revision_id,
        revision_file_id=revision_file_id,
        status="blocked",
        block_code="active_graph_dependency",
        cleanup_not_before=NOW - timedelta(seconds=1),
    )
    return LockedCleanupScope(
        library=SimpleNamespace(
            id=library_id,
            deleted_at=None,
            lifecycle_mode="managed",
            revision_retention_enabled=True,
        ),
        record=record,
        document=SimpleNamespace(
            id=document_id,
            library_id=library_id,
            deleted_at=None,
            current_revision_id=replacement_revision_id,
        ),
        old_revision=SimpleNamespace(
            id=old_revision_id,
            library_id=library_id,
            document_id=document_id,
            status="superseded",
        ),
        replacement_revision=SimpleNamespace(
            id=replacement_revision_id,
            library_id=library_id,
            document_id=document_id,
            status="ready",
        ),
        revision_file=SimpleNamespace(
            id=revision_file_id,
            library_id=library_id,
            document_id=document_id,
            document_revision_id=old_revision_id,
            lifecycle_status="available",
        ),
    )


def test_preview_hashes_bounded_content_free_impact_and_single_publication(monkeypatch):
    from app.models.graph_publication import GraphPublication
    from app.models.ontology_version import OntologyVersion
    from app.services import revision_coordinated_purge as service

    scope = _preview_scope()
    target_ids = (uuid.uuid4(), uuid.uuid4())
    ontology = OntologyVersion(
        id=uuid.uuid4(),
        library_id=scope.library.id,
        version_key="v1",
        version_no=1,
        status="active",
    )
    source = GraphPublication(
        id=uuid.uuid4(),
        library_id=scope.library.id,
        ontology_version_id=ontology.id,
        status="active",
        source_mode="manual_plan",
        manifest_hash="a" * 64,
        include_drafts=False,
    )
    affected_id = uuid.uuid4()
    source_item = _item(
        "entity", affected_id, item_hash="1" * 64, support=[target_ids[0]]
    )
    source_item.publication_id = source.id
    source_item.library_id = scope.library.id
    source_item.ontology_version_id = ontology.id
    candidate = _snapshot([])
    build_candidate = AsyncMock(return_value=candidate)
    monkeypatch.setattr(
        service, "build_graph_publication_snapshot", build_candidate
    )

    class Db(_QueryDB):
        async def get(self, model, object_id):
            if model is OntologyVersion and object_id == ontology.id:
                return ontology
            return None

    config = _runtime_config(revision_coordinated_purge_impact_sample=1)
    preview = asyncio.run(
        service._build_preview(
            Db(target_ids, [source], [source_item]),
            scope,
            current_time=NOW,
            config=config,
        )
    )
    impact = preview.impact_snapshot
    assert impact["target_evidence_count"] == 2
    assert impact["target_evidence_hash"] == service._sha256(
        [str(value) for value in target_ids]
    )
    assert impact["affected_item_hash"] == service._sha256(
        [f"entity:{affected_id}"]
    )
    assert impact["target_evidence_id_sample"] == [str(target_ids[0])]
    assert impact["affected_item_key_sample"] == [f"entity:{affected_id}"]
    assert preview.impact_hash == service._sha256(impact)
    assert len(preview.confirmation_hash) == 64
    serialized = str(impact).lower()
    for forbidden in (
        "quote",
        "content",
        "file_name",
        "object_key",
        "locator",
        "prompt",
        "credential",
        "http://",
        "https://",
    ):
        assert forbidden not in serialized

    second = GraphPublication(
        id=uuid.uuid4(),
        library_id=scope.library.id,
        ontology_version_id=ontology.id,
        status="degraded",
        source_mode="manual_plan",
        manifest_hash="b" * 64,
        include_drafts=False,
    )
    second_item = _item(
        "entity", uuid.uuid4(), item_hash="2" * 64, support=[target_ids[0]]
    )
    second_item.publication_id = second.id
    with pytest.raises(service.CoordinatedPurgeError) as exc_info:
        asyncio.run(
            service._build_preview(
                Db(target_ids, [source, second], [source_item, second_item]),
                scope,
                current_time=NOW,
                config=config,
            )
        )
    assert exc_info.value.code == "affected_publication_scope_invalid"


def test_preview_requires_due_graph_block_and_plan_rejects_confirmation_drift(
    monkeypatch,
):
    from app.services import revision_coordinated_purge as service

    scope = _preview_scope()
    scope.record.status = "eligible"
    with pytest.raises(service.CoordinatedPurgeError) as exc_info:
        service._validate_scope(scope, NOW)
    assert exc_info.value.code == "retention_not_graph_blocked"

    scope.record.status = "blocked"
    scope.record.block_code = "active_graph_dependency"
    scope.record.cleanup_not_before = NOW + timedelta(seconds=1)
    with pytest.raises(service.CoordinatedPurgeError) as exc_info:
        service._validate_scope(scope, NOW)
    assert exc_info.value.code == "cleanup_not_due"

    scope.record.cleanup_not_before = NOW - timedelta(seconds=1)
    planned_preview = SimpleNamespace(confirmation_hash="a" * 64)

    async def lock(*args, **kwargs):
        return scope

    build = AsyncMock(return_value=planned_preview)
    monkeypatch.setattr(service, "lock_cleanup_scope", lock)
    monkeypatch.setattr(service, "_build_preview", build)
    with pytest.raises(service.CoordinatedPurgeError) as exc_info:
        asyncio.run(
            service.plan_coordinated_revision_purge(
                _QueryDB([]),
                retention_record_id=scope.record.id,
                expected_confirmation_hash="b" * 64,
                idempotency_key="confirmation-drift",
                at=NOW,
                config=_runtime_config(),
            )
        )
    assert exc_info.value.code == "confirmation_hash_changed"


def test_coordinated_activation_accepts_candidate_subset_but_not_changed_item():
    from app.config import Settings
    from app.models.graph_publication import GraphPublication
    from app.services.graph_publication_activation import (
        GraphPublicationActivationError,
        _validate_stored_snapshot,
    )
    from app.services.graph_publication_planner import (
        _manifest_hash,
        _sha256_json,
        build_publication_policy_snapshot,
    )

    config = Settings(_env_file=None)
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    kept_id = uuid.uuid4()
    extra_id = uuid.uuid4()
    kept = _item("entity", kept_id, item_hash="1" * 64)
    kept.library_id = library_id
    kept.ontology_version_id = ontology_id
    kept.status = "planned"
    extra = _item("entity", extra_id, item_hash="2" * 64)
    extra.library_id = library_id
    extra.ontology_version_id = ontology_id
    policy = build_publication_policy_snapshot(config)
    publication = GraphPublication(
        id=uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=ontology_id,
        source_mode="coordinated_purge",
        manifest_version=config.graph_publication_manifest_version,
        policy_version=config.graph_publication_policy_version,
        policy_snapshot=policy,
        parent_publication_id=parent_id,
        include_drafts=False,
        entity_count=1,
        relation_count=0,
        blocked_counts={},
    )
    publication.manifest_hash = _manifest_hash(
        config=config,
        policy_snapshot_hash=_sha256_json(policy),
        library_id=library_id,
        ontology_version_id=ontology_id,
        source_mode="coordinated_purge",
        parent_publication_id=parent_id,
        include_drafts=False,
        items=[kept],
        blocked_counts={},
    )
    candidate = _snapshot([kept, extra])
    candidate = candidate.__class__(
        items=candidate.items,
        blocked_counts={},
        policy_snapshot=policy,
        policy_snapshot_hash=_sha256_json(policy),
    )
    _validate_stored_snapshot(publication, [kept], candidate, config=config)

    changed = _item("entity", kept_id, item_hash="3" * 64)
    changed.library_id = library_id
    changed.ontology_version_id = ontology_id
    changed_candidate = candidate.__class__(
        items=(changed, extra),
        blocked_counts={},
        policy_snapshot=policy,
        policy_snapshot_hash=_sha256_json(policy),
    )
    with pytest.raises(GraphPublicationActivationError) as exc_info:
        _validate_stored_snapshot(
            publication,
            [kept],
            changed_candidate,
            config=config,
        )
    assert exc_info.value.code == "coordinated_purge_item_ineligible"


def test_explicit_coordinated_plan_replays_only_an_exact_manifest():
    from app.models.graph_publication import GraphPublication
    from app.models.library import Library
    from app.models.ontology_version import OntologyVersion
    from app.services.graph_publication_planner import (
        GraphPublicationPlanError,
        graph_publication_snapshot_manifest_hash,
        plan_explicit_graph_publication_snapshot,
    )

    config = _runtime_config()
    library = Library(
        id=uuid.uuid4(),
        slug="coordinated-replay",
        name="Coordinated Replay",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="coordinated_replay",
    )
    ontology = OntologyVersion(
        id=uuid.uuid4(),
        library_id=library.id,
        version_key="v1",
        version_no=1,
        status="active",
    )
    parent = GraphPublication(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        status="active",
        source_mode="manual_plan",
        manifest_hash="a" * 64,
    )
    item = _item("entity", uuid.uuid4(), item_hash="1" * 64)
    item.library_id = library.id
    item.ontology_version_id = ontology.id
    item.status = "planned"
    snapshot = _snapshot([item])
    manifest_hash = graph_publication_snapshot_manifest_hash(
        library_id=library.id,
        ontology_version_id=ontology.id,
        source_mode="coordinated_purge",
        parent_publication_id=parent.id,
        include_drafts=False,
        snapshot=snapshot,
        config=config,
    )
    existing = GraphPublication(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        status="planned",
        source_mode="coordinated_purge",
        manifest_hash=manifest_hash,
        parent_publication_id=parent.id,
        idempotency_key="purge:exact-replay",
        policy_snapshot=snapshot.policy_snapshot,
        blocked_counts=snapshot.blocked_counts,
    )

    class Db(_QueryDB):
        async def get(self, model, object_id):
            if model is OntologyVersion and object_id == ontology.id:
                return ontology
            return None

    result = asyncio.run(
        plan_explicit_graph_publication_snapshot(
            Db([], [parent], [existing]),
            library,
            ontology_version_id=ontology.id,
            snapshot=snapshot,
            source_mode="coordinated_purge",
            include_drafts=False,
            idempotency_key="purge:exact-replay",
            expected_parent_publication_id=parent.id,
            config=config,
        )
    )
    assert result.reused is True
    assert result.publication is existing

    changed = _item("entity", item.entity_id, item_hash="2" * 64)
    changed.library_id = library.id
    changed.ontology_version_id = ontology.id
    changed.status = "planned"
    with pytest.raises(GraphPublicationPlanError) as exc_info:
        asyncio.run(
            plan_explicit_graph_publication_snapshot(
                Db([], [parent], [existing]),
                library,
                ontology_version_id=ontology.id,
                snapshot=_snapshot([changed]),
                source_mode="coordinated_purge",
                include_drafts=False,
                idempotency_key="purge:exact-replay",
                expected_parent_publication_id=parent.id,
                config=config,
            )
        )
    assert exc_info.value.code == "idempotency_conflict"


def _runtime_config(**overrides):
    from app.config import Settings

    values = {
        "revision_coordinated_purge_enabled": True,
        "graph_publication_enabled": True,
        "revision_retention_enabled": True,
        "revision_file_storage_enabled": True,
        "revision_cleanup_enabled": True,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _operation_scope(*, status="planned"):
    from app.services.revision_cleanup import LockedCleanupScope
    from app.services.revision_coordinated_purge import _LockedOperationScope

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    revision_file_id = uuid.uuid4()
    retention_id = uuid.uuid4()
    source_id = uuid.uuid4()
    replacement_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        library_id=library_id,
        manifest_hash="a" * 64,
        status="active",
        superseded_by_publication_id=None,
    )
    replacement = SimpleNamespace(
        id=replacement_id,
        library_id=library_id,
        source_mode="coordinated_purge",
        manifest_hash="b" * 64,
        parent_publication_id=source_id,
        status="planned",
        plan_options={
            "retention_record_id": str(retention_id),
            "confirmation_hash": "c" * 64,
            "target_evidence_hash": "e" * 64,
        },
    )
    operation = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        retention_record_id=retention_id,
        revision_file_id=revision_file_id,
        document_id=document_id,
        document_revision_id=revision_id,
        source_publication_id=source_id,
        replacement_publication_id=replacement_id,
        status=status,
        idempotency_key="purge-1",
        confirmation_hash="c" * 64,
        impact_snapshot={"target_evidence_hash": "e" * 64},
        impact_hash="d" * 64,
        source_manifest_hash=source.manifest_hash,
        replacement_manifest_hash=replacement.manifest_hash,
        requested_by_user_id=None,
        attempt_count=0,
        available_at=NOW - timedelta(seconds=1),
        worker_id=None,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        finished_at=None,
        last_error_code=None,
    )
    retention = SimpleNamespace(
        id=retention_id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        replacement_revision_id=replacement_revision_id,
        revision_file_id=revision_file_id,
        status="blocked",
    )
    library = SimpleNamespace(id=library_id)
    retention_scope = LockedCleanupScope(
        library=library,
        record=retention,
        document=SimpleNamespace(id=document_id, library_id=library_id),
        old_revision=SimpleNamespace(id=revision_id),
        replacement_revision=SimpleNamespace(id=replacement_revision_id),
        revision_file=SimpleNamespace(
            id=revision_file_id,
            lifecycle_status="available",
        ),
    )
    return _LockedOperationScope(
        operation=operation,
        retention_scope=retention_scope,
        source_publication=source,
        replacement_publication=replacement,
    )


def _patch_operation_scope(monkeypatch, scope):
    async def lock(*args, **kwargs):
        return scope

    monkeypatch.setattr(
        "app.services.revision_coordinated_purge._lock_operation_scope", lock
    )


def test_existing_operation_replays_after_retention_state_changes(monkeypatch):
    from app.services import revision_coordinated_purge as service

    scope = _operation_scope(status="completed")
    scope.retention_scope.record.status = "cleaned"
    preview = SimpleNamespace(confirmation_hash=scope.operation.confirmation_hash)
    rebuild = AsyncMock(return_value=preview)

    async def lock(*args, **kwargs):
        return scope.retention_scope

    async def reject_preview(*args, **kwargs):
        raise AssertionError("an idempotent replay must not rebuild a live preview")

    monkeypatch.setattr(service, "lock_cleanup_scope", lock)
    monkeypatch.setattr(service, "_rebuild_existing_preview", rebuild)
    monkeypatch.setattr(service, "_build_preview", reject_preview)

    result = asyncio.run(
        service.plan_coordinated_revision_purge(
            _QueryDB([scope.operation]),
            retention_record_id=scope.operation.retention_record_id,
            expected_confirmation_hash=scope.operation.confirmation_hash,
            idempotency_key=scope.operation.idempotency_key,
            at=NOW,
            config=_runtime_config(),
        )
    )

    assert result.reused is True
    assert result.operation is scope.operation
    assert result.preview is preview
    rebuild.assert_awaited_once()


def test_coordinated_activation_rejects_missing_or_changed_operation():
    from app.models.graph_publication import GraphPublication
    from app.services.graph_publication_activation import (
        GraphPublicationActivationError,
        _reject_coordinated_target_evidence,
    )

    publication = GraphPublication(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        ontology_version_id=uuid.uuid4(),
        source_mode="coordinated_purge",
        parent_publication_id=uuid.uuid4(),
        plan_options={},
    )
    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(
            _reject_coordinated_target_evidence(
                _QueryDB([]), publication, []
            )
        )
    assert exc_info.value.code == "coordinated_purge_operation_invalid"

    scope = _operation_scope()
    publication.id = scope.replacement_publication.id
    publication.library_id = scope.operation.library_id
    publication.parent_publication_id = scope.operation.source_publication_id
    publication.manifest_hash = scope.operation.replacement_manifest_hash
    publication.plan_options = {
        **scope.replacement_publication.plan_options,
        "target_evidence_hash": "f" * 64,
    }
    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(
            _reject_coordinated_target_evidence(
                _QueryDB([scope.operation]), publication, []
            )
        )
    assert exc_info.value.code == "coordinated_purge_operation_changed"


def test_coordinated_activation_rejects_target_revision_evidence():
    from app.models.graph_publication import GraphPublication
    from app.services.graph_publication_activation import (
        GraphPublicationActivationError,
        _reject_coordinated_target_evidence,
    )

    scope = _operation_scope()
    publication = GraphPublication(
        id=scope.operation.replacement_publication_id,
        library_id=scope.operation.library_id,
        ontology_version_id=uuid.uuid4(),
        source_mode="coordinated_purge",
        parent_publication_id=scope.operation.source_publication_id,
        manifest_hash=scope.operation.replacement_manifest_hash,
        plan_options=scope.replacement_publication.plan_options,
    )
    target_evidence_id = uuid.uuid4()
    item = _item(
        "entity", uuid.uuid4(), item_hash="1" * 64, support=[target_evidence_id]
    )
    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(
            _reject_coordinated_target_evidence(
                _QueryDB([scope.operation], [(target_evidence_id,)]),
                publication,
                [item],
            )
        )
    assert exc_info.value.code == "coordinated_purge_target_evidence"


def test_operation_claim_rotates_lease_and_stale_worker_cannot_finalize(monkeypatch):
    from dataclasses import replace

    from app.services.revision_coordinated_purge import (
        CoordinatedPurgeError,
        claim_coordinated_purge_operation,
        finalize_coordinated_purge_activation,
    )

    scope = _operation_scope()
    _patch_operation_scope(monkeypatch, scope)
    claim = asyncio.run(
        claim_coordinated_purge_operation(
            object(),
            operation_id=scope.operation.id,
            worker_id="purge-worker",
            at=NOW,
            config=_runtime_config(),
        )
    )
    assert claim is not None
    assert scope.operation.status == "processing"
    assert scope.operation.attempt_count == 1
    assert scope.operation.lease_expires_at == NOW + timedelta(seconds=300)
    stale = replace(claim, claim_token=uuid.uuid4())
    with pytest.raises(CoordinatedPurgeError) as exc_info:
        asyncio.run(
            finalize_coordinated_purge_activation(
                object(),
                claim=stale,
                at=NOW,
                config=_runtime_config(),
            )
        )
    assert exc_info.value.code == "coordinated_purge_claim_stale"


def test_finalization_stales_old_bindings_then_releases_retention(monkeypatch):
    from app.services.revision_coordinated_purge import (
        claim_coordinated_purge_operation,
        finalize_coordinated_purge_activation,
    )

    scope = _operation_scope()
    _patch_operation_scope(monkeypatch, scope)
    claim = asyncio.run(
        claim_coordinated_purge_operation(
            object(),
            operation_id=scope.operation.id,
            worker_id="purge-worker",
            at=NOW,
            config=_runtime_config(),
        )
    )
    scope.replacement_publication.status = "active"
    scope.source_publication.status = "superseded"
    scope.source_publication.superseded_by_publication_id = (
        scope.replacement_publication.id
    )
    stale = AsyncMock()
    refresh = AsyncMock(return_value=SimpleNamespace(status="eligible"))
    audit = AsyncMock()
    monkeypatch.setattr(
        "app.services.graph_evidence.mark_document_revision_graph_evidence_stale",
        stale,
    )
    monkeypatch.setattr(
        "app.services.revision_coordinated_purge.refresh_revision_retention",
        refresh,
    )
    monkeypatch.setattr(
        "app.services.revision_coordinated_purge.audit_log.record", audit
    )
    db = object()
    result = asyncio.run(
        finalize_coordinated_purge_activation(
            db,
            claim=claim,
            at=NOW,
            config=_runtime_config(),
        )
    )
    assert result.status == "cleanup_pending"
    assert scope.operation.status == "cleanup_pending"
    assert scope.operation.claim_token is None
    stale.assert_awaited_once_with(
        db,
        scope.retention_scope.library,
        document_revision_id=scope.operation.document_revision_id,
    )
    assert refresh.await_count == 1
    assert audit.await_count == 1


def test_batch_calls_activation_outside_operation_transaction(monkeypatch):
    from app.services import revision_coordinated_purge_runner as runner
    from app.services.revision_coordinated_purge import (
        CoordinatedPurgeExecutionResult,
        _claim_from_scope,
    )

    scope = _operation_scope(status="processing")
    scope.operation.attempt_count = 1
    scope.operation.available_at = None
    scope.operation.worker_id = "purge-worker"
    scope.operation.claim_token = uuid.uuid4()
    scope.operation.claimed_at = NOW
    scope.operation.lease_expires_at = NOW + timedelta(minutes=5)
    claim = _claim_from_scope(scope)
    transaction_depth = 0

    class Transaction:
        async def __aenter__(self):
            nonlocal transaction_depth
            transaction_depth += 1

        async def __aexit__(self, exc_type, exc, traceback):
            nonlocal transaction_depth
            transaction_depth -= 1

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return Transaction()

        async def rollback(self):
            return None

    async def candidates(*args, **kwargs):
        return (scope.operation.id,)

    async def claimed(*args, **kwargs):
        return claim

    async def activate(*args, **kwargs):
        assert transaction_depth == 0
        return SimpleNamespace()

    async def finalize(*args, **kwargs):
        return CoordinatedPurgeExecutionResult(
            scope.operation.id, "cleanup_pending", "eligible"
        )

    monkeypatch.setattr(runner, "coordinated_purge_candidate_ids", candidates)
    monkeypatch.setattr(runner, "claim_coordinated_purge_operation", claimed)
    monkeypatch.setattr(runner, "activate_graph_publication", activate)
    monkeypatch.setattr(runner, "finalize_coordinated_purge_activation", finalize)
    result = asyncio.run(
        runner.run_coordinated_purge_batch(
            lambda: Session(),
            worker_id="purge-worker",
            at=NOW,
            config=_runtime_config(),
        )
    )
    assert transaction_depth == 0
    assert result.cleanup_pending_ids == (scope.operation.id,)
    assert result.failed_ids == ()
