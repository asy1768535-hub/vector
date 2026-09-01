from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import personal_library_scopes as api
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.user import User
from app.models.user_library_scope import UserLibraryScope
from app.services.personal_library_scopes import PersonalLibraryScopeError


NOW = datetime(2026, 7, 22, 23, 45, tzinfo=timezone.utc)


def _user() -> User:
    return User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", hashed_password="hash",
        is_active=True, is_superuser=False, is_verified=True, created_at=NOW,
    )


def _scope(org_id, user_id):
    return UserLibraryScope(
        id=uuid.uuid4(), organization_id=org_id, user_id=user_id, scope_kind="named",
        name="Legal", normalized_name="legal", created_at=NOW, updated_at=NOW,
    )


def _body(org_id):
    return {"organization_id": str(org_id), "name": "Legal", "library_slugs": ["legal"]}


def test_routes_are_mounted_and_default_off():
    user, org_id = _user(), uuid.uuid4()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(settings, "personal_library_scopes_enabled", False):
            response = TestClient(app).get(f"/me/library-scopes?organization_id={org_id}")
        assert response.status_code == 404
        paths = app.openapi()["paths"]
        assert "/me/library-scopes" in paths
        assert "/me/library-scopes/last-used" in paths
        assert "/me/library-scopes/{scope_id}/resolve" in paths
    finally:
        app.dependency_overrides.clear()


def test_create_is_cookie_scoped_strict_and_commits():
    user, org_id = _user(), uuid.uuid4()
    scope = _scope(org_id, user.id)
    db = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "personal_library_scopes_enabled", True),
            patch.object(api, "create_named_scope", new=AsyncMock(return_value=scope)) as create,
        ):
            response = TestClient(app).post("/me/library-scopes", json=_body(org_id))
        assert response.status_code == 201
        assert response.json()["scope_id"] == str(scope.id)
        create.assert_awaited_once()
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once_with(scope)

        invalid = _body(org_id)
        invalid["extra"] = "forbidden"
        with patch.object(settings, "personal_library_scopes_enabled", True):
            rejected = TestClient(app).post("/me/library-scopes", json=invalid)
        assert rejected.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_conflict_and_not_found_are_stable_without_raw_diagnostics():
    user, org_id = _user(), uuid.uuid4()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "personal_library_scopes_enabled", True),
            patch.object(
                api,
                "create_named_scope",
                new=AsyncMock(
                    side_effect=PersonalLibraryScopeError(
                        "personal_scope_incompatible", "secret model diagnostic"
                    )
                ),
            ),
        ):
            conflict = TestClient(app).post("/me/library-scopes", json=_body(org_id))
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "personal_scope_incompatible"}
        assert "secret model diagnostic" not in conflict.text

        with (
            patch.object(settings, "personal_library_scopes_enabled", True),
            patch.object(
                api, "resolve_named_scope",
                new=AsyncMock(side_effect=PersonalLibraryScopeError("personal_scope_not_found")),
            ),
        ):
            missing = TestClient(app).get(
                f"/me/library-scopes/{uuid.uuid4()}/resolve?organization_id={org_id}"
            )
        assert missing.status_code == 404
        assert missing.json() == {"detail": "not found"}
    finally:
        app.dependency_overrides.clear()
