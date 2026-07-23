from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, validate_schema_lifecycle_startup
from app.models.schema_lifecycle_action import SchemaLifecycleAction
from app.schemas.schema_lifecycle import (
    SchemaActivationRequest,
    SchemaAttributeCreateRequest,
    SchemaCloneRequest,
    SchemaConstraintCreateRequest,
    SchemaEntityTypeCreateRequest,
    SchemaItemDisableRequest,
    SchemaRelationTypeCreateRequest,
)
from app.services.schema_lifecycle_contracts import (
    SCHEMA_LIFECYCLE_ACTION_KINDS,
    SCHEMA_LIFECYCLE_ERROR_CODES,
    SCHEMA_LIFECYCLE_TARGET_KINDS,
    SchemaLifecycleCommand,
    SchemaLifecycleError,
    canonical_schema_lifecycle_state_hash,
    deterministic_schema_clone_id,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0039_v09_schema_lifecycle_actions.py"


def _command(**overrides) -> SchemaLifecycleCommand:
    values = {
        "library_id": uuid.uuid4(),
        "ontology_version_id": uuid.uuid4(),
        "actor_user_id": uuid.uuid4(),
        "action_kind": "update_item",
        "target_kind": "entity_type",
        "target_id": uuid.uuid4(),
        "expected_state_hash": "a" * 64,
        "idempotency_key": "schema-edit-1",
        "payload": {"label": "Company", "properties_schema": {"type": "object"}},
    }
    values.update(overrides)
    return SchemaLifecycleCommand(**values)


def test_schema_lifecycle_defaults_off_and_requires_organization_authorization():
    assert Settings(_env_file=None).schema_lifecycle_enabled is False
    validate_schema_lifecycle_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="Schema lifecycle requires"):
        validate_schema_lifecycle_startup(
            Settings(_env_file=None, schema_lifecycle_enabled=True)
        )
    validate_schema_lifecycle_startup(
        Settings(
            _env_file=None,
            schema_lifecycle_enabled=True,
            organization_authorization_enabled=True,
        )
    )


def test_action_model_and_migration_are_closed_bounded_and_private():
    assert set(SCHEMA_LIFECYCLE_ACTION_KINDS) == {
        "clone_version",
        "create_item",
        "update_item",
        "disable_item",
        "activate_version",
    }
    assert set(SCHEMA_LIFECYCLE_TARGET_KINDS) == {
        "ontology_version",
        "entity_type",
        "relation_type",
        "attribute",
        "constraint",
    }
    assert set(SchemaLifecycleAction.__table__.c.keys()) == {
        "id",
        "library_id",
        "ontology_version_id",
        "action_kind",
        "target_kind",
        "target_id",
        "expected_state_hash",
        "command_hash",
        "idempotency_key",
        "result_payload",
        "actor_user_id",
        "applied_at",
        "created_at",
    }
    table_text = str(SchemaLifecycleAction.__table__)
    assert table_text == "schema_lifecycle_actions"
    constraint_names = {
        constraint.name for constraint in SchemaLifecycleAction.__table__.constraints
    }
    assert {
        "ck_schema_lifecycle_actions_kind",
        "ck_schema_lifecycle_actions_target_kind",
        "ck_schema_lifecycle_actions_hashes",
        "ck_schema_lifecycle_actions_payload",
        "ck_schema_lifecycle_actions_idempotency_key",
        "uq_schema_lifecycle_actions_library_key",
    } <= constraint_names
    forbidden = {
        "source_text",
        "prompt",
        "raw_response",
        "credential",
        "api_key",
        "secret",
        "object_key",
        "storage_url",
        "exception",
    }
    assert forbidden.isdisjoint(SchemaLifecycleAction.__table__.c.keys())

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0039"' in migration
    assert 'down_revision: Union[str, None] = "0038"' in migration
    assert 'op.create_table(\n        "schema_lifecycle_actions"' in migration
    assert 'op.drop_table("schema_lifecycle_actions")' in migration
    for value in (*SCHEMA_LIFECYCLE_ACTION_KINDS, *SCHEMA_LIFECYCLE_TARGET_KINDS):
        assert value in migration
    for value in forbidden:
        assert value not in migration


def test_command_hash_and_clone_identity_are_canonical_and_fail_closed():
    payload = {"properties_schema": {"required": ["name"], "type": "object"}, "label": "Company"}
    command = _command(payload=payload)
    payload["label"] = "Changed"
    same = replace(
        command,
        payload={"label": "Company", "properties_schema": {"type": "object", "required": ["name"]}},
    )
    assert command.payload["label"] == "Company"
    assert command.command_hash == same.command_hash
    assert len(command.command_hash) == 64
    assert deterministic_schema_clone_id(
        command.library_id,
        command.ontology_version_id,
        command.idempotency_key,
        command.command_hash,
    ) == deterministic_schema_clone_id(
        command.library_id,
        command.ontology_version_id,
        command.idempotency_key,
        command.command_hash,
    )
    assert canonical_schema_lifecycle_state_hash({"b": 2, "a": 1}) == (
        canonical_schema_lifecycle_state_hash({"a": 1, "b": 2})
    )

    with pytest.raises(SchemaLifecycleError) as bad_action:
        replace(command, action_kind="delete_version")
    assert bad_action.value.code == "schema_lifecycle_request_invalid"
    with pytest.raises(SchemaLifecycleError):
        replace(command, idempotency_key="contains spaces")
    with pytest.raises(SchemaLifecycleError):
        replace(command, expected_state_hash="not-a-hash")
    with pytest.raises(SchemaLifecycleError):
        replace(command, payload={"value": "x" * 65_537})
    assert set(SCHEMA_LIFECYCLE_ERROR_CODES) >= {
        "schema_lifecycle_state_changed",
        "schema_lifecycle_idempotency_conflict",
        "schema_lifecycle_invalid_draft",
        "schema_lifecycle_dependency_conflict",
    }


def test_http_command_contracts_are_strict_and_bounded():
    common = {"expected_version_state_hash": "a" * 64, "idempotency_key": "intent-1"}
    assert SchemaCloneRequest(**common).idempotency_key == "intent-1"
    assert SchemaEntityTypeCreateRequest(key="company", label="Company", **common).key == "company"
    assert (
        SchemaRelationTypeCreateRequest(
            key="invests_in",
            label="Invests In",
            direction="directed",
            default_review_policy="pending_review",
            **common,
        ).requires_evidence
        is True
    )
    owner_id = uuid.uuid4()
    assert SchemaAttributeCreateRequest(
        owner_kind="entity_type",
        owner_type_id=owner_id,
        key="registered_at",
        label="Registered At",
        value_type="date",
        **common,
    ).owner_type_id == owner_id
    assert SchemaConstraintCreateRequest(
        relation_type_id=uuid.uuid4(),
        source_entity_type_id=uuid.uuid4(),
        target_entity_type_id=uuid.uuid4(),
        **common,
    ).cardinality is None
    assert SchemaItemDisableRequest(**common).expected_version_state_hash == "a" * 64
    assert SchemaActivationRequest(
        expected_version_state_hash="a" * 64,
        expected_active_version_id=None,
        confirmation="activate_schema_version",
        idempotency_key="activate-1",
    ).confirmation == "activate_schema_version"

    with pytest.raises(ValidationError):
        SchemaCloneRequest(**common, unexpected=True)
    with pytest.raises(ValidationError):
        SchemaEntityTypeCreateRequest(key="contains spaces", label="Company", **common)
    with pytest.raises(ValidationError):
        SchemaAttributeCreateRequest(
            owner_kind="entity_type",
            owner_type_id=owner_id,
            key="kind",
            label="Kind",
            value_type="enum",
            enum_values=[],
            **common,
        )
    with pytest.raises(ValidationError):
        SchemaActivationRequest(
            expected_version_state_hash="a" * 64,
            expected_active_version_id=None,
            confirmation="yes",
            idempotency_key="activate-1",
        )
