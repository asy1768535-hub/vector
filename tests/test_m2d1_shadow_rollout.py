from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.api import admin_libraries
from app.config import Settings, settings
from app.models.library import Library
from app.schemas.admin import LibraryCreate, LibraryRead, LibraryUpdate
from app.services.shadow_rollout import (
    resolve_library_shadow_extraction,
    shadow_extraction_config_for_library,
)


def _library(**changes):
    values = {
        "id": uuid.uuid4(),
        "slug": "shadow_rollout",
        "name": "Shadow Rollout",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "vector_distance": "cosine",
        "chunk_size": 1000,
        "chunk_overlap": 120,
        "retrieval_mode": "dense",
        "qdrant_collection": "lib_shadow_rollout",
        "lifecycle_mode": "managed",
        "index_state": "ready",
        "graph_extraction_enabled": True,
        "external_llm_enabled": True,
        "claim_graph_shadow_policy": "inherit",
        "graph_extraction_allowed_security_levels": ["internal"],
        "knowledge_artifact_auto_enabled": False,
        "summary_artifact_enabled": False,
        "outline_artifact_enabled": False,
        "knowledge_artifact_external_model_enabled": False,
        "knowledge_artifact_allowed_security_levels": [],
        "created_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
    }
    values.update(changes)
    return Library(**values)


@pytest.mark.parametrize(
    ("global_enabled", "policy", "expected"),
    [
        (False, "inherit", False),
        (False, "enabled", True),
        (False, "disabled", False),
        (True, "inherit", True),
        (True, "enabled", True),
        (True, "disabled", False),
    ],
)
def test_shadow_resolver_matrix(global_enabled, policy, expected):
    library = SimpleNamespace(
        claim_graph_shadow_policy=policy,
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    )
    config = shadow_extraction_config_for_library(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=global_enabled),
    )

    assert resolve_library_shadow_extraction(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=global_enabled),
    ) is expected
    assert config.library_policy == policy


@pytest.mark.parametrize("field", ["graph_extraction_enabled", "external_llm_enabled"])
def test_shadow_resolver_has_external_llm_and_graph_extraction_safety_gates(field):
    library = SimpleNamespace(
        claim_graph_shadow_policy="enabled",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    )
    setattr(library, field, False)

    assert resolve_library_shadow_extraction(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=True),
    ) is False
    assert shadow_extraction_config_for_library(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=True),
    ).library_policy == "disabled"


def test_legacy_library_without_policy_is_inherit_and_fail_closed_by_default():
    library = SimpleNamespace(graph_extraction_enabled=True, external_llm_enabled=True)
    config = shadow_extraction_config_for_library(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=False),
    )

    assert config.library_policy == "inherit"
    assert resolve_library_shadow_extraction(
        library,
        settings_obj=SimpleNamespace(graph_claim_shadow_enabled=False),
    ) is False


def test_settings_default_and_env_parse_for_global_shadow_flag():
    assert Settings.model_fields["graph_claim_shadow_enabled"].default is False
    assert Settings(_env_file=None, graph_claim_shadow_enabled=True).graph_claim_shadow_enabled is True


def test_library_policy_schema_defaults_and_rejects_invalid_values():
    assert LibraryCreate(slug="default_shadow", name="Default Shadow").claim_graph_shadow_policy == "inherit"
    assert LibraryUpdate(claim_graph_shadow_policy="enabled").claim_graph_shadow_policy == "enabled"
    with pytest.raises(ValidationError):
        LibraryCreate(slug="bad_shadow", name="Bad Shadow", claim_graph_shadow_policy="maybe")
    with pytest.raises(ValidationError):
        LibraryUpdate(claim_graph_shadow_policy="maybe")


def test_library_orm_has_nonnull_default_and_policy_check():
    column = Library.__table__.c.claim_graph_shadow_policy
    assert column.nullable is False
    assert column.default.arg == "inherit"
    assert column.server_default.arg == "inherit"
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Library.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_lib_claim_graph_shadow_policy" in checks
    assert "inherit" in checks["ck_lib_claim_graph_shadow_policy"]
    assert "enabled" in checks["ck_lib_claim_graph_shadow_policy"]
    assert "disabled" in checks["ck_lib_claim_graph_shadow_policy"]


def test_admin_create_persists_policy_without_changing_other_modes():
    body = LibraryCreate(
        slug="shadow_enabled",
        name="Shadow Enabled",
        claim_graph_shadow_policy="enabled",
    )
    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()) as audit, \
         patch.object(admin_libraries, "_attach_active_classification_labels", new=AsyncMock()):
        library = asyncio.run(admin_libraries.create_library(body, _library(), db))

    assert library.claim_graph_shadow_policy == "enabled"
    assert library.schema_confirmation_policy == "required"
    audit_payload = audit.await_args.args[3]
    assert audit_payload["claim_graph_shadow_policy"] == library.claim_graph_shadow_policy


def test_admin_update_persists_explicit_policy():
    existing = _library(slug="shadow-update")
    row = MagicMock()
    row.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch.object(admin_libraries.audit_log, "record", new=AsyncMock()) as audit:
        result = asyncio.run(
            admin_libraries.update_library(
                "shadow-update",
                LibraryUpdate(claim_graph_shadow_policy="disabled"),
                _library(),
                db,
            )
        )

    assert result.claim_graph_shadow_policy == "disabled"
    audit_payload = audit.await_args.args[3]
    assert audit_payload["claim_graph_shadow_policy"] == result.claim_graph_shadow_policy


def test_library_read_returns_policy_and_resolved_projection(monkeypatch):
    monkeypatch.setattr(settings, "graph_claim_shadow_enabled", True)
    result = LibraryRead.model_validate(_library(claim_graph_shadow_policy="enabled"))

    assert result.claim_graph_shadow_policy == "enabled"
    assert result.claim_graph_shadow_enabled is True

    result = LibraryRead.model_validate(
        _library(claim_graph_shadow_policy="enabled", external_llm_enabled=False)
    )
    assert result.claim_graph_shadow_enabled is False


def test_migration_is_linear_and_reversible():
    spec = importlib.util.spec_from_file_location(
        "migration_0057",
        "alembic/versions/0057_claim_graph_shadow_policy.py",
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0057"
    assert migration.down_revision == "0056"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)
