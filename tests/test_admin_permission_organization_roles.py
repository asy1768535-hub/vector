from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_permissions as permissions_api
from app.auth.backend import get_api_key_strategy, get_jwt_strategy
from app.auth.user_manager import get_user_manager
from app.config import settings
from app.db import get_db
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.services.organization_accounts import UserOrganizationMembership


def _user(*, superuser=False):
    return User(
        id=uuid.uuid4(),
        email="person@example.com",
        hashed_password="unused",
        is_active=True,
        is_superuser=superuser,
        is_verified=True,
    )


def _row(user, *, organization=None, role="member", status="active"):
    organization = organization or Organization(
        id=uuid.uuid4(), slug="example", name="Example Organization", status="active"
    )
    membership = OrganizationMembership(
        id=uuid.uuid4(), organization_id=organization.id, user_id=user.id,
        role=role, status=status,
    )
    return UserOrganizationMembership(organization=organization, membership=membership)


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(permissions_api.router)
    db = AsyncMock()
    cookie_strategy = SimpleNamespace(read_token=AsyncMock())
    key_strategy = SimpleNamespace(read_token=AsyncMock())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_manager] = lambda: SimpleNamespace()
    app.dependency_overrides[get_jwt_strategy] = lambda: cookie_strategy
    app.dependency_overrides[get_api_key_strategy] = lambda: key_strategy
    with TestClient(app) as client:
        yield client, db, cookie_strategy, key_strategy


def _authenticate(client, strategy, actor):
    strategy.read_token.return_value = actor
    client.cookies.set(settings.cookie_name, "test-session")


def test_organization_roles_returns_service_projection_and_manage_capability(api):
    client, db, strategy, _ = api
    actor, target = _user(superuser=True), _user()
    shared = _row(target, role="organization_admin")
    other = _row(target)
    actor_row = _row(actor, organization=shared.organization, role="organization_admin")
    db.get.return_value = target
    _authenticate(client, strategy, actor)
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(permissions_api, "list_user_organizations",
                     new=AsyncMock(side_effect=[(shared, other), (actor_row,)])) as listing,
    ):
        response = client.get(f"/admin/permissions/users/{target.id}/organizations")
    assert response.status_code == 200
    assert response.json() == [
        {
            "membership_id": str(row.membership.id),
            "organization_id": str(row.organization.id),
            "name": row.organization.name,
            "role": row.membership.role,
            "status": row.membership.status,
            "can_manage": row is shared,
        }
        for row in (shared, other)
    ]
    assert [call.kwargs["user_id"] for call in listing.await_args_list] == [target.id, actor.id]
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("actor_role,actor_status", [("member", "active"), ("organization_admin", "disabled")])
def test_organization_roles_cannot_manage_without_active_same_organization_admin(api, actor_role, actor_status):
    client, db, strategy, _ = api
    actor, target = _user(superuser=True), _user()
    target_row = _row(target)
    actor_row = _row(actor, organization=target_row.organization, role=actor_role, status=actor_status)
    db.get.return_value = target
    _authenticate(client, strategy, actor)
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(permissions_api, "list_user_organizations",
                     new=AsyncMock(side_effect=[(target_row,), (actor_row,)])),
    ):
        response = client.get(f"/admin/permissions/users/{target.id}/organizations")
    assert response.status_code == 200
    assert response.json()[0]["can_manage"] is False


def test_organization_roles_cannot_manage_own_membership(api):
    client, db, strategy, _ = api
    actor = _user(superuser=True)
    row = _row(actor, role="organization_admin")
    db.get.return_value = actor
    _authenticate(client, strategy, actor)
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(permissions_api, "list_user_organizations",
                     new=AsyncMock(return_value=(row,))),
    ):
        response = client.get(f"/admin/permissions/users/{actor.id}/organizations")
    assert response.status_code == 200
    assert response.json()[0]["can_manage"] is False


def test_organization_roles_legacy_mode_returns_empty_without_membership_queries(api):
    client, db, strategy, _ = api
    actor, target = _user(superuser=True), _user()
    db.get.return_value = target
    _authenticate(client, strategy, actor)
    with (
        patch.object(settings, "organization_authorization_enabled", False),
        patch.object(permissions_api, "list_user_organizations", new=AsyncMock()) as listing,
    ):
        response = client.get(f"/admin/permissions/users/{target.id}/organizations")
    assert response.status_code == 200
    assert response.json() == []
    listing.assert_not_awaited()


@pytest.mark.parametrize("deleted,enabled", [(False, True), (True, True), (False, False)])
def test_organization_roles_missing_or_deleted_user_is_404(api, deleted, enabled):
    client, db, strategy, _ = api
    actor, target = _user(superuser=True), _user()
    if deleted:
        target.deleted_at = datetime.now(timezone.utc)
    db.get.return_value = target if deleted else None
    _authenticate(client, strategy, actor)
    with patch.object(settings, "organization_authorization_enabled", enabled):
        response = client.get(f"/admin/permissions/users/{target.id}/organizations")
    assert response.status_code == 404


@pytest.mark.parametrize("authenticated,expected", [(False, 401), (True, 403)])
def test_organization_roles_requires_superuser_cookie(api, authenticated, expected):
    client, db, strategy, _ = api
    if authenticated:
        _authenticate(client, strategy, _user())
    response = client.get(f"/admin/permissions/users/{uuid.uuid4()}/organizations")
    assert response.status_code == expected
    db.get.assert_not_awaited()


def test_organization_roles_does_not_accept_superuser_api_key(api):
    client, db, _, key_strategy = api
    key_strategy.read_token.return_value = _user(superuser=True)
    response = client.get(
        f"/admin/permissions/users/{uuid.uuid4()}/organizations",
        headers={"Authorization": "Bearer test-api-key"},
    )
    assert response.status_code == 401
    key_strategy.read_token.assert_not_awaited()
    db.get.assert_not_awaited()
