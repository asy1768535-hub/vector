from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0030_v08_organization_identity.py"
NOW = datetime(2026, 7, 22, 18, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, *rows, objects=None):
        self.rows = list(rows)
        self.objects = dict(objects or {})
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.rows, "unexpected database query"
        return _Result(self.rows.pop(0))

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        self.flush_count += 1


def _constraint_sql(table, name: str) -> str:
    constraint = next(item for item in table.constraints if item.name == name)
    assert isinstance(constraint, CheckConstraint)
    return " ".join(str(constraint.sqltext).lower().split())


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _foreign_keys(table) -> set[tuple[str | None, str, str | None]]:
    return {
        (foreign_key.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.foreign_keys
    }


def _indexes(table) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (index.name, tuple(column.name for column in index.columns))
        for index in table.indexes
    }


def _user(*, active=True):
    from app.models.user import User

    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=active,
        is_superuser=False,
        is_verified=True,
        deleted_at=None if active else NOW,
    )


def _organization(*, status="active"):
    from app.models.organization import Organization

    return Organization(
        id=uuid.uuid4(),
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Organization",
        deployment_profile="private",
        status=status,
    )


def _membership(organization, user, *, role="member", status="active"):
    from app.models.organization_membership import OrganizationMembership

    return OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=role,
        status=status,
        disabled_at=NOW if status == "disabled" else None,
    )


def test_0030_orm_migration_and_default_identity_match():
    from app.models import Library, Organization, OrganizationMembership
    from app.models.organization import (
        DEFAULT_ORGANIZATION_ID,
        DEFAULT_ORGANIZATION_SLUG,
    )

    assert Organization.__tablename__ == "sys_organizations"
    assert set(Organization.__table__.columns.keys()) == {
        "id",
        "slug",
        "name",
        "deployment_profile",
        "status",
        "created_by_user_id",
        "created_at",
        "updated_at",
    }
    assert set(OrganizationMembership.__table__.columns.keys()) == {
        "id",
        "organization_id",
        "user_id",
        "role",
        "status",
        "created_by_user_id",
        "disabled_at",
        "created_at",
        "updated_at",
    }
    organization_columns = Organization.__table__.columns
    assert organization_columns.slug.type.length == 64
    assert organization_columns.name.type.length == 160
    assert organization_columns.deployment_profile.type.length == 16
    assert organization_columns.status.type.length == 16
    assert str(organization_columns.deployment_profile.server_default.arg) == "private"
    assert str(organization_columns.status.server_default.arg) == "active"
    assert _foreign_keys(Organization.__table__) == {
        ("fk_sys_organizations_created_by", "sys_users.id", "SET NULL")
    }
    assert _indexes(Organization.__table__) == {
        ("ix_sys_organizations_status_created", ("status", "created_at"))
    }
    membership_columns = OrganizationMembership.__table__.columns
    assert membership_columns.role.type.length == 32
    assert membership_columns.status.type.length == 16
    assert str(membership_columns.status.server_default.arg) == "active"
    assert _foreign_keys(OrganizationMembership.__table__) == {
        (
            "fk_sys_organization_memberships_organization",
            "sys_organizations.id",
            "RESTRICT",
        ),
        ("fk_sys_organization_memberships_user", "sys_users.id", "RESTRICT"),
        ("fk_sys_organization_memberships_created_by", "sys_users.id", "SET NULL"),
    }
    assert _indexes(OrganizationMembership.__table__) == {
        (
            "ix_sys_organization_memberships_user_status",
            ("user_id", "status"),
        ),
        (
            "ix_sys_organization_memberships_org_status_role",
            ("organization_id", "status", "role"),
        ),
    }
    assert "organization_admin" in _constraint_sql(
        OrganizationMembership.__table__,
        "ck_sys_organization_memberships_role",
    )
    assert "disabled_at" in _constraint_sql(
        OrganizationMembership.__table__,
        "ck_sys_organization_memberships_disabled_shape",
    )
    unique = next(
        item
        for item in OrganizationMembership.__table__.constraints
        if isinstance(item, UniqueConstraint)
    )
    assert tuple(column.name for column in unique.columns) == (
        "organization_id",
        "user_id",
    )
    assert Library.__table__.columns.organization_id.nullable is False
    library_fk = next(
        item
        for item in Library.__table__.foreign_keys
        if item.parent.name == "organization_id"
    )
    assert library_fk.target_fullname == "sys_organizations.id"
    assert library_fk.ondelete == "RESTRICT"
    assert library_fk.name == "fk_sys_libraries_organization"
    assert str(Library.__table__.columns.organization_id.server_default.arg) == str(
        DEFAULT_ORGANIZATION_ID
    )
    assert (
        "ix_sys_libraries_organization_id",
        ("organization_id",),
    ) in _indexes(Library.__table__)

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0030"' in migration
    assert 'down_revision: Union[str, None] = "0029"' in migration
    assert str(DEFAULT_ORGANIZATION_ID) in migration
    assert f'DEFAULT_ORGANIZATION_SLUG = "{DEFAULT_ORGANIZATION_SLUG}"' in migration
    assert "md5('default-organization-membership:'" in migration
    assert "UPDATE sys_libraries" in migration
    assert "UPDATE documents" not in migration
    assert "UPDATE casbin_rule" not in migration
    assert "DELETE FROM" not in migration


def test_0030_remains_linear_and_local_offline_sql_is_exactly_reversible():
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    migration = script.get_revision("0030")
    assert migration is not None
    assert migration.down_revision == "0029"
    assert Path(migration.path).resolve() == MIGRATION.resolve()
    next_migration = script.get_revision("0031")
    assert next_migration is not None
    assert next_migration.down_revision == "0030"
    compatibility_migration = script.get_revision("0032")
    assert compatibility_migration is not None
    assert compatibility_migration.down_revision == "0031"
    personal_scope_migration = script.get_revision("0033")
    assert personal_scope_migration is not None
    assert personal_scope_migration.down_revision == "0032"
    taxonomy_migration = script.get_revision("0034")
    assert taxonomy_migration is not None
    assert taxonomy_migration.down_revision == "0033"
    assert script.get_heads() == ["0039"]

    upgrade = _offline("upgrade", "0029:0030")
    downgrade = _offline("downgrade", "0030:0029")
    for fragment in (
        "create table sys_organizations",
        "create table sys_organization_memberships",
        "constraint ck_sys_organizations_slug",
        "constraint ck_sys_organization_memberships_disabled_shape",
        "constraint uq_sys_organization_memberships_org_user",
        "constraint fk_sys_organizations_created_by",
        "constraint fk_sys_organization_memberships_organization",
        "constraint fk_sys_organization_memberships_user",
        "constraint fk_sys_organization_memberships_created_by",
        "create index ix_sys_organizations_status_created",
        "create index ix_sys_organization_memberships_user_status",
        "create index ix_sys_organization_memberships_org_status_role",
        "add constraint fk_sys_libraries_organization",
        "foreign key(organization_id) references sys_organizations (id) on delete restrict",
        "alter column organization_id set not null",
        "create index ix_sys_libraries_organization_id",
        "insert into sys_organization_memberships",
        "update sys_libraries",
    ):
        assert fragment in upgrade
    for fragment in (
        "drop index ix_sys_libraries_organization_id",
        "drop constraint fk_sys_libraries_organization",
        "drop column organization_id",
        "drop table sys_organization_memberships",
        "drop table sys_organizations",
    ):
        assert fragment in downgrade
    for protected_table in (
        "documents",
        "document_revisions",
        "chunks",
        "evidence",
        "entities",
        "knowledge_relations",
        "casbin_rule",
    ):
        assert f"update {protected_table}" not in upgrade
        assert f"delete from {protected_table}" not in upgrade


def test_library_read_adds_compatibility_organization_without_write_input():
    from app.models.library import Library
    from app.models.organization import DEFAULT_ORGANIZATION_ID
    from app.schemas.admin import LibraryCreate, LibraryRead

    library = Library(
        id=uuid.uuid4(),
        slug="compat_lib",
        name="Compatibility",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection="lib_compat_lib",
        graph_extraction_enabled=False,
        external_llm_enabled=False,
        graph_extraction_allowed_security_levels=[],
        knowledge_artifact_auto_enabled=False,
        summary_artifact_enabled=False,
        outline_artifact_enabled=False,
        knowledge_artifact_external_model_enabled=False,
        knowledge_artifact_allowed_security_levels=[],
        revision_retention_enabled=False,
        revision_retention_days=60,
        revision_retention_notice_days=7,
        lifecycle_mode="managed",
        index_state="ready",
        created_at=NOW,
    )
    assert library.organization_id is None
    read = LibraryRead.model_validate(library)
    assert read.organization_id == DEFAULT_ORGANIZATION_ID
    assert "organization_id" not in LibraryCreate.model_fields


def test_commands_are_strict_normalized_and_frozen():
    from app.services.organization_identity import (
        MembershipCreateCommand,
        OrganizationCreateCommand,
        OrganizationIdentityError,
        OrganizationUpdateCommand,
    )

    command = OrganizationCreateCommand(
        slug="  Acme-01  ",
        name="  Acme  ",
        deployment_profile="hosted",
        initial_admin_user_id=uuid.uuid4(),
    )
    assert command.slug == "acme-01"
    assert command.name == "Acme"
    with pytest.raises(FrozenInstanceError):
        command.slug = "changed"  # type: ignore[misc]
    with pytest.raises(OrganizationIdentityError) as exc_info:
        MembershipCreateCommand(
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="owner",
        )
    assert exc_info.value.code == "organization_membership_role_invalid"
    with pytest.raises(OrganizationIdentityError) as exc_info:
        OrganizationUpdateCommand(
            organization_id=uuid.uuid4(), expected_status="active"
        )
    assert exc_info.value.code == "organization_update_empty"
    with pytest.raises(OrganizationIdentityError) as exc_info:
        MembershipCreateCommand(
            organization_id="not-a-uuid",  # type: ignore[arg-type]
            user_id=uuid.uuid4(),
            role="member",
        )
    assert exc_info.value.code == "organization_id_invalid"
    with pytest.raises(OrganizationIdentityError) as exc_info:
        OrganizationCreateCommand(
            slug="valid-org",
            name="Valid",
            deployment_profile="private",
            initial_admin_user_id=uuid.uuid4(),
            actor_user_id="not-a-uuid",  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "organization_actor_invalid"


def test_create_organization_is_atomic_idempotent_and_audit_safe(monkeypatch):
    from app.models.organization import Organization
    from app.models.organization_membership import OrganizationMembership
    from app.services import organization_identity as service

    admin = _user()
    actor_id = uuid.uuid4()
    command = service.OrganizationCreateCommand(
        slug="customer-one",
        name="Customer One",
        deployment_profile="hosted",
        initial_admin_user_id=admin.id,
        actor_user_id=actor_id,
    )
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    db = _DB([], [admin])
    organization = asyncio.run(service.create_organization(db, command))

    assert isinstance(organization, Organization)
    membership = next(
        item for item in db.added if isinstance(item, OrganizationMembership)
    )
    assert membership.organization_id == organization.id
    assert membership.user_id == admin.id
    assert membership.role == "organization_admin"
    assert membership.status == "active"
    assert db.flush_count == 1
    audit.assert_awaited_once()
    target = audit.await_args.args[3]
    assert target == {
        "organization_id": str(organization.id),
        "organization_slug": "customer-one",
        "initial_admin_user_id": str(admin.id),
        "deployment_profile": "hosted",
    }
    assert not {"password", "hash", "email", "content", "credential"}.intersection(
        target
    )

    existing_membership = _membership(
        organization, admin, role="organization_admin"
    )
    replay_db = _DB([organization], [existing_membership])
    replay = asyncio.run(service.create_organization(replay_db, command))
    assert replay is organization
    assert replay_db.added == []
    assert audit.await_count == 1


def test_organization_and_membership_reads_are_bounded_and_filtered():
    from app.models.organization import Organization
    from app.services import organization_identity as service

    organization = _organization()
    user = _user()
    membership = _membership(organization, user)

    loaded = asyncio.run(
        service.get_organization(
            _DB(objects={(Organization, organization.id): organization}),
            organization.id,
        )
    )
    assert loaded is organization

    organization_db = _DB([organization])
    assert asyncio.run(
        service.list_organizations(organization_db, status="active", limit=25)
    ) == (organization,)
    organization_sql = str(organization_db.statements[0])
    assert "sys_organizations.status" in organization_sql
    assert "ORDER BY sys_organizations.slug" in organization_sql

    membership_db = _DB([membership])
    assert asyncio.run(
        service.list_user_memberships(
            membership_db,
            user_id=user.id,
            active_only=True,
            limit=25,
        )
    ) == (membership,)
    membership_sql = str(membership_db.statements[0])
    assert "sys_organization_memberships.user_id" in membership_sql
    assert "sys_organization_memberships.status" in membership_sql

    for call in (
        lambda: service.list_organizations(_DB(), limit=0),
        lambda: service.list_user_memberships(
            _DB(), user_id=user.id, active_only=True, limit=501
        ),
    ):
        with pytest.raises(service.OrganizationIdentityError) as exc_info:
            asyncio.run(call())
        assert exc_info.value.code == "organization_limit_invalid"


def test_add_membership_locks_org_then_user_and_replays_exact_identity(monkeypatch):
    from app.models.organization import Organization
    from app.models.user import User
    from app.services import organization_identity as service

    organization = _organization()
    user = _user()
    command = service.MembershipCreateCommand(
        organization_id=organization.id,
        user_id=user.id,
        role="member",
    )
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB([organization], [user], [])
    membership = asyncio.run(service.add_organization_membership(db, command))
    models = [statement.column_descriptions[0].get("entity") for statement in db.statements]
    assert models[:2] == [Organization, User]
    assert membership.organization_id == organization.id
    assert membership.user_id == user.id

    replay_db = _DB([organization], [user], [membership])
    replay = asyncio.run(service.add_organization_membership(replay_db, command))
    assert replay is membership
    assert replay_db.added == []


def test_add_membership_fails_closed_for_suspended_missing_and_conflicting_identity(
    monkeypatch,
):
    from app.services import organization_identity as service

    user = _user()
    suspended = _organization(status="suspended")
    suspended_command = service.MembershipCreateCommand(
        organization_id=suspended.id,
        user_id=user.id,
        role="member",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.add_organization_membership(_DB([suspended]), suspended_command)
        )
    assert exc_info.value.code == "organization_suspended"

    organization = _organization()
    missing_user_command = service.MembershipCreateCommand(
        organization_id=organization.id,
        user_id=uuid.uuid4(),
        role="member",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.add_organization_membership(
                _DB([organization], []), missing_user_command
            )
        )
    assert exc_info.value.code == "organization_user_unavailable"

    existing = _membership(
        organization,
        user,
        role="organization_admin",
    )
    conflicting_command = service.MembershipCreateCommand(
        organization_id=organization.id,
        user_id=user.id,
        role="member",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.add_organization_membership(
                _DB([organization], [user], [existing]),
                conflicting_command,
            )
        )
    assert exc_info.value.code == "organization_membership_conflict"


def test_membership_change_rejects_stale_state_and_final_admin(monkeypatch):
    from app.services import organization_identity as service

    organization = _organization()
    user = _user()
    membership = _membership(
        organization, user, role="organization_admin", status="active"
    )
    stale = service.MembershipChangeCommand(
        membership_id=membership.id,
        expected_role="member",
        expected_status="active",
        role="member",
        status="disabled",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.change_organization_membership(
                _DB(
                    [SimpleNamespace(id=membership.id, organization_id=organization.id)],
                    [organization],
                    [membership],
                ),
                stale,
            )
        )
    assert exc_info.value.code == "organization_membership_state_changed"

    command = service.MembershipChangeCommand(
        membership_id=membership.id,
        expected_role="organization_admin",
        expected_status="active",
        role="member",
        status="disabled",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.change_organization_membership(
                _DB(
                    [SimpleNamespace(id=membership.id, organization_id=organization.id)],
                    [organization],
                    [membership],
                    [membership.id],
                ),
                command,
            )
        )
    assert exc_info.value.code == "organization_last_admin"

    other_admin_id = uuid.uuid4()
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    changed = asyncio.run(
        service.change_organization_membership(
            _DB(
                [SimpleNamespace(id=membership.id, organization_id=organization.id)],
                [organization],
                [membership],
                [membership.id, other_admin_id],
            ),
            command,
            at=NOW,
        )
    )
    assert changed.role == "member"
    assert changed.status == "disabled"
    assert changed.disabled_at == NOW
    target = audit.await_args.args[3]
    assert target["organization_id"] == str(organization.id)
    assert target["status"] == "disabled"


def test_update_organization_uses_expected_state_and_bounded_audit(monkeypatch):
    from app.services import organization_identity as service

    organization = _organization()
    command = service.OrganizationUpdateCommand(
        organization_id=organization.id,
        expected_status="active",
        name="Renamed",
        status="suspended",
        actor_user_id=uuid.uuid4(),
    )
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    updated = asyncio.run(
        service.update_organization(_DB([organization]), command)
    )
    assert updated.name == "Renamed"
    assert updated.status == "suspended"
    assert audit.await_args.args[3] == {
        "organization_id": str(organization.id),
        "organization_slug": organization.slug,
        "name_changed": True,
        "status": "suspended",
    }
    assert "Renamed" not in repr(audit.await_args.args[3])

    stale_command = service.OrganizationUpdateCommand(
        organization_id=organization.id,
        expected_status="active",
        name="Another",
    )
    with pytest.raises(service.OrganizationIdentityError) as exc_info:
        asyncio.run(
            service.update_organization(_DB([organization]), stale_command)
        )
    assert exc_info.value.code == "organization_state_changed"


def test_foundation_does_not_mount_or_change_authorization_routes():
    service = Path("app/services/organization_identity.py").read_text(encoding="utf-8")
    assert "has_permission" not in service
    assert "ApiKey" not in service
    assert "qdrant" not in service.lower()
    assert "object_storage" not in service
