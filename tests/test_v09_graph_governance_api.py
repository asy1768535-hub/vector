from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import graph_governance as api
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.graph_governance_action import GraphGovernanceAction
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.user import User
from app.services.graph_governance_actions import GraphGovernanceActionResult
from app.services.graph_governance_contracts import GraphGovernanceError
from app.services.graph_publication_planner import GraphPublicationPlanResult


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="governance@example.com",
        hashed_password="x",
        is_active=True,
    )


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


def _action(library: Library, user: User) -> GraphGovernanceAction:
    return GraphGovernanceAction(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        action_kind="entity_create",
        status="pending_review",
        target_entity_id=uuid.uuid4(),
        payload={"canonical_name": "Acme", "properties": {}},
        expected_state_hash="a" * 64,
        command_hash="b" * 64,
        idempotency_key="entity-1",
        requested_by_user_id=user.id,
        created_at=NOW,
        updated_at=NOW,
    )


def test_routes_are_mounted_default_off_before_library_lookup():
    user = _user()
    db = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(settings, "graph_governance_enabled", False):
            response = TestClient(app).get(
                "/libraries/hidden/graph-governance/actions"
            )
        assert response.status_code == 404
        db.execute.assert_not_awaited()
        paths = set(app.openapi()["paths"])
        for path in (
            "/libraries/{slug}/graph-governance/actions",
            "/libraries/{slug}/graph-governance/entities",
            "/libraries/{slug}/graph-governance/relations",
            "/libraries/{slug}/graph-governance/entities/merge",
            "/libraries/{slug}/graph-governance/actions/{action_id}/decision",
        ):
            assert path in paths
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_insert_and_management_dependencies_are_cookie_only_and_distinct():
    user, library = _user(), _library()
    db = AsyncMock()
    with (
        patch.object(settings, "graph_governance_enabled", True),
        patch.object(
            api.deps_module,
            "load_active_library",
            new=AsyncMock(return_value=library),
        ),
        patch.object(
            api,
            "resolve_loaded_library_access",
            new=AsyncMock(),
        ) as insert_auth,
        patch.object(
            api,
            "resolve_loaded_library_management",
            new=AsyncMock(),
        ) as management_auth,
    ):
        insert = await api.require_governance_insert(library.slug, user, db)
        management = await api.require_governance_management(library.slug, user, db)
    assert insert.library is library and management.user is user
    assert insert_auth.await_args.kwargs["action"] == "insert"
    management_auth.assert_awaited_once()
    assert "current_cookie_user" in inspect.getsource(api.require_governance_insert)
    assert "current_cookie_user" in inspect.getsource(api.require_governance_management)


def test_manual_submission_maps_strict_body_and_owns_commit():
    user, library = _user(), _library()
    action = _action(library, user)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    app.dependency_overrides[api.require_governance_insert] = lambda: api.GraphGovernanceContext(
        user, library
    )
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(
            api,
            "submit_manual_entity",
            new=AsyncMock(return_value=GraphGovernanceActionResult(action, (), True)),
        ) as submit:
            response = TestClient(app).post(
                f"/libraries/{library.slug}/graph-governance/entities",
                json={
                    "ontology_version_id": str(action.ontology_version_id),
                    "entity_type_id": str(uuid.uuid4()),
                    "canonical_name": "Acme",
                    "properties": {},
                    "idempotency_key": "entity-1",
                },
            )
        assert response.status_code == 201
        assert response.json()["status"] == "pending_review"
        command = submit.await_args.args[1]
        assert command.library_id == library.id and command.actor_user_id == user.id
        db.commit.assert_awaited_once()

        invalid = TestClient(app).post(
            f"/libraries/{library.slug}/graph-governance/entities",
            json={
                "ontology_version_id": str(action.ontology_version_id),
                "entity_type_id": str(uuid.uuid4()),
                "canonical_name": "Acme",
                "idempotency_key": "entity-2",
                "unexpected": True,
            },
        )
        assert invalid.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_publication_plan_maps_command_commits_and_returns_reuse():
    user, library = _user(), _library()
    ontology_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    action_ids = [uuid.uuid4(), uuid.uuid4()]
    publication = GraphPublication(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology_id,
        parent_publication_id=parent_id,
        status="planned",
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={},
        manifest_hash="a" * 64,
        idempotency_key="governance-plan-1",
        include_drafts=False,
        plan_options={},
        entity_count=2,
        relation_count=1,
        blocked_counts={"relation": 1},
        blocked_diagnostics={},
        item_hashes_summary={},
        created_at=NOW,
        updated_at=NOW,
    )
    result = GraphPublicationPlanResult(
        publication,
        (),
        publication.manifest_hash,
        "b" * 64,
        {"relation": 1},
        reused=True,
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    app.dependency_overrides[api.require_governance_management] = lambda: (
        api.GraphGovernanceContext(user, library)
    )
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(
            api,
            "plan_graph_governance_publication",
            new=AsyncMock(return_value=(result, "c" * 64)),
        ) as plan:
            response = TestClient(app).post(
                f"/libraries/{library.slug}/graph-governance/publications/plan",
                json={
                    "ontology_version_id": str(ontology_id),
                    "action_ids": [str(value) for value in reversed(action_ids)],
                    "expected_parent_publication_id": str(parent_id),
                    "idempotency_key": publication.idempotency_key,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json() == {
        "publication_id": str(publication.id),
        "status": "planned",
        "manifest_hash": "a" * 64,
        "action_set_hash": "c" * 64,
        "parent_publication_id": str(parent_id),
        "entity_count": 2,
        "relation_count": 1,
        "blocked_counts": {"relation": 1},
        "dry_run": False,
        "reused": True,
    }
    command = plan.await_args.args[1]
    assert command.library_id == library.id
    assert command.actor_user_id == user.id
    assert command.action_ids == tuple(sorted(action_ids, key=str))
    assert command.expected_parent_publication_id == parent_id
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_stable_error_mapping_hides_scope_and_invariants():
    assert api._http_error(GraphGovernanceError("graph_governance_not_found")).detail == "not found"
    assert api._http_error(GraphGovernanceError("graph_governance_forbidden")).detail == "forbidden"
    conflict = api._http_error(GraphGovernanceError("graph_governance_merge_conflict"))
    assert conflict.status_code == 409 and conflict.detail == "graph_governance_merge_conflict"
    unavailable = api._http_error(GraphGovernanceError("graph_governance_unavailable"))
    assert unavailable.status_code == 503 and unavailable.detail == "graph_governance_unavailable"
    with pytest.raises(HTTPException) as disabled:
        with patch.object(settings, "graph_governance_enabled", False):
            api._require_runtime()
    assert disabled.value.status_code == 404
