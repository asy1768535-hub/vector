from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app.config import Settings, settings, validate_personal_library_scopes_startup
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.models.user_library_scope import UserLibraryScope, UserLibraryScopeItem
from app.services import personal_library_scopes as service
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityFingerprint,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)


NOW = datetime(2026, 7, 22, 23, 45, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.commit = AsyncMock()

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(), slug="org", name="Org", deployment_profile="hosted",
        status="active", created_at=NOW, updated_at=NOW,
    )


def _user() -> User:
    return User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", hashed_password="hash",
        is_active=True, is_superuser=False, is_verified=True, created_at=NOW,
    )


def _library(org: Organization, slug: str) -> Library:
    return Library(
        id=uuid.uuid4(), organization_id=org.id, slug=slug, name=slug.title(),
        embedding_model="bge-m3", embedding_dim=1024, qdrant_collection=f"c_{slug}",
        index_state="ready", created_at=NOW,
    )


def _assessment(org: Organization, libraries, *, incompatible=()):
    profiles = tuple(
        LibraryCompatibilityProfile(
            library=library,
            embedding=CompatibilityFingerprint("embedding-v1", "a" * 64, True),
            retrieval=CompatibilityFingerprint("retrieval-v1", "b" * 64, True),
            graph=None,
        )
        for library in libraries
    )
    return CompatibilityAssessment(
        organization_id=org.id, channels=("text",), profiles=profiles,
        compatible=not incompatible, incompatibilities=tuple(incompatible),
    )


def test_0033_orm_migration_and_offline_sql_are_reversible():
    assert UserLibraryScope.__table__.columns.name.type.length == 80
    assert UserLibraryScopeItem.__table__.columns.ordinal.nullable is False
    constraints = {item.name for item in UserLibraryScope.__table__.constraints if isinstance(item, CheckConstraint)}
    assert "ck_user_library_scopes_name_shape" in constraints
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0040"]
    assert script.get_revision("0033").down_revision == "0032"
    assert script.get_revision("0034").down_revision == "0033"
    upgrade = _offline("upgrade", "0032:0033")
    downgrade = _offline("downgrade", "0033:0032")
    assert "create table user_library_scopes" in upgrade
    assert "create table user_library_scope_items" in upgrade
    assert downgrade.index("drop table user_library_scope_items") < downgrade.index("drop table user_library_scopes")
    assert "update sys_" not in upgrade + downgrade
    assert "delete from" not in upgrade + downgrade


def test_feature_defaults_off_and_requires_authorization_and_compatibility():
    assert settings.personal_library_scopes_enabled is False
    validate_personal_library_scopes_startup(Settings())
    with pytest.raises(RuntimeError, match="Organization authorization"):
        validate_personal_library_scopes_startup(Settings(personal_library_scopes_enabled=True))
    validate_personal_library_scopes_startup(
        Settings(
            personal_library_scopes_enabled=True,
            organization_authorization_enabled=True,
            cross_library_compatibility_enabled=True,
        )
    )


def test_name_normalization_is_nfkc_casefolded_and_bounded():
    assert service.normalize_scope_name("  Ｌｅｇａｌ   Team ") == ("Legal Team", "legal team")
    with pytest.raises(service.PersonalLibraryScopeError):
        service.normalize_scope_name("   ")


def test_create_named_scope_validates_exact_selection_and_never_commits(monkeypatch):
    org, user = _organization(), _user()
    libraries = (_library(org, "legal"), _library(org, "contracts"))
    monkeypatch.setattr(service, "_require_membership", AsyncMock())
    monkeypatch.setattr(service, "_lock_user", AsyncMock())
    assess = AsyncMock(return_value=_assessment(org, libraries))
    monkeypatch.setattr(service, "assess_library_compatibility", assess)
    db = _DB(_Result(scalar=0), _Result(scalar=None))
    scope = asyncio.run(
        service.create_named_scope(
            db, user=user, organization_id=org.id, name=" My Scope ",
            library_slugs=("legal", "contracts"),
        )
    )
    assert scope.name == "My Scope" and scope.normalized_name == "my scope"
    assert [item.library_id for item in db.added if isinstance(item, UserLibraryScopeItem)] == [
        libraries[0].id, libraries[1].id,
    ]
    assert assess.await_args.kwargs["channels"] == ("text",)
    db.commit.assert_not_awaited()


def test_restore_hides_revoked_details_and_rechecks_survivors_once(monkeypatch):
    org, user = _organization(), _user()
    first, second, revoked = (
        _library(org, "legal"), _library(org, "contracts"), _library(org, "private")
    )
    scope = UserLibraryScope(
        id=uuid.uuid4(), organization_id=org.id, user_id=user.id, scope_kind="named",
        name="Scope", normalized_name="scope", created_at=NOW, updated_at=NOW,
    )
    monkeypatch.setattr(service, "_require_membership", AsyncMock())
    monkeypatch.setattr(service, "list_accessible_libraries", AsyncMock(return_value=(first, second)))
    assessments = AsyncMock(
        side_effect=[
            _assessment(
                org, (first, second),
                incompatible=(LibraryIncompatibility(second.slug, ("library_index_unready",)),),
            ),
            _assessment(org, (first,)),
        ]
    )
    monkeypatch.setattr(service, "assess_library_compatibility", assessments)
    db = _DB(_Result(rows=(first.id, second.id, revoked.id)))
    resolved = asyncio.run(service._resolve_scope(db, user=user, scope=scope))
    assert [library.slug for library in resolved.libraries] == ["legal"]
    removed = {item.library_id: item.reason_codes for item in resolved.removed}
    assert removed[revoked.id] == ("unavailable_or_forbidden",)
    assert removed[second.id] == ("library_index_unready",)
    assert assessments.await_count == 2
    assert "private" not in repr(resolved.removed).lower()
    db.commit.assert_not_awaited()
