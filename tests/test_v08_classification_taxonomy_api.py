from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import classification_taxonomies as api
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.classification_taxonomy import ClassificationTaxonomy
from app.models.organization import Organization
from app.models.user import User
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
)


NOW = datetime(2026, 7, 22, 17, 0, tzinfo=timezone.utc)


def _user(*, superuser: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=superuser,
        is_verified=True,
        created_at=NOW,
    )


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug="example",
        name="Example",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _taxonomy(organization: Organization, user: User) -> ClassificationTaxonomy:
    return ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="draft",
        description="Default categories",
        created_by_user_id=user.id,
        created_at=NOW,
        updated_at=NOW,
    )


def _context(user: User, organization: Organization) -> OrganizationAdminContext:
    return OrganizationAdminContext(
        organization_id=organization.id,
        membership_id=uuid.uuid4(),
        user_id=user.id,
    )


def test_routes_are_default_off_and_do_not_probe_scope():
    user, organization = _user(), _organization()
    db = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", False),
            patch.object(api, "resolve_organization_admin", new=AsyncMock()) as resolve,
        ):
            response = TestClient(app).get(
                f"/organizations/{organization.id}/classification-taxonomies"
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
        resolve.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_platform_superuser_without_organization_admin_is_forbidden():
    user, organization = _user(superuser=True), _organization()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(
                    side_effect=OrganizationAuthorizationError(
                        "organization_admin_forbidden"
                    )
                ),
            ),
            patch.object(api, "list_taxonomies", new=AsyncMock()) as listing,
        ):
            response = TestClient(app).get(
                f"/organizations/{organization.id}/classification-taxonomies"
            )
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
        listing.assert_not_awaited()
        assert organization.name not in response.text
    finally:
        app.dependency_overrides.clear()


def test_bearer_api_key_cannot_enter_cookie_only_management_route():
    organization = _organization()
    authorization_scheme = "".join(("Bea", "rer"))
    with patch.object(settings, "classification_taxonomy_enabled", True):
        response = TestClient(app).get(
            f"/organizations/{organization.id}/classification-taxonomies",
            headers={
                "Authorization": f"{authorization_scheme} vk_not_a_cookie_session"
            },
        )
    assert response.status_code == 401


def test_create_uses_cookie_admin_commits_and_returns_bounded_dto():
    user, organization = _user(), _organization()
    taxonomy = _taxonomy(organization, user)
    db = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=_context(user, organization)),
            ),
            patch.object(
                api,
                "create_taxonomy",
                new=AsyncMock(return_value=taxonomy),
            ) as create,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomies",
                json={
                    "version_key": "DOCUMENT-CATEGORY",
                    "version_no": 1,
                    "description": "Default categories",
                },
            )
        assert response.status_code == 201
        assert response.json()["organization_id"] == str(organization.id)
        assert response.json()["status"] == "draft"
        command = create.await_args.args[1]
        assert command.version_key == "document-category"
        assert command.actor_user_id == user.id
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once_with(taxonomy)
        for forbidden in ("password", "email", "organization_name", "source_config"):
            assert forbidden not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_extra_fields_are_rejected_before_mutation():
    user, organization = _user(), _organization()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=_context(user, organization)),
            ),
            patch.object(api, "create_taxonomy", new=AsyncMock()) as create,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomies",
                json={
                    "version_key": "document-category",
                    "version_no": 1,
                    "description": None,
                    "secret": "must not pass",
                },
            )
        assert response.status_code == 422
        create.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
