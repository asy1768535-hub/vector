from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.models.api_key import ApiKey
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID, Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    bind_credential_organization,
    resolve_library_access,
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


def test_0031_backfills_and_downgrades_without_key_loss(monkeypatch):
    name, _ = _create_database(monkeypatch, "vkt_v08_org_auth_migration")
    user_id = uuid.uuid4()
    key_id = uuid.uuid4()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0030")
        asyncio.run(
            _execute(
                name,
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES (
                    '{user_id}', 'key-owner@example.com', 'hash', true, false, true
                );
                INSERT INTO sys_api_keys (
                    id, user_id, name, key_prefix, key_hash
                ) VALUES (
                    '{key_id}', '{user_id}', 'legacy', 'vk_legacy', 'hash'
                )
                """,
            )
        )
        command.upgrade(config, "0031")
        assert asyncio.run(
            _execute(
                name,
                f"SELECT organization_id FROM sys_api_keys WHERE id = '{key_id}'",
            )
        ) == [(DEFAULT_ORGANIZATION_ID,)]

        command.downgrade(config, "0030")
        assert asyncio.run(
            _execute(
                name,
                f"SELECT id, user_id, name FROM sys_api_keys WHERE id = '{key_id}'",
            )
        ) == [(key_id, user_id, "legacy")]
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'sys_api_keys'
                  AND column_name = 'organization_id'
                """,
            )
        )[0][0] == 0
    finally:
        _drop_database(name)


def test_enabled_access_rechecks_membership_and_key_scope(monkeypatch):
    name, url = _create_database(monkeypatch, "vkt_v08_org_auth_access")
    try:
        command.upgrade(Config("alembic.ini"), "head")
        monkeypatch.setattr(settings, "organization_authorization_enabled", True)

        async def scenario():
            engine = create_async_engine(url)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                organization = Organization(
                    id=uuid.uuid4(),
                    slug=f"org-{uuid.uuid4().hex[:8]}",
                    name="Customer",
                    deployment_profile="hosted",
                    status="active",
                )
                user = User(
                    id=uuid.uuid4(),
                    email=f"{uuid.uuid4().hex}@example.com",
                    hashed_password="hash",
                    is_active=True,
                    is_superuser=True,
                    is_verified=True,
                )
                membership = OrganizationMembership(
                    id=uuid.uuid4(),
                    organization_id=organization.id,
                    user_id=user.id,
                    role="organization_admin",
                    status="active",
                )
                library = Library(
                    id=uuid.uuid4(),
                    organization_id=organization.id,
                    slug=f"lib_{uuid.uuid4().hex[:8]}",
                    name="Library",
                    embedding_model="bge-m3",
                    embedding_dim=1024,
                    qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
                )
                key = ApiKey(
                    id=uuid.uuid4(),
                    organization_id=organization.id,
                    user_id=user.id,
                    name="key",
                    key_prefix="vk_pg_key",
                    key_hash="hash",
                )
                async with Session() as db:
                    db.add_all((organization, user))
                    await db.flush()
                    db.add_all((membership, library, key))
                    await db.commit()
                async with Session() as db:
                    access = await resolve_library_access(
                        db,
                        user=user,
                        library_slug=library.slug,
                        action="read",
                    )
                    assert access.organization_id == organization.id
                bind_credential_organization(user, uuid.uuid4())
                async with Session() as db:
                    with pytest.raises(OrganizationAuthorizationError):
                        await resolve_library_access(
                            db,
                            user=user,
                            library_slug=library.slug,
                            action="read",
                        )
                del user.__dict__["_organization_credential_scope"]
                async with Session() as db:
                    row = await db.get(OrganizationMembership, membership.id)
                    row.status = "disabled"
                    row.disabled_at = datetime.now(timezone.utc)
                    await db.commit()
                async with Session() as db:
                    with pytest.raises(OrganizationAuthorizationError):
                        await resolve_library_access(
                            db,
                            user=user,
                            library_slug=library.slug,
                            action="read",
                        )
            finally:
                await engine.dispose()

        asyncio.run(scenario())
    finally:
        _drop_database(name)
