from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import admin_libraries as admin_libraries_api
from app.api import api_keys as api_keys_api
from app.api import me as me_api
from app.api import organizations as organizations_api
from app.api import v02_m4 as v02_m4_api
from app.api import v04_graph_extraction as graph_extraction_api
from app.auth.backend import current_active_user, current_cookie_user
from app.auth.user_manager import get_user_manager
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.schemas.organizations import (
    OrganizationMemberCreate,
    OrganizationPermissionGrant,
)
from app.schemas.admin import PermissionMatrixRow
from app.schemas.v02_m4 import SyncBatchRequest
from app.services.organization_accounts import (
    OrganizationAccountError,
    OrganizationAccountResult,
    UserOrganizationMembership,
)
from app.services.organization_permissions import OrganizationPermissionError
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
    PermissionProjection,
)


NOW = datetime(2026, 7, 22, 21, tzinfo=timezone.utc)


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        username="user",
        display_name="User",
        created_at=NOW,
    )


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _membership(organization: Organization, user: User, *, role="member"):
    return OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=role,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _library(organization: Organization) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=f"lib-{uuid.uuid4().hex[:8]}",
        name="Library",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
        created_at=NOW,
    )


def _admin_context(organization: Organization, user: User) -> OrganizationAdminContext:
    return OrganizationAdminContext(
        organization_id=organization.id,
        membership_id=uuid.uuid4(),
        user_id=user.id,
    )


def test_organization_routes_are_mounted_but_default_off():
    user = _user()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(settings, "organization_authorization_enabled", False):
            response = TestClient(app).get("/me/organizations")
        assert response.status_code == 404
        paths = set(app.openapi()["paths"])
        assert "/organizations/{organization_id}/members" in paths
        assert "/organizations/{organization_id}/permissions" in paths
    finally:
        app.dependency_overrides.clear()


def test_me_organizations_returns_only_service_projection():
    user = _user()
    organization = _organization()
    membership = _membership(organization, user, role="organization_admin")
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(
                organizations_api,
                "list_user_organizations",
                new=AsyncMock(
                    return_value=(
                        UserOrganizationMembership(
                            organization=organization,
                            membership=membership,
                        ),
                    )
                ),
            ),
        ):
            response = TestClient(app).get("/me/organizations")
        assert response.status_code == 200
        assert response.json() == [
            {
                "organization_id": str(organization.id),
                "slug": organization.slug,
                "name": organization.name,
                "role": "organization_admin",
            }
        ]
    finally:
        app.dependency_overrides.clear()


def test_me_permissions_exposes_service_owned_organization_identity():
    user = _user()
    organization = _organization()
    library = _library(organization)
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(
                me_api,
                "list_effective_permissions",
                new=AsyncMock(
                    return_value=(
                        PermissionProjection(
                            organization_id=organization.id,
                            library_slug=library.slug,
                            library_name=library.name,
                            actions=("read",),
                        ),
                    )
                ),
            ),
        ):
            response = TestClient(app).get("/me/permissions")
        assert response.status_code == 200
        assert response.json() == [
            {
                "library_slug": library.slug,
                "library_name": library.name,
                "actions": ["read"],
                "organization_id": str(organization.id),
            }
        ]
    finally:
        app.dependency_overrides.clear()


def test_permission_row_keeps_legacy_organization_identity_optional():
    row = PermissionMatrixRow(
        library_slug="legacy-library",
        library_name="Legacy Library",
        actions=["read"],
    )
    assert row.organization_id is None


def test_organization_admin_creates_member_without_credential_leak():
    actor = _user()
    organization = _organization()
    created = _user()
    created.email = "member@example.com"
    created.username = "member"
    created.display_name = "Member"
    membership = _membership(organization, created)
    result = OrganizationAccountResult(user=created, membership=membership)
    db = AsyncMock()
    manager = SimpleNamespace(
        validate_password=AsyncMock(),
        password_helper=SimpleNamespace(hash=MagicMock(return_value="secret-hash")),
    )
    app.dependency_overrides[organizations_api.require_organization_admin] = lambda: (
        _admin_context(organization, actor)
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_manager] = lambda: manager
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(
                organizations_api,
                "create_organization_account",
                new=AsyncMock(return_value=result),
            ) as create,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/members",
                json={
                    "email": "member@example.com",
                    "password": "strong-password-123",
                    "username": "member",
                    "display_name": "Member",
                    "role": "member",
                },
            )
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "member@example.com"
        assert "password" not in body
        assert "hash" not in body
        command = create.await_args.args[1]
        assert command.organization_id == organization.id
        assert command.actor_user_id == actor.id
        assert create.await_args.kwargs["password_hash"] == "secret-hash"
        db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_shared_account_password_reset_returns_conflict_without_commit():
    actor = _user()
    organization = _organization()
    db = AsyncMock()
    manager = SimpleNamespace(
        validate_password=AsyncMock(),
        password_helper=SimpleNamespace(hash=MagicMock(return_value="new-hash")),
    )
    app.dependency_overrides[organizations_api.require_organization_admin] = lambda: (
        _admin_context(organization, actor)
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_manager] = lambda: manager
    try:
        with patch.object(
            organizations_api,
            "reset_organization_account_password",
            new=AsyncMock(
                side_effect=OrganizationAccountError("organization_account_shared")
            ),
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/members/{uuid.uuid4()}/reset-password",
                json={"password": "another-strong-password"},
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "organization_account_shared"
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_permission_commit_failure_compensates():
    db = AsyncMock()
    db.commit.side_effect = RuntimeError("commit failed")
    result = MagicMock()
    with pytest.raises(RuntimeError, match="commit failed"):
        import asyncio

        asyncio.run(organizations_api._commit_permission_mutation(db, result))
    result.compensate.assert_called_once_with()
    db.rollback.assert_awaited_once()


def test_permission_compensation_failure_still_rolls_back():
    db = AsyncMock()
    db.commit.side_effect = RuntimeError("commit failed")
    result = MagicMock()
    result.compensate.side_effect = OrganizationPermissionError(
        "organization_permission_compensation_failed"
    )
    with pytest.raises(OrganizationPermissionError) as exc_info:
        asyncio.run(organizations_api._commit_permission_mutation(db, result))
    assert exc_info.value.code == "organization_permission_compensation_failed"
    db.rollback.assert_awaited_once()


def test_enabled_api_key_issue_requires_and_returns_organization_scope():
    user = _user()
    organization = _organization()
    db = AsyncMock()
    db.add = MagicMock()

    async def refresh(row):
        if row.id is None:
            row.id = uuid.uuid4()
        row.created_at = NOW

    db.refresh = AsyncMock(side_effect=refresh)
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(
                api_keys_api,
                "resolve_organization_membership",
                new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
            ) as resolve,
            patch.object(
                api_keys_api,
                "generate_api_key",
                return_value=("vk_plain-once", "vk_plain-o", "secret-hash"),
            ),
        ):
            missing_scope = TestClient(app).post(
                "/me/api-keys",
                json={"name": "agent"},
            )
            response = TestClient(app).post(
                "/me/api-keys",
                json={"name": "agent", "organization_id": str(organization.id)},
            )
        assert missing_scope.status_code == 422
        assert missing_scope.json()["detail"] == "organization_id is required"
        assert response.status_code == 201
        body = response.json()
        assert body["organization_id"] == str(organization.id)
        assert body["plaintext_key"] == "vk_plain-once"
        assert "key_hash" not in body
        resolve.assert_awaited_once()
        row = db.add.call_args.args[0]
        assert row.organization_id == organization.id
    finally:
        app.dependency_overrides.clear()


def test_enabled_batch_delete_rechecks_delete_without_superuser_bypass():
    user = _user()
    user.is_superuser = True
    organization = _organization()
    library = _library(organization)
    request = SyncBatchRequest(items=[{"action": "delete", "external_id": "record-1"}])
    denial = OrganizationAuthorizationError("organization_forbidden")
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(
            v02_m4_api,
            "resolve_loaded_library_access",
            new=AsyncMock(side_effect=denial),
        ) as authorize,
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(
            v02_m4_api._require_batch_delete_permission(
                request,
                user,
                library,
                AsyncMock(),
            )
        )
    assert exc_info.value.status_code == 403
    assert authorize.await_args.kwargs["action"] == "delete"


def test_enabled_faq_hidden_items_require_organization_library_management():
    user = _user()
    user.is_superuser = True
    organization = _organization()
    library = _library(organization)
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(
            admin_libraries_api,
            "authorize_library_management",
            new=AsyncMock(
                side_effect=OrganizationAuthorizationError("organization_forbidden")
            ),
        ) as authorize,
    ):
        allowed = asyncio.run(
            admin_libraries_api._can_manage_or_see_inactive(
                AsyncMock(),
                user,
                library,
            )
        )
    assert allowed is False
    authorize.assert_awaited_once()


def test_enabled_graph_rerun_uses_management_service_without_superuser_bypass():
    user = _user()
    user.is_superuser = True
    organization = _organization()
    library = _library(organization)
    denial = OrganizationAuthorizationError("organization_forbidden")
    with (
        patch.object(settings, "organization_authorization_enabled", True),
        patch.object(
            graph_extraction_api.deps_module,
            "load_active_library",
            new=AsyncMock(return_value=library),
        ),
        patch.object(
            graph_extraction_api,
            "authorize_library_management",
            new=AsyncMock(side_effect=denial),
        ) as authorize,
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(
            graph_extraction_api._require_graph_rerun_library(
                library.slug,
                user,
                AsyncMock(),
            )
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "admin_required"
    authorize.assert_awaited_once()


def test_organization_write_schemas_reject_platform_role_and_invalid_actions():
    with pytest.raises(ValidationError):
        OrganizationMemberCreate(
            email="member@example.com",
            password="strong-password",
            role="member",
            is_superuser=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        OrganizationPermissionGrant(
            user_id=uuid.uuid4(),
            library_slug="library",
            actions=["read", "read"],
        )
