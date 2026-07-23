from __future__ import annotations

import io
import uuid
from dataclasses import replace

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.config import Settings, validate_graph_governance_startup
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.schemas.graph_governance import (
    GraphGovernanceActionPageRead,
    GraphGovernanceEntityCorrectionRequest,
    GraphGovernanceEntityMergeRequest,
    GraphGovernanceManualEntityRequest,
    GraphGovernancePublicationPlanRequest,
)
from app.services.graph_governance_contracts import (
    GRAPH_GOVERNANCE_ERROR_CODES,
    CancelGraphGovernanceActionCommand,
    GraphGovernanceActionBinding,
    GraphGovernanceEffect,
    GraphGovernanceError,
    PlanGraphGovernancePublicationCommand,
    StageGraphGovernanceActionCommand,
    canonical_governance_absent_state_hash,
    canonical_governance_action_set_hash,
    canonical_governance_expected_state_hash,
    graph_governance_transition,
)


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _fk(column) -> tuple[str, str | None, str | None]:
    foreign_key = next(iter(column.foreign_keys))
    return foreign_key.target_fullname, foreign_key.ondelete, foreign_key.name


def _constraint_names(table, kind) -> set[str]:
    return {
        item.name
        for item in table.constraints
        if isinstance(item, kind) and item.name is not None
    }


def test_graph_governance_models_are_exported_and_match_durable_shape():
    from app.models import GraphGovernanceAction as ExportedAction
    from app.models import GraphGovernanceActionItem as ExportedItem

    assert ExportedAction is GraphGovernanceAction
    assert ExportedItem is GraphGovernanceActionItem
    action = GraphGovernanceAction.__table__
    item = GraphGovernanceActionItem.__table__
    assert set(action.c.keys()) == {
        "id",
        "library_id",
        "ontology_version_id",
        "action_kind",
        "status",
        "target_entity_id",
        "target_relation_id",
        "target_alias_id",
        "survivor_entity_id",
        "loser_entity_id",
        "payload",
        "expected_state_hash",
        "command_hash",
        "idempotency_key",
        "reason_code",
        "planned_publication_id",
        "applied_publication_id",
        "requested_by_user_id",
        "decided_by_user_id",
        "cancelled_by_user_id",
        "created_at",
        "updated_at",
        "decided_at",
        "cancelled_at",
        "applied_at",
    }
    assert set(item.c.keys()) == {
        "id",
        "action_id",
        "library_id",
        "ordinal",
        "item_kind",
        "effect_kind",
        "entity_id",
        "relation_id",
        "alias_id",
        "before_hash",
        "after_hash",
        "effect_payload",
        "status",
        "applied_at",
        "created_at",
    }
    assert isinstance(action.c.payload.type, JSONB)
    assert isinstance(item.c.effect_payload.type, JSONB)
    assert action.c.expected_state_hash.type.length == 64
    assert action.c.idempotency_key.type.length == 128
    assert item.c.ordinal.nullable is False
    assert _fk(action.c.library_id) == (
        "sys_libraries.id",
        "RESTRICT",
        "fk_graph_governance_actions_library",
    )
    assert _fk(action.c.planned_publication_id) == (
        "graph_publications.id",
        "SET NULL",
        "fk_graph_governance_actions_planned",
    )
    assert _fk(item.c.action_id) == (
        "graph_governance_actions.id",
        "CASCADE",
        "fk_graph_governance_action_items_action",
    )
    assert _fk(item.c.relation_id) == (
        "knowledge_relations.id",
        "RESTRICT",
        "fk_graph_governance_action_items_relation",
    )

    assert {
        "ck_graph_governance_actions_kind",
        "ck_graph_governance_actions_status",
        "ck_graph_governance_actions_hashes",
        "ck_graph_governance_actions_payload",
        "ck_graph_governance_actions_target",
        "ck_graph_governance_actions_lifecycle",
    } <= _constraint_names(action, CheckConstraint)
    assert {
        "ck_graph_governance_action_items_kind",
        "ck_graph_governance_action_items_effect",
        "ck_graph_governance_action_items_target",
        "ck_graph_governance_action_items_hashes",
        "ck_graph_governance_action_items_payload",
        "ck_graph_governance_action_items_status",
    } <= _constraint_names(item, CheckConstraint)
    assert "uq_graph_governance_actions_library_key" in _constraint_names(
        action, UniqueConstraint
    )
    assert "uq_graph_governance_action_items_action_order" in _constraint_names(
        item, UniqueConstraint
    )
    names = [
        value.name
        for table in (action, item)
        for value in (*table.constraints, *table.indexes)
        if value.name
    ]
    assert max(map(len, names)) <= 63


def test_0038_migration_is_additive_reversible_and_precedes_current_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0042"]
    assert script.get_revision("0038").down_revision == "0037"
    assert script.get_revision("0039").down_revision == "0038"
    assert script.get_revision("0041").down_revision == "0040"
    upgrade = _offline("upgrade", "0037:0038")
    downgrade = _offline("downgrade", "0038:0037")
    assert "create table graph_governance_actions" in upgrade
    assert "create table graph_governance_action_items" in upgrade
    assert "drop table graph_governance_action_items" in downgrade
    assert "drop table graph_governance_actions" in downgrade
    assert downgrade.index("drop table graph_governance_action_items") < downgrade.index(
        "drop table graph_governance_actions"
    )
    for forbidden in ("update entities", "update knowledge_relations", "delete from"):
        assert forbidden not in upgrade + downgrade


def test_feature_defaults_off_and_requires_authorization_and_publication():
    assert Settings().graph_governance_enabled is False
    validate_graph_governance_startup(Settings())
    for values in (
        {"graph_governance_enabled": True},
        {
            "graph_governance_enabled": True,
            "organization_authorization_enabled": True,
        },
        {
            "graph_governance_enabled": True,
            "graph_publication_enabled": True,
        },
    ):
        with pytest.raises(RuntimeError, match="graph governance requires"):
            validate_graph_governance_startup(Settings(**values))
    validate_graph_governance_startup(
        Settings(
            graph_governance_enabled=True,
            organization_authorization_enabled=True,
            graph_publication_enabled=True,
        )
    )


def _entity_action(**overrides) -> StageGraphGovernanceActionCommand:
    entity_id = overrides.pop("target_entity_id", uuid.uuid4())
    values = {
        "library_id": uuid.uuid4(),
        "ontology_version_id": uuid.uuid4(),
        "actor_user_id": uuid.uuid4(),
        "idempotency_key": "entity-create-1",
        "action_kind": "entity_create",
        "payload": {"canonical_name": "Acme", "properties": {"kind": "company"}},
        "expected_state_hash": canonical_governance_absent_state_hash(
            item_kind="entity", item_id=entity_id
        ),
        "initial_status": "pending_review",
        "target_entity_id": entity_id,
    }
    values.update(overrides)
    return StageGraphGovernanceActionCommand(**values)


def test_command_payload_and_hash_are_canonical_and_caller_input_is_copied():
    payload = {"properties": {"z": 1, "a": [2, 3]}, "canonical_name": "Acme"}
    command = _entity_action(payload=payload)
    payload["canonical_name"] = "Changed"
    same = replace(command, payload={"canonical_name": "Acme", "properties": {"a": [2, 3], "z": 1}})
    assert command.payload["canonical_name"] == "Acme"
    assert command.command_hash == same.command_hash
    assert len(command.command_hash) == 64

    with pytest.raises(GraphGovernanceError) as wrong_target:
        replace(command, action_kind="relation_create")
    assert wrong_target.value.code == "graph_governance_request_invalid"
    with pytest.raises(GraphGovernanceError):
        replace(command, payload={"value": "x" * 65_537})


def test_expected_item_and_action_set_hashes_are_stable_and_ordered():
    entity_id = uuid.uuid4()
    before = canonical_governance_expected_state_hash(
        item_kind="entity",
        item_id=entity_id,
        state={"status": "active", "canonical_name": "Acme"},
    )
    effect = GraphGovernanceEffect(
        ordinal=0,
        item_kind="entity",
        effect_kind="disable",
        item_id=entity_id,
        before_hash=before,
        after_state={"canonical_name": "Acme", "status": "disabled"},
    )
    assert len(effect.after_hash) == 64
    assert len(effect.item_hash) == 64

    one = GraphGovernanceActionBinding(uuid.uuid4(), "a" * 64, (effect.item_hash,))
    two = GraphGovernanceActionBinding(uuid.uuid4(), "b" * 64, ("c" * 64,))
    assert canonical_governance_action_set_hash((one, two)) == canonical_governance_action_set_hash(
        (two, one)
    )
    changed = GraphGovernanceActionBinding(two.action_id, two.command_hash, ("d" * 64,))
    assert canonical_governance_action_set_hash((one, two)) != canonical_governance_action_set_hash(
        (one, changed)
    )


def test_lifecycle_and_management_commands_fail_closed():
    assert graph_governance_transition("pending_review", "approve") == "approved"
    assert graph_governance_transition("approved", "apply") == "applied"
    with pytest.raises(GraphGovernanceError) as terminal:
        graph_governance_transition("rejected", "apply")
    assert terminal.value.code == "graph_governance_state_changed"
    assert set(GRAPH_GOVERNANCE_ERROR_CODES) >= {
        "graph_governance_idempotency_conflict",
        "graph_governance_merge_incompatible",
        "graph_governance_publication_changed",
    }

    action_id = uuid.uuid4()
    cancel = CancelGraphGovernanceActionCommand(
        uuid.uuid4(), action_id, uuid.uuid4(), "approved", "duplicate_fact"
    )
    assert cancel.reason_code == "duplicate_fact"
    with pytest.raises(GraphGovernanceError):
        replace(cancel, reason_code="contains spaces")

    action_ids = (uuid.uuid4(), uuid.uuid4())
    plan = PlanGraphGovernancePublicationCommand(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), action_ids, None, "plan-1"
    )
    assert plan.action_ids == tuple(sorted(action_ids, key=str))
    with pytest.raises(GraphGovernanceError):
        replace(plan, action_ids=(action_ids[0], action_ids[0]))


def test_http_contracts_are_strict_bounded_and_require_explicit_fences():
    request = GraphGovernanceManualEntityRequest(
        ontology_version_id=uuid.uuid4(),
        entity_type_id=uuid.uuid4(),
        canonical_name="  Acme   Corp  ",
        properties={"registered": True},
        idempotency_key=" request-1 ",
    )
    assert request.canonical_name == "Acme Corp"
    assert request.idempotency_key == "request-1"
    with pytest.raises(ValidationError):
        GraphGovernanceManualEntityRequest(
            ontology_version_id=uuid.uuid4(),
            entity_type_id=uuid.uuid4(),
            canonical_name="Acme",
            idempotency_key="request-1",
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        GraphGovernanceEntityCorrectionRequest(
            expected_state_hash="a" * 64,
            idempotency_key="correct-1",
        )
    with pytest.raises(ValidationError):
        GraphGovernanceEntityMergeRequest(
            ontology_version_id=uuid.uuid4(),
            survivor_entity_id=(same_id := uuid.uuid4()),
            loser_entity_id=same_id,
            expected_survivor_state_hash="a" * 64,
            expected_loser_state_hash="b" * 64,
            idempotency_key="merge-1",
        )
    with pytest.raises(ValidationError):
        GraphGovernancePublicationPlanRequest(
            ontology_version_id=uuid.uuid4(),
            action_ids=[same_id, same_id],
            idempotency_key="plan-1",
        )
    with pytest.raises(ValidationError):
        GraphGovernanceActionPageRead(items=[], total=0, limit=101, offset=0)


def test_durable_and_audit_contracts_exclude_source_or_secret_fields():
    forbidden = {
        "quote_text",
        "evidence_text_snapshot",
        "source_text",
        "raw_response",
        "prompt",
        "credential",
        "api_key",
        "secret",
        "url",
        "exception",
    }
    for table in (GraphGovernanceAction.__table__, GraphGovernanceActionItem.__table__):
        assert forbidden.isdisjoint(table.c.keys())
