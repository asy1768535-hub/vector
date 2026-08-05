from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Index

from app.config import Settings, settings, validate_organization_authorization_startup
from app.models.api_key import ApiKey
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID, Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User


NOW = datetime(2026, 7, 22, 20, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []
        self.added = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.rows, "unexpected database query"
        return _Result(self.rows.pop(0))

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _user(*, superuser=False) -> User:
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
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _library(organization: Organization) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=f"lib_{uuid.uuid4().hex[:8]}",
        name="Library",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
        created_at=NOW,
    )


def _membership(
    organization: Organization,
    user: User,
    *,
    role="member",
    status="active",
) -> OrganizationMembership:
    return OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=role,
        status=status,
        disabled_at=NOW if status == "disabled" else None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_0031_api_key_orm_migration_and_offline_sql_match():
    column = ApiKey.__table__.columns.organization_id
    assert column.nullable is False
    assert str(column.server_default.arg) == str(DEFAULT_ORGANIZATION_ID)
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.name == "fk_sys_api_keys_organization"
    assert foreign_key.target_fullname == "sys_organizations.id"
    assert foreign_key.ondelete == "RESTRICT"
    index = next(
        item
        for item in ApiKey.__table__.indexes
        if item.name == "ix_sys_api_keys_organization_user_revoked"
    )
    assert isinstance(index, Index)
    assert tuple(item.name for item in index.columns) == (
        "organization_id",
        "user_id",
        "revoked_at",
    )

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision("0031")
    assert migration is not None and migration.down_revision == "0030"
    next_migration = script.get_revision("0032")
    assert next_migration is not None
    assert next_migration.down_revision == "0031"
    personal_scope_migration = script.get_revision("0033")
    assert personal_scope_migration is not None
    assert personal_scope_migration.down_revision == "0032"
    taxonomy_migration = script.get_revision("0034")
    assert taxonomy_migration is not None
    assert taxonomy_migration.down_revision == "0033"
    assert script.get_heads() == ["0052"]
    upgrade = _offline("upgrade", "0030:0031")
    downgrade = _offline("downgrade", "0031:0030")
    for fragment in (
        "add column organization_id uuid",
        "add constraint fk_sys_api_keys_organization",
        "update sys_api_keys",
        "alter column organization_id set not null",
        "create index ix_sys_api_keys_organization_user_revoked",
    ):
        assert fragment in upgrade
    for fragment in (
        "drop index ix_sys_api_keys_organization_user_revoked",
        "drop constraint fk_sys_api_keys_organization",
        "drop column organization_id",
    ):
        assert fragment in downgrade
    assert "delete from sys_api_keys" not in upgrade + downgrade


def test_runtime_defaults_off_and_rejects_public_registration_when_enabled():
    assert Settings.model_fields["organization_authorization_enabled"].default is False
    validate_organization_authorization_startup(
        Settings(organization_authorization_enabled=False)
    )
    invalid = Settings(
        organization_authorization_enabled=True,
        allow_public_registration=True,
    )
    with pytest.raises(RuntimeError, match="public registration"):
        validate_organization_authorization_startup(invalid)


def test_enabled_library_access_enforces_role_action_superuser_and_key_scope(monkeypatch):
    from app.services import organization_authorization as service

    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    organization = _organization()
    library = _library(organization)
    admin = _user()
    admin_membership = _membership(organization, admin, role="organization_admin")

    with patch.object(service, "has_permission", return_value=False) as permission:
        access = asyncio.run(
            service.resolve_library_access(
                _DB([(library, admin_membership, organization)]),
                user=admin,
                library_slug=library.slug,
                action="read",
            )
        )
    assert access.role == "organization_admin"
    permission.assert_not_called()

    with patch.object(service, "has_permission", return_value=False):
        with pytest.raises(service.OrganizationAuthorizationError):
            asyncio.run(
                service.resolve_library_access(
                    _DB([(library, admin_membership, organization)]),
                    user=admin,
                    library_slug=library.slug,
                    action="insert",
                )
            )

    platform_user = _user(superuser=True)
    platform_membership = _membership(organization, platform_user, role="member")
    with patch.object(service, "has_permission", return_value=False):
        with pytest.raises(service.OrganizationAuthorizationError):
            asyncio.run(
                service.resolve_library_access(
                    _DB([(library, platform_membership, organization)]),
                    user=platform_user,
                    library_slug=library.slug,
                    action="read",
                )
            )

    service.bind_credential_organization(admin, uuid.uuid4())
    with pytest.raises(service.OrganizationAuthorizationError):
        asyncio.run(
            service.resolve_library_access(
                _DB([(library, admin_membership, organization)]),
                user=admin,
                library_slug=library.slug,
                action="read",
            )
        )


def test_enabled_library_management_accepts_org_admin_or_explicit_admin_only(monkeypatch):
    from app.services import organization_authorization as service

    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    organization = _organization()
    library = _library(organization)

    organization_admin = _user()
    admin_membership = _membership(
        organization,
        organization_admin,
        role="organization_admin",
    )
    with patch.object(service, "has_permission", return_value=False):
        access = asyncio.run(
            service.resolve_loaded_library_management(
                _DB([(admin_membership, organization)]),
                user=organization_admin,
                library=library,
            )
        )
    assert access.role == "organization_admin"

    library_admin = _user()
    member_membership = _membership(organization, library_admin)
    with patch.object(service, "has_permission", return_value=True) as permission:
        access = asyncio.run(
            service.resolve_loaded_library_management(
                _DB([(member_membership, organization)]),
                user=library_admin,
                library=library,
            )
        )
    assert access.role == "member"
    permission.assert_called_once_with(str(library_admin.id), library.slug, "admin")

    platform_superuser = _user(superuser=True)
    platform_membership = _membership(organization, platform_superuser)
    with (
        patch.object(service, "has_permission", return_value=False),
        pytest.raises(service.OrganizationAuthorizationError),
    ):
        asyncio.run(
            service.resolve_loaded_library_management(
                _DB([(platform_membership, organization)]),
                user=platform_superuser,
                library=library,
            )
        )


def test_api_key_auth_binds_scope_and_rejects_disabled_membership(monkeypatch):
    from app.auth.api_key import APIKeyStrategy
    from app.services.organization_authorization import credential_organization_scope

    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    organization = _organization()
    user = _user()
    key = ApiKey(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        name="integration",
        key_prefix="vk_scope123",
        key_hash="hash",
        created_at=NOW,
    )
    manager = SimpleNamespace(get=AsyncMock(return_value=user))
    db = _DB([key], [uuid.uuid4()], [])
    with patch("app.auth.api_key.verify_api_key", return_value=True):
        authenticated = asyncio.run(APIKeyStrategy(db).read_token("vk_scope123-token", manager))
    assert authenticated is user
    assert credential_organization_scope(user).organization_id == organization.id
    assert db.commit_count == 1

    denied_db = _DB([key], [])
    with patch("app.auth.api_key.verify_api_key", return_value=True):
        denied = asyncio.run(
            APIKeyStrategy(denied_db).read_token("vk_scope123-token", manager)
        )
    assert denied is None
    assert denied_db.commit_count == 0


def test_account_create_is_atomic_and_audit_contains_no_credentials(monkeypatch):
    from app.services import organization_accounts as service

    organization = _organization()
    actor = _user()
    actor_membership = _membership(
        organization,
        actor,
        role="organization_admin",
    )
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    db = _DB([organization], [actor_membership], [])
    result = asyncio.run(
        service.create_organization_account(
            db,
            service.OrganizationAccountCreateCommand(
                organization_id=organization.id,
                actor_user_id=actor.id,
                email=" New.User@example.com ",
                username="new-user",
                display_name="New User",
                role="member",
            ),
            password_hash="argon2-secret-hash",
        )
    )
    assert result.user.email == "new.user@example.com"
    assert result.user.hashed_password == "argon2-secret-hash"
    assert result.user.is_superuser is False
    assert result.membership.organization_id == organization.id
    assert db.commit_count == 0
    target = audit.await_args.args[3]
    assert "email" not in target
    assert "password" not in repr(target).lower()
    assert "hash" not in repr(target).lower()


def test_shared_global_account_password_reset_fails_before_hash_change():
    from app.services import organization_accounts as service

    organization = _organization()
    actor = _user()
    target = _user()
    actor_membership = _membership(organization, actor, role="organization_admin")
    target_membership = _membership(organization, target)
    db = _DB(
        [organization],
        [actor_membership],
        [target_membership],
        [target_membership.id, uuid.uuid4()],
    )
    with pytest.raises(service.OrganizationAccountError) as exc_info:
        asyncio.run(
            service.reset_organization_account_password(
                db,
                organization_id=organization.id,
                actor_user_id=actor.id,
                target_user_id=target.id,
                password_hash="new-hash",
            )
        )
    assert exc_info.value.code == "organization_account_shared"
    assert target.hashed_password == "hash"


def test_permission_grant_compensates_when_audit_fails(monkeypatch):
    from app.services import organization_permissions as service

    organization = _organization()
    library = _library(organization)
    actor = _user()
    target = _user()
    actor_membership = _membership(organization, actor, role="organization_admin")
    target_membership = _membership(organization, target)
    db = _DB([organization], [actor_membership, target_membership], [library])
    grant = MagicMock(return_value=[(str(target.id), f"library:{library.slug}", "read")])
    revoke = MagicMock(return_value=1)
    monkeypatch.setattr(service.casbin_service, "grant", grant)
    monkeypatch.setattr(service.casbin_service, "revoke", revoke)
    monkeypatch.setattr(
        service.audit_log,
        "record",
        AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(
            service.grant_organization_permissions(
                db,
                organization_id=organization.id,
                actor_user_id=actor.id,
                target_user_id=target.id,
                library_slug=library.slug,
                actions=["read"],
            )
        )
    revoke.assert_called_once_with(str(target.id), library.slug, ("read",))


def test_platform_permission_grant_creates_active_membership(monkeypatch):
    from app.services import organization_permissions as service

    organization = _organization()
    library = _library(organization)
    actor = _user(superuser=True)
    target = _user()
    db = _DB([organization], [])
    db.add = MagicMock()
    grant = MagicMock(
        return_value=[
            (str(target.id), f"library:{library.slug}", action)
            for action in ("read", "insert", "delete", "admin")
        ]
    )
    monkeypatch.setattr(service.casbin_service, "grant", grant)
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())

    result = asyncio.run(
        service.grant_platform_library_permissions(
            db,
            actor_user_id=actor.id,
            target_user_id=target.id,
            library=library,
            actions=["read", "insert", "delete", "admin"],
        )
    )

    membership = db.add.call_args.args[0]
    assert membership.organization_id == organization.id
    assert membership.user_id == target.id
    assert membership.role == "member"
    assert membership.status == "active"
    assert result.added == ("read", "insert", "delete", "admin")
    grant.assert_called_once_with(
        str(target.id),
        library.slug,
        ("read", "insert", "delete", "admin"),
    )


def test_platform_permission_grant_reactivates_disabled_membership(monkeypatch):
    from app.services import organization_permissions as service

    organization = _organization()
    library = _library(organization)
    actor = _user(superuser=True)
    target = _user()
    membership = _membership(organization, target, status="disabled")
    db = _DB([organization], [membership])
    db.add = MagicMock()
    monkeypatch.setattr(service.casbin_service, "grant", MagicMock(return_value=[]))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())

    asyncio.run(
        service.grant_platform_library_permissions(
            db,
            actor_user_id=actor.id,
            target_user_id=target.id,
            library=library,
            actions=["read"],
        )
    )

    assert membership.status == "active"
    assert membership.disabled_at is None
    db.add.assert_not_called()


def test_organization_admin_and_permission_services_reject_cross_organization_scope(
    monkeypatch,
):
    from app.services import organization_authorization as authorization_service
    from app.services import organization_permissions as permission_service

    organization = _organization()
    other_organization = _organization()
    actor = _user(superuser=True)
    target = _user()
    actor_membership = _membership(
        organization,
        actor,
        role="organization_admin",
    )
    other_library = _library(other_organization)

    with pytest.raises(authorization_service.OrganizationAuthorizationError):
        asyncio.run(
            authorization_service.resolve_organization_admin(
                _DB([]),
                organization_id=organization.id,
                user=actor,
            )
        )

    grant = MagicMock()
    monkeypatch.setattr(permission_service.casbin_service, "grant", grant)
    with pytest.raises(permission_service.OrganizationPermissionError) as exc_info:
        asyncio.run(
            permission_service.grant_organization_permissions(
                _DB([organization], [actor_membership]),
                organization_id=organization.id,
                actor_user_id=actor.id,
                target_user_id=target.id,
                library_slug=other_library.slug,
                actions=["read"],
            )
        )
    assert exc_info.value.code == "organization_permission_target_not_found"
    grant.assert_not_called()

    target_membership = _membership(organization, target)
    with pytest.raises(permission_service.OrganizationPermissionError) as exc_info:
        asyncio.run(
            permission_service.grant_organization_permissions(
                _DB(
                    [organization],
                    [actor_membership, target_membership],
                    [],
                ),
                organization_id=organization.id,
                actor_user_id=actor.id,
                target_user_id=target.id,
                library_slug=other_library.slug,
                actions=["read"],
            )
        )
    assert exc_info.value.code == "organization_permission_library_not_found"
    grant.assert_not_called()
