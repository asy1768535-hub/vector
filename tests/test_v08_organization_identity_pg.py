from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.services.organization_identity import (
    MembershipChangeCommand,
    MembershipCreateCommand,
    OrganizationIdentityError,
    add_organization_membership,
    change_organization_membership,
)
from tests.v08_pg_support import execute_sql


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None


def _admin_url() -> URL:
    assert _ADMIN_URL is not None
    return _ADMIN_URL


async def _admin(sql: str) -> None:
    import asyncpg

    url = _admin_url()
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


async def _execute(name: str, sql: str):
    return await execute_sql(_database_url(name), sql)


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _create_database(monkeypatch, prefix: str) -> tuple[str, URL]:
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    return name, _database_url(name)


def _drop_database(name: str) -> None:
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_0030_backfills_and_downgrades_without_casbin_drift(monkeypatch):
    name, _ = _create_database(monkeypatch, "vkt_v08_org_migration")
    superuser_id = uuid.uuid4()
    member_id = uuid.uuid4()
    disabled_id = uuid.uuid4()
    library_id = uuid.uuid4()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0029")
        for sql in (
            f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified,
                    username, display_name, deleted_at
                ) VALUES
                    ('{superuser_id}', 'admin@example.com', 'hash', true, true, true,
                     'admin', 'Admin', NULL),
                    ('{member_id}', 'member@example.com', 'hash', true, false, true,
                     'member', 'Member', NULL),
                    ('{disabled_id}', 'disabled@example.com', 'hash', false, false, true,
                     'disabled', 'Disabled', now())
                """,
            f"""
                INSERT INTO sys_libraries (
                    id, slug, name, embedding_model, embedding_dim, qdrant_collection
                ) VALUES (
                    '{library_id}', 'legacy_org_lib', 'Legacy', 'bge-m3', 1024,
                    'lib_legacy_org_lib'
                )
                """,
            f"""
                INSERT INTO casbin_rule (ptype, v0, v1, v2)
                VALUES ('p', '{member_id}', 'library:legacy_org_lib', 'read')
                """,
        ):
            asyncio.run(_execute(name, sql))
        casbin_before = asyncio.run(
            _execute(
                name,
                "SELECT ptype, v0, v1, v2 FROM casbin_rule ORDER BY id",
            )
        )

        command.upgrade(config, "0030")
        organization = asyncio.run(
            _execute(
                name,
                "SELECT id, slug, deployment_profile, status FROM sys_organizations",
            )
        )
        assert organization == [
            (DEFAULT_ORGANIZATION_ID, "default", "private", "active")
        ]
        memberships = asyncio.run(
            _execute(
                name,
                """
                SELECT user_id, role, status, disabled_at IS NOT NULL
                FROM sys_organization_memberships
                ORDER BY user_id
                """,
            )
        )
        by_user = {row[0]: row[1:] for row in memberships}
        assert by_user[superuser_id] == ("organization_admin", "active", False)
        assert by_user[member_id] == ("member", "active", False)
        assert by_user[disabled_id] == ("member", "disabled", True)
        assert asyncio.run(
            _execute(
                name,
                "SELECT organization_id FROM sys_libraries",
            )
        ) == [(DEFAULT_ORGANIZATION_ID,)]
        assert asyncio.run(
            _execute(
                name,
                "SELECT ptype, v0, v1, v2 FROM casbin_rule ORDER BY id",
            )
        ) == casbin_before

        command.downgrade(config, "0029")
        assert asyncio.run(
            _execute(
                name,
                "SELECT to_regclass('public.sys_organizations')",
            )
        )[0][0] is None
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'sys_libraries'
                  AND column_name = 'organization_id'
                """,
            )
        )[0][0] == 0
        assert asyncio.run(
            _execute(
                name,
                "SELECT ptype, v0, v1, v2 FROM casbin_rule ORDER BY id",
            )
        ) == casbin_before
    finally:
        _drop_database(name)


async def _seed_users(Session, count: int) -> tuple[User, ...]:
    users = tuple(
        User(
            id=uuid.uuid4(),
            email=f"org-{uuid.uuid4().hex}@example.com",
            hashed_password="hash",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        for _ in range(count)
    )
    async with Session() as db:
        db.add_all(users)
        await db.commit()
    return users


async def _add_membership(Session, command_value):
    async with Session() as db:
        async with db.begin():
            return await add_organization_membership(db, command_value)


async def _change_membership(Session, command_value):
    try:
        async with Session() as db:
            async with db.begin():
                return await change_organization_membership(db, command_value)
    except OrganizationIdentityError as exc:
        return exc


def test_concurrent_membership_and_final_admin_fences(monkeypatch):
    name, url = _create_database(monkeypatch, "vkt_v08_org_concurrency")
    try:
        command.upgrade(Config("alembic.ini"), "0030")
        async def scenario():
            engine = create_async_engine(url)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                member, admin_a, admin_b = await _seed_users(Session, 3)
                membership_command = MembershipCreateCommand(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    user_id=member.id,
                    role="member",
                )
                results = await asyncio.gather(
                    _add_membership(Session, membership_command),
                    _add_membership(Session, membership_command),
                )
                assert results[0].id == results[1].id

                admin_memberships = await asyncio.gather(
                    _add_membership(
                        Session,
                        MembershipCreateCommand(
                            organization_id=DEFAULT_ORGANIZATION_ID,
                            user_id=admin_a.id,
                            role="organization_admin",
                        ),
                    ),
                    _add_membership(
                        Session,
                        MembershipCreateCommand(
                            organization_id=DEFAULT_ORGANIZATION_ID,
                            user_id=admin_b.id,
                            role="organization_admin",
                        ),
                    ),
                )
                commands = tuple(
                    MembershipChangeCommand(
                        membership_id=row.id,
                        expected_role="organization_admin",
                        expected_status="active",
                        role="member",
                        status="disabled",
                    )
                    for row in admin_memberships
                )
                outcomes = await asyncio.gather(
                    _change_membership(Session, commands[0]),
                    _change_membership(Session, commands[1]),
                )
                assert (
                    sum(
                        isinstance(value, OrganizationMembership)
                        for value in outcomes
                    )
                    == 1
                )
                errors = [
                    value
                    for value in outcomes
                    if isinstance(value, OrganizationIdentityError)
                ]
                assert len(errors) == 1
                assert errors[0].code == "organization_last_admin"

                async with Session() as db:
                    active_admin_count = (
                        await db.execute(
                            select(func.count(OrganizationMembership.id)).where(
                                OrganizationMembership.organization_id
                                == DEFAULT_ORGANIZATION_ID,
                                OrganizationMembership.role == "organization_admin",
                                OrganizationMembership.status == "active",
                            )
                        )
                    ).scalar_one()
                assert active_admin_count == 1
            finally:
                await engine.dispose()

        asyncio.run(scenario())
    finally:
        _drop_database(name)
