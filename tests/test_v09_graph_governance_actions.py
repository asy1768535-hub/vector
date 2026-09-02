from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.services import graph_governance_actions as service
from app.services.graph_governance_contracts import (
    DecideGraphGovernanceActionCommand,
    GraphGovernanceError,
    StageEntityMergeCommand,
    StageEntityStatusCommand,
    SubmitEntityCorrectionCommand,
    SubmitManualEntityCommand,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


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

    def scalar_one(self):
        return self.scalar


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        self.flush_count += 1


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="governance",
        name="Governance",
        embedding_model="model",
        embedding_dim=8,
        qdrant_collection="governance",
        created_at=NOW,
    )


def _entity(
    library: Library,
    *,
    entity_type_id: uuid.UUID | None = None,
    status: str = "active",
    name: str = "Acme",
) -> Entity:
    return Entity(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        entity_type_id=entity_type_id or uuid.uuid4(),
        canonical_name=name,
        normalized_name=name.casefold(),
        properties={"kind": "company"},
        status=status,
        source_type="manual",
        created_at=NOW,
        updated_at=NOW,
    )


def _relation(
    library: Library,
    source: Entity,
    target: Entity,
    *,
    status: str = "active",
) -> KnowledgeRelation:
    return KnowledgeRelation(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=source.ontology_version_id,
        relation_type_id=uuid.uuid4(),
        source_entity_id=source.id,
        target_entity_id=target.id,
        properties={"amount": 1},
        status=status,
        review_status="approved",
        source_type="manual",
        created_at=NOW,
        updated_at=NOW,
    )


def _patch_common(monkeypatch, library: Library):
    monkeypatch.setattr(service, "_lock_library", AsyncMock(return_value=library))
    monkeypatch.setattr(service, "_require_access", AsyncMock())
    monkeypatch.setattr(service, "_idempotent_action", AsyncMock(return_value=None))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())


def test_manual_entity_is_staged_idempotently_without_published_mutation(monkeypatch):
    library = _library()
    ontology_id, entity_type_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _patch_common(monkeypatch, library)
    validation = SimpleNamespace(
        ontology_version=SimpleNamespace(id=ontology_id),
        entity_type=SimpleNamespace(id=entity_type_id),
        canonical_name="Acme",
        normalized_name="acme",
        properties={"kind": "company"},
    )
    monkeypatch.setattr(
        service.graph_schema_validator,
        "validate_entity_write",
        AsyncMock(return_value=validation),
    )
    db = _DB()
    import asyncio

    result = asyncio.run(
        service.submit_manual_entity(
            db,
            SubmitManualEntityCommand(
                library.id,
                ontology_id,
                entity_type_id,
                actor_id,
                "entity-create-1",
                "Acme",
                {"kind": "company"},
            ),
            now=NOW,
        )
    )
    entity = next(value for value in db.added if isinstance(value, Entity))
    action = next(value for value in db.added if isinstance(value, GraphGovernanceAction))
    item = next(value for value in db.added if isinstance(value, GraphGovernanceActionItem))
    assert result.created is True
    assert entity.status == "draft"
    assert action.status == "pending_review" and action.target_entity_id == entity.id
    assert item.before_hash == service.entity_governance_state_hash(entity)
    assert item.effect_payload["status"] == "active"
    audit_target = service.audit_log.record.await_args.args[3]
    assert "Acme" not in repr(audit_target)
    assert set(audit_target) == {
        "action_id",
        "action_kind",
        "command_hash",
        "effect_count",
        "library_id",
        "ontology_version_id",
        "reason_code",
        "status",
    }


def test_correction_and_disable_only_write_projected_effects(monkeypatch):
    import asyncio

    library = _library()
    entity = _entity(library)
    related = _relation(library, entity, _entity(library))
    related.ontology_version_id = entity.ontology_version_id
    _patch_common(monkeypatch, library)
    monkeypatch.setattr(service, "_lock_entity", AsyncMock(return_value=entity))
    validation = SimpleNamespace(
        canonical_name="Acme Holdings",
        normalized_name="acme holdings",
        properties={"kind": "company", "verified": True},
    )
    monkeypatch.setattr(
        service.graph_schema_validator,
        "validate_entity_write",
        AsyncMock(return_value=validation),
    )
    before_name = entity.canonical_name
    correction_db = _DB()
    correction = asyncio.run(
        service.submit_entity_correction(
            correction_db,
            SubmitEntityCorrectionCommand(
                library.id,
                entity.id,
                uuid.uuid4(),
                "correct-1",
                service.entity_governance_state_hash(entity),
                canonical_name="Acme Holdings",
                properties={"kind": "company", "verified": True},
                replace_properties=True,
            ),
            now=NOW,
        )
    )
    assert entity.canonical_name == before_name and entity.status == "active"
    assert correction.items[0].effect_payload["canonical_name"] == "Acme Holdings"

    disable_db = _DB(_Result(rows=(related,)))
    disabled = asyncio.run(
        service.stage_entity_status(
            disable_db,
            StageEntityStatusCommand(
                library.id,
                entity.id,
                uuid.uuid4(),
                "disable-1",
                service.entity_governance_state_hash(entity),
                "disable",
                "duplicate_fact",
            ),
            now=NOW,
        )
    )
    assert entity.status == "active" and related.status == "active"
    assert [(item.item_kind, item.effect_kind) for item in disabled.items] == [
        ("entity", "disable"),
        ("relation", "disable"),
    ]


def test_merge_is_same_scope_type_and_preserves_rows_until_activation(monkeypatch):
    import asyncio

    library = _library()
    entity_type_id = uuid.uuid4()
    survivor = _entity(library, entity_type_id=entity_type_id, name="Acme")
    loser = _entity(library, entity_type_id=entity_type_id, name="Acme Ltd")
    survivor_canonical_id = uuid.uuid4()
    loser_canonical_id = uuid.uuid4()
    survivor.canonical_entity_id = survivor_canonical_id
    loser.canonical_entity_id = loser_canonical_id
    loser.ontology_version_id = survivor.ontology_version_id
    relation = _relation(library, loser, survivor)
    alias = EntityAlias(
        id=uuid.uuid4(),
        library_id=library.id,
        entity_id=loser.id,
        alias="Acme Limited",
        normalized_alias="acme limited",
        source_type="manual",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    _patch_common(monkeypatch, library)
    db = _DB(
        _Result(rows=(survivor, loser)),
        _Result(rows=(relation,)),
        _Result(rows=(relation,)),
        _Result(rows=(alias,)),
    )
    result = asyncio.run(
        service.stage_entity_merge(
            db,
            StageEntityMergeCommand(
                library.id,
                survivor.ontology_version_id,
                survivor.id,
                loser.id,
                uuid.uuid4(),
                "merge-1",
                service.entity_governance_state_hash(survivor),
                service.entity_governance_state_hash(loser),
            ),
            now=NOW,
        )
    )
    assert survivor.status == "active" and loser.status == "active"
    assert relation.source_entity_id == loser.id and alias.entity_id == loser.id
    assert survivor.canonical_entity_id == survivor_canonical_id
    assert loser.canonical_entity_id == loser_canonical_id
    assert [(item.item_kind, item.effect_kind) for item in result.items] == [
        ("entity", "retain"),
        ("entity", "disable"),
        ("relation", "reassign"),
        ("alias", "reassign"),
    ]

    incompatible = _entity(library, entity_type_id=uuid.uuid4(), name="Other")
    incompatible.ontology_version_id = survivor.ontology_version_id
    bad_db = _DB(_Result(rows=(survivor, incompatible)))
    with pytest.raises(GraphGovernanceError) as exc_info:
        asyncio.run(
            service.stage_entity_merge(
                bad_db,
                StageEntityMergeCommand(
                    library.id,
                    survivor.ontology_version_id,
                    survivor.id,
                    incompatible.id,
                    uuid.uuid4(),
                    "merge-2",
                    service.entity_governance_state_hash(survivor),
                    service.entity_governance_state_hash(incompatible),
                ),
                now=NOW,
            )
        )
    assert exc_info.value.code == "graph_governance_merge_incompatible"


def test_stale_decision_is_rejected_and_services_never_commit(monkeypatch):
    import asyncio

    library = _library()
    action = GraphGovernanceAction(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        action_kind="entity_disable",
        status="approved",
        target_entity_id=uuid.uuid4(),
        payload={},
        expected_state_hash="a" * 64,
        command_hash="b" * 64,
        idempotency_key="disable-1",
        requested_by_user_id=uuid.uuid4(),
        decided_by_user_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
        decided_at=NOW,
    )
    _patch_common(monkeypatch, library)
    monkeypatch.setattr(service, "_lock_action", AsyncMock(return_value=action))
    with pytest.raises(GraphGovernanceError) as exc_info:
        asyncio.run(
            service.review_action(
                _DB(),
                DecideGraphGovernanceActionCommand(
                    library.id,
                    action.id,
                    uuid.uuid4(),
                    "pending_review",
                    "approve",
                ),
            )
        )
    assert exc_info.value.code == "graph_governance_state_changed"
    assert ".commit(" not in inspect.getsource(service)
