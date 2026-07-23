from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import UniqueConstraint

from app.config import Settings, validate_external_graph_sync_startup
from app.models.external_graph_sync import (
    GraphExternalFactMapping,
    GraphExternalSyncConflict,
    GraphExternalSyncOperation,
    GraphSyncSourcePolicy,
)
from app.schemas.external_graph_sync import ExternalGraphSyncBatchRequest


ONTOLOGY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _entity(**overrides):
    row = {
        "action": "upsert",
        "fact_kind": "entity",
        "external_type": "employee",
        "external_id": "E-1",
        "ontology_version_id": str(ONTOLOGY_ID),
        "entity_type_key": "person",
        "canonical_name": "Alice",
        "normalized_name": "alice",
        "properties": {"department": "Engineering"},
    }
    row.update(overrides)
    return row


def test_0041_is_single_head_and_exactly_reversible() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0041"]
    revision = script.get_revision("0041")
    assert revision is not None
    assert revision.down_revision == "0040"

    path = Path("alembic/versions/0041_v09_external_graph_sync.py")
    source = path.read_text(encoding="utf-8")
    for table in (
        "graph_sync_source_policies",
        "graph_external_sync_operations",
        "graph_external_fact_mappings",
        "graph_external_sync_conflicts",
    ):
        assert f'op.create_table(\n        "{table}"' in source
        assert f'op.drop_table("{table}")' in source

    spec = importlib.util.spec_from_file_location("migration_0041", path)
    assert spec is not None and spec.loader is not None


def test_orm_models_preserve_identity_and_target_constraints() -> None:
    tables = {
        GraphSyncSourcePolicy.__table__.name,
        GraphExternalSyncOperation.__table__.name,
        GraphExternalFactMapping.__table__.name,
        GraphExternalSyncConflict.__table__.name,
    }
    assert tables == {
        "graph_sync_source_policies",
        "graph_external_sync_operations",
        "graph_external_fact_mappings",
        "graph_external_sync_conflicts",
    }
    mapping_unique = {
        tuple(constraint.columns.keys())
        for constraint in GraphExternalFactMapping.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "library_id",
        "sync_source_id",
        "fact_kind",
        "external_type",
        "external_id",
    ) in mapping_unique
    assert GraphExternalFactMapping.__table__.c.entity_id.nullable is True
    assert GraphExternalFactMapping.__table__.c.relation_id.nullable is True
    assert GraphExternalFactMapping.__table__.c.last_operation_id.nullable is False


def test_batch_schema_is_strict_bounded_and_deduplicated() -> None:
    body = ExternalGraphSyncBatchRequest(
        idempotency_key="event-1",
        snapshot_id="snapshot-1",
        complete_snapshot=True,
        items=[_entity()],
    )
    assert body.items[0].fact_kind == "entity"

    with pytest.raises(ValidationError, match="unique"):
        ExternalGraphSyncBatchRequest(
            idempotency_key="event-1",
            items=[_entity(), _entity()],
        )
    with pytest.raises(ValidationError, match="snapshot_id"):
        ExternalGraphSyncBatchRequest(
            idempotency_key="event-1",
            complete_snapshot=True,
            items=[_entity()],
        )
    with pytest.raises(ValidationError):
        ExternalGraphSyncBatchRequest(
            idempotency_key="event-1",
            items=[_entity(extra="forbidden")],
        )


def test_delete_and_locator_contracts_reject_payload_or_secret_material() -> None:
    with pytest.raises(ValidationError, match="identity only"):
        ExternalGraphSyncBatchRequest(
            idempotency_key="delete-1",
            items=[_entity(action="delete")],
        )
    with pytest.raises(ValidationError, match="reserved key"):
        ExternalGraphSyncBatchRequest(
            idempotency_key="event-1",
            items=[
                _entity(
                    source_locator={
                        "metadata": {"credentials": {"token": "secret"}}
                    }
                )
            ],
        )

    deletion = ExternalGraphSyncBatchRequest(
        idempotency_key="delete-1",
        items=[
            {
                "action": "delete",
                "fact_kind": "entity",
                "external_type": "employee",
                "external_id": "E-1",
            }
        ],
    )
    assert deletion.items[0].action == "delete"


def test_external_graph_sync_startup_is_default_off_and_dependency_closed() -> None:
    defaults = Settings()
    assert defaults.external_graph_sync_enabled is False
    validate_external_graph_sync_startup(defaults)

    enabled = Settings(
        external_graph_sync_enabled=True,
        organization_authorization_enabled=True,
        graph_governance_enabled=True,
        graph_publication_enabled=True,
        enable_sync_source_api=True,
    )
    validate_external_graph_sync_startup(enabled)

    with pytest.raises(RuntimeError, match="requires"):
        validate_external_graph_sync_startup(
            enabled.model_copy(update={"graph_governance_enabled": False})
        )
    with pytest.raises(RuntimeError, match="batch limit"):
        validate_external_graph_sync_startup(
            enabled.model_copy(update={"external_graph_sync_max_batch_items": 101})
        )
