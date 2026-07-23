from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.models.entity import Entity
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.services import graph_governance_publication as governance
from app.services import graph_publication_activation as activation
from app.services import graph_publication_planner as planner
from app.services.graph_governance_actions import (
    entity_governance_state,
    entity_governance_state_hash,
)
from app.services.graph_governance_contracts import (
    GraphGovernanceEffect,
    GraphGovernanceError,
    PlanGraphGovernancePublicationCommand,
    StageGraphGovernanceActionCommand,
)
from app.services.graph_publication_planner import (
    GraphPublicationPlanResult,
    GraphPublicationProjection,
)


NOW = datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, *results, objects=None):
        self.results = list(results)
        self.objects = objects or {}
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)

    async def get(self, model, identity):
        return self.objects.get((model, identity))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _config() -> Settings:
    return Settings(
        _env_file=None,
        graph_publication_enabled=True,
        graph_governance_enabled=True,
        organization_authorization_enabled=True,
    )


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="governance-publication",
        name="Governance Publication",
        embedding_model="model",
        embedding_dim=8,
        qdrant_collection="governance_publication",
        created_at=NOW,
    )


def _entity(library: Library, *, status="active") -> Entity:
    return Entity(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        entity_type_id=uuid.uuid4(),
        canonical_name="Acme",
        normalized_name="acme",
        properties={"kind": "company"},
        status=status,
        source_type="manual",
        created_at=NOW,
        updated_at=NOW,
    )


def _governance_action(
    library: Library,
    entity: Entity,
    *,
    publication_id: uuid.UUID | None = None,
):
    expected = entity_governance_state_hash(entity)
    command = StageGraphGovernanceActionCommand(
        library.id,
        entity.ontology_version_id,
        uuid.uuid4(),
        "disable-entity-1",
        "entity_disable",
        {"related_relation_count": 0},
        expected,
        "approved",
        target_entity_id=entity.id,
        reason_code="duplicate_fact",
    )
    action = GraphGovernanceAction(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        action_kind="entity_disable",
        status="approved",
        target_entity_id=entity.id,
        payload=command.payload,
        expected_state_hash=expected,
        command_hash=command.command_hash,
        idempotency_key=command.idempotency_key,
        reason_code="review_updated_reason",
        planned_publication_id=publication_id,
        requested_by_user_id=command.actor_user_id,
        decided_by_user_id=command.actor_user_id,
        created_at=NOW,
        updated_at=NOW,
        decided_at=NOW,
    )
    effect = GraphGovernanceEffect(
        0,
        "entity",
        "disable",
        entity.id,
        expected,
        entity_governance_state(entity, status="disabled"),
    )
    item = GraphGovernanceActionItem(
        id=uuid.uuid4(),
        action_id=action.id,
        library_id=library.id,
        ordinal=0,
        item_kind="entity",
        effect_kind="disable",
        entity_id=entity.id,
        before_hash=expected,
        after_hash=effect.after_hash,
        effect_payload=effect.after_state,
        status="planned",
        created_at=NOW,
    )
    return action, item


def _publication(library: Library, ontology_id: uuid.UUID) -> GraphPublication:
    return GraphPublication(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology_id,
        status="planned",
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="a" * 64,
        idempotency_key="publication-1",
        include_drafts=False,
        plan_options={},
        entity_count=0,
        relation_count=0,
        blocked_counts={},
        blocked_diagnostics={},
        item_hashes_summary={},
        created_at=NOW,
        updated_at=NOW,
    )


def test_optional_projection_preserves_ordinary_manifest_and_never_mutates_rows():
    library = _library()
    entity = _entity(library)
    policy_hash = "b" * 64
    ordinary = planner._manifest_hash(
        config=_config(),
        policy_snapshot_hash=policy_hash,
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        source_mode="manual_plan",
        parent_publication_id=None,
        include_drafts=False,
        items=[],
        blocked_counts={},
    )
    explicit_none = planner._manifest_hash(
        config=_config(),
        policy_snapshot_hash=policy_hash,
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        source_mode="manual_plan",
        parent_publication_id=None,
        include_drafts=False,
        items=[],
        blocked_counts={},
        governance_action_set_hash=None,
    )
    governed = planner._manifest_hash(
        config=_config(),
        policy_snapshot_hash=policy_hash,
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        source_mode="manual_plan",
        parent_publication_id=None,
        include_drafts=False,
        items=[],
        blocked_counts={},
        governance_action_set_hash="c" * 64,
    )
    projected = planner._project_entity(
        entity,
        entity_governance_state(entity, status="disabled"),
    )
    assert ordinary == explicit_none and governed != ordinary
    assert projected.status == "disabled" and entity.status == "active"


def test_projection_loader_revalidates_hashes_states_and_canonical_action_set():
    library = _library()
    entity = _entity(library)
    action, item = _governance_action(library, entity)
    db = _DB(
        _Result((action,)),
        _Result((item,)),
        _Result((entity,)),
    )
    loaded = asyncio.run(
        governance.load_graph_governance_projection(
            db,
            library_id=library.id,
            ontology_version_id=entity.ontology_version_id,
            action_ids=(action.id,),
        )
    )
    assert loaded.projection.action_ids == (action.id,)
    assert len(loaded.projection.action_set_hash or "") == 64
    assert loaded.projection.entity_states[entity.id]["status"] == "disabled"
    assert entity.status == "active"

    item.before_hash = "f" * 64
    stale_db = _DB(_Result((action,)), _Result((item,)), _Result((entity,)))
    with pytest.raises(GraphGovernanceError) as exc_info:
        asyncio.run(
            governance.load_graph_governance_projection(
                stale_db,
                library_id=library.id,
                ontology_version_id=entity.ontology_version_id,
                action_ids=(action.id,),
            )
        )
    assert getattr(exc_info.value, "code", None) == "graph_governance_state_changed"


def test_governance_plan_binds_exact_actions_and_apply_is_content_free(monkeypatch):
    library = _library()
    entity = _entity(library)
    publication = _publication(library, entity.ontology_version_id)
    action, item = _governance_action(library, entity)
    loader_db = _DB(_Result((action,)), _Result((item,)), _Result((entity,)))
    loaded = asyncio.run(
        governance.load_graph_governance_projection(
            loader_db,
            library_id=library.id,
            ontology_version_id=entity.ontology_version_id,
            action_ids=(action.id,),
        )
    )
    publication.plan_options = {
        "graph_governance": governance.graph_governance_plan_identity(loaded.projection)
    }
    action.planned_publication_id = publication.id
    monkeypatch.setattr(governance.audit_log, "record", AsyncMock())
    apply_db = _DB()
    asyncio.run(
        governance.apply_loaded_graph_governance_projection(
            apply_db,
            publication=publication,
            loaded=loaded,
            actor_user_id=uuid.uuid4(),
            now=NOW,
        )
    )
    assert entity.status == "disabled"
    assert action.status == "applied" and action.applied_publication_id == publication.id
    assert item.status == "applied" and item.applied_at == NOW
    audit_target = governance.audit_log.record.await_args.args[3]
    assert set(audit_target) == {
        "action_count",
        "action_set_hash",
        "effect_count",
        "library_id",
        "ontology_version_id",
        "publication_id",
    }
    assert "Acme" not in repr(audit_target)


def test_plan_service_passes_projection_and_binds_without_committing(monkeypatch):
    library = _library()
    entity = _entity(library)
    publication = _publication(library, entity.ontology_version_id)
    action, item = _governance_action(library, entity)
    projection = GraphPublicationProjection(
        entity_states={entity.id: entity_governance_state(entity, status="disabled")},
        relation_states={},
        action_ids=(action.id,),
        action_set_hash="c" * 64,
    )
    loaded = governance.LoadedGraphGovernanceProjection(
        projection,
        (action,),
        (item,),
        {entity.id: entity},
        {},
        {},
    )
    monkeypatch.setattr(governance, "_lock_library", AsyncMock(return_value=library))
    monkeypatch.setattr(governance, "_require_management", AsyncMock())
    monkeypatch.setattr(
        governance,
        "load_graph_governance_projection",
        AsyncMock(return_value=loaded),
    )
    plan = AsyncMock(
        return_value=GraphPublicationPlanResult(
            publication,
            (),
            publication.manifest_hash,
            "d" * 64,
            {},
        )
    )
    monkeypatch.setattr(governance, "plan_graph_publication", plan)
    monkeypatch.setattr(governance.audit_log, "record", AsyncMock())
    db = _DB()
    result, action_hash = asyncio.run(
        governance.plan_graph_governance_publication(
            db,
            PlanGraphGovernancePublicationCommand(
                library.id,
                entity.ontology_version_id,
                uuid.uuid4(),
                (action.id,),
                None,
                "plan-governance-1",
            ),
            config=_config(),
        )
    )
    assert result.publication is publication and action_hash == "c" * 64
    assert action.planned_publication_id == publication.id
    assert plan.await_args.kwargs["projection"] is projection
    assert not hasattr(db, "commit")


def test_governance_plan_replays_exact_bound_publication_without_duplicate_audit(
    monkeypatch,
):
    library = _library()
    entity = _entity(library)
    publication = _publication(library, entity.ontology_version_id)
    action, item = _governance_action(
        library,
        entity,
        publication_id=publication.id,
    )
    projection = GraphPublicationProjection(
        entity_states={entity.id: entity_governance_state(entity, status="disabled")},
        relation_states={},
        action_ids=(action.id,),
        action_set_hash="c" * 64,
    )
    loaded = governance.LoadedGraphGovernanceProjection(
        projection,
        (action,),
        (item,),
        {entity.id: entity},
        {},
        {},
    )
    monkeypatch.setattr(governance, "_lock_library", AsyncMock(return_value=library))
    monkeypatch.setattr(governance, "_require_management", AsyncMock())
    loader = AsyncMock(return_value=loaded)
    monkeypatch.setattr(governance, "load_graph_governance_projection", loader)
    monkeypatch.setattr(
        governance,
        "plan_graph_publication",
        AsyncMock(
            return_value=GraphPublicationPlanResult(
                publication,
                (),
                publication.manifest_hash,
                "d" * 64,
                {},
                reused=True,
            )
        ),
    )
    monkeypatch.setattr(governance.audit_log, "record", AsyncMock())
    db = _DB()

    result, action_hash = asyncio.run(
        governance.plan_graph_governance_publication(
            db,
            PlanGraphGovernancePublicationCommand(
                library.id,
                entity.ontology_version_id,
                uuid.uuid4(),
                (action.id,),
                None,
                publication.idempotency_key,
            ),
            config=_config(),
        )
    )

    assert result.reused and action_hash == "c" * 64
    assert loader.await_args.kwargs["allow_existing_publication_binding"] is True
    assert action.planned_publication_id == publication.id
    governance.audit_log.record.assert_not_awaited()
    assert db.flush_count == 0


def test_rollback_projection_only_restores_preserved_status():
    library = _library()
    entity = _entity(library, status="disabled")
    publication = _publication(library, entity.ontology_version_id)
    item = GraphPublicationItem(
        id=uuid.uuid4(),
        publication_id=publication.id,
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        item_kind="entity",
        entity_id=entity.id,
        item_hash="a" * 64,
        status="superseded",
        support_evidence_ids=[],
        support_counts={},
        fact_snapshot={},
    )
    db = _DB(
        _Result((entity,)),
        objects={(Entity, entity.id): entity},
    )
    projection = asyncio.run(
        activation._build_rollback_status_projection(
            db,
            publication=publication,
            items=[item],
        )
    )
    assert projection.entity_states == {entity.id: {"status": "active"}}
    assert entity.status == "disabled"
