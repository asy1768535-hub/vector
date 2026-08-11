from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import settings
from app.models.library import Library
from app.schemas.admin import LibraryCreate, LibraryRead, LibraryUpdate
from app.services.canonical_mapping_shadow import (
    build_canonical_mapping_shadow_authorization,
    resolve_canonical_mapping_shadow,
    run_canonical_mapping_shadow,
)
from tests.test_canonical_mapping_rollout import _FakeMapper, _cases


def test_policy_is_independent_and_fail_closed_by_default():
    assert settings.canonical_mapping_shadow_enabled is False
    assert resolve_canonical_mapping_shadow(
        global_enabled=False,
        policy="enabled",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    ) is False
    assert resolve_canonical_mapping_shadow(
        global_enabled=True,
        policy="inherit",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    ) is False
    assert resolve_canonical_mapping_shadow(
        global_enabled=True,
        policy="disabled",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    ) is False
    assert resolve_canonical_mapping_shadow(
        global_enabled=True,
        policy="enabled",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    ) is True


@pytest.mark.parametrize(
    ("schema_mode", "graph_extraction_enabled", "external_llm_enabled"),
    [
        ("disabled", True, True),
        ("explore", False, True),
        ("explore", True, False),
    ],
)
def test_policy_requires_raw_authority_eligibility(
    schema_mode, graph_extraction_enabled, external_llm_enabled
):
    assert resolve_canonical_mapping_shadow(
        global_enabled=True,
        policy="enabled",
        schema_mode=schema_mode,
        graph_extraction_enabled=graph_extraction_enabled,
        external_llm_enabled=external_llm_enabled,
    ) is False


def test_library_admin_contract_uses_independent_field_and_authoritative_read(monkeypatch):
    create = LibraryCreate(slug="shadow_contract", name="Shadow contract")
    update = LibraryUpdate(canonical_mapping_shadow_policy="enabled")
    assert create.canonical_mapping_shadow_policy == "inherit"
    assert update.canonical_mapping_shadow_policy == "enabled"

    library = Library(
        id=uuid4(),
        slug="shadow_contract",
        name="Shadow contract",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection="lib_shadow_contract",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
        knowledge_artifact_auto_enabled=False,
        summary_artifact_enabled=False,
        outline_artifact_enabled=False,
        knowledge_artifact_external_model_enabled=False,
        knowledge_artifact_allowed_security_levels=[],
        schema_mode="explore",
        canonical_mapping_shadow_policy="enabled",
        lifecycle_mode="managed",
        index_state="ready",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    monkeypatch.setattr(settings, "canonical_mapping_shadow_enabled", False)
    off = LibraryRead.model_validate(library)
    assert off.canonical_mapping_shadow_policy == "enabled"
    assert off.canonical_mapping_shadow_global_enabled is False
    assert off.canonical_mapping_shadow_resolved is False

    monkeypatch.setattr(settings, "canonical_mapping_shadow_enabled", True)
    on = LibraryRead.model_validate(library)
    assert on.canonical_mapping_shadow_global_enabled is True
    assert on.canonical_mapping_shadow_resolved is True


def test_shadow_authorization_keeps_global_and_library_gates_separate():
    library_id = uuid4()
    disabled = build_canonical_mapping_shadow_authorization(
        library_id=library_id,
        global_enabled=True,
        policy="inherit",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    )
    assert disabled.global_execution_enabled is True
    assert disabled.authorized_library_ids == ()
    assert disabled.read_visibility_enabled is False
    assert disabled.bridge_visibility_enabled is False

    enabled = build_canonical_mapping_shadow_authorization(
        library_id=library_id,
        global_enabled=True,
        policy="enabled",
        schema_mode="explore",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    )
    assert enabled.authorized_library_ids == (library_id,)
    assert enabled.read_visibility_enabled is True
    assert enabled.bridge_visibility_enabled is False


def test_disabled_shadow_never_calls_mapper_or_persistence():
    cases = _cases()
    mapper = _FakeMapper()
    persistence = AsyncMock()
    report = asyncio.run(
        run_canonical_mapping_shadow(
            library_id=cases[0].input.library_id,
            global_enabled=False,
            policy="enabled",
            schema_mode="explore",
            graph_extraction_enabled=True,
            external_llm_enabled=True,
            cases=cases,
            mapper=mapper,
            persistence=persistence,
        )
    )
    assert report.status == "disabled"
    assert mapper.calls == []
    persistence.assert_not_awaited()
    assert report.operational_metrics.mapper_call_count == 0
    assert report.operational_metrics.persistence_call_count == 0
    assert report.publication_handoff == "not_authorized"


def test_enabled_shadow_delegates_only_to_typed_m5_runner():
    cases = _cases()
    mapper = _FakeMapper()
    report = asyncio.run(
        run_canonical_mapping_shadow(
            library_id=cases[0].input.library_id,
            global_enabled=True,
            policy="enabled",
            schema_mode="explore",
            graph_extraction_enabled=True,
            external_llm_enabled=True,
            cases=cases,
            mapper=mapper,
        )
    )
    assert mapper.calls == ["asset", "legal", "medical"]
    assert report.status == "succeeded"
    assert report.bridge_visibility_enabled is False
    assert report.publication_handoff == "not_authorized"


def test_shadow_adapter_has_no_forbidden_runtime_imports():
    path = Path(__file__).parents[1] / "app" / "services" / "canonical_mapping_shadow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden = (
        "app.db",
        "app.models",
        "app.workers",
        "app.services.graph_extraction_provider",
        "app.services.graph_extraction_worker",
        "app.services.graph_extraction_materializer",
        "app.services.graph_publication",
    )
    assert not any(item == prefix or item.startswith(prefix + ".") for item in imports for prefix in forbidden)


def test_shadow_policy_migration_is_single_head_and_reversible():
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0060_canonical_mapping_shadow_policy.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0060"' in migration
    assert 'down_revision: Union[str, None] = "0059"' in migration
    assert 'op.add_column("sys_libraries"' not in migration
    assert '"canonical_mapping_shadow_policy"' in migration
    assert "op.create_check_constraint" in migration
    assert "op.drop_constraint" in migration
    assert "op.drop_column" in migration
