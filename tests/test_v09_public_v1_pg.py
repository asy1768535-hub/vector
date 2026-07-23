from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import public_v1 as public_api
from app.auth.api_key import generate_api_key
from app.auth.backend import get_jwt_strategy
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.user import User
from app.services import public_v1 as service
from app.services.organization_authorization import (
    bind_credential_organization,
    credential_organization_scope,
)
from app.services.public_v1_contracts import PublicAPIError
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


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def test_saved_scope_owner_and_api_key_organization_are_fail_closed(
    monkeypatch,
):
    name = f"vkt_v09_public_{uuid.uuid4().hex[:8]}"
    owner_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    library_id, owner_scope_id, other_scope_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    other_organization_id = uuid.uuid4()
    plain_key, key_prefix, key_hash = generate_api_key()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "head")
        asyncio.run(
            execute_sql(
                _database_url(name),
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES
                    ('{owner_id}', 'owner@example.com', 'hash', true, false, true),
                    ('{other_user_id}', 'other@example.com', 'hash', true, false, true);

                INSERT INTO sys_organizations (
                    id, slug, name, deployment_profile, status
                ) VALUES (
                    '{other_organization_id}', 'other-org', 'Other Org', 'hosted', 'active'
                );

                INSERT INTO sys_organization_memberships (
                    id, organization_id, user_id, role, status
                ) VALUES (
                    '{uuid.uuid4()}', '{DEFAULT_ORGANIZATION_ID}', '{owner_id}',
                    'member', 'active'
                );

                INSERT INTO sys_api_keys (
                    id, organization_id, user_id, name, key_prefix, key_hash
                ) VALUES (
                    '{uuid.uuid4()}', '{DEFAULT_ORGANIZATION_ID}', '{owner_id}',
                    'public-v1-test', '{key_prefix}', '{key_hash}'
                );

                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    '{library_id}', '{DEFAULT_ORGANIZATION_ID}', 'alpha', 'Alpha',
                    'bge-m3', 1024, 'cosine', 1000, 120, 'public_alpha'
                );

                INSERT INTO user_library_scopes (
                    id, organization_id, user_id, scope_kind, name, normalized_name
                ) VALUES
                    ('{owner_scope_id}', '{DEFAULT_ORGANIZATION_ID}', '{owner_id}',
                     'named', 'Owner Scope', 'owner scope'),
                    ('{other_scope_id}', '{DEFAULT_ORGANIZATION_ID}', '{other_user_id}',
                     'named', 'Other Scope', 'other scope');

                INSERT INTO user_library_scope_items (id, scope_id, library_id, ordinal)
                VALUES ('{uuid.uuid4()}', '{owner_scope_id}', '{library_id}', 0);
                """,
            )
        )

        async def exercise() -> None:
            engine = create_async_engine(_database_url(name))
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with session_factory() as db:
                    owner = await db.get(User, owner_id)
                    library = await db.get(Library, library_id)
                    assert owner is not None and library is not None
                    resolved = SimpleNamespace(removed=(), libraries=(library,))
                    resolver = AsyncMock(return_value=resolved)
                    monkeypatch.setattr(service, "resolve_named_scope", resolver)

                    organization_id, slugs = await service._saved_scope_selection(
                        db,
                        user=owner,
                        scope_id=owner_scope_id,
                    )
                    assert organization_id == DEFAULT_ORGANIZATION_ID
                    assert slugs == ("alpha",)

                    with pytest.raises(PublicAPIError) as hidden:
                        await service._saved_scope_selection(
                            db,
                            user=owner,
                            scope_id=other_scope_id,
                        )
                    assert hidden.value.code == "resource_not_found"

                    bind_credential_organization(owner, other_organization_id)
                    with pytest.raises(PublicAPIError) as forbidden:
                        await service._saved_scope_selection(
                            db,
                            user=owner,
                            scope_id=owner_scope_id,
                        )
                    assert forbidden.value.code == "scope_forbidden"
                    assert resolver.await_count == 1
            finally:
                await engine.dispose()

        asyncio.run(exercise())

        auth_engine = create_async_engine(_database_url(name), poolclass=NullPool)
        auth_sessions = async_sessionmaker(auth_engine, expire_on_commit=False)

        async def override_db():
            async with auth_sessions() as db:
                yield db

        credential_scopes = []

        async def list_libraries(db, *, user):  # noqa: ARG001
            credential_scopes.append(credential_organization_scope(user))
            return (), False

        cookie_user = User(
            id=owner_id,
            email="owner@example.com",
            hashed_password="hash",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        token = asyncio.run(get_jwt_strategy().write_token(cookie_user))
        app.dependency_overrides[get_db] = override_db
        monkeypatch.setattr(public_api, "list_public_libraries", list_libraries)
        monkeypatch.setattr(settings, "public_api_v1_enabled", True)
        monkeypatch.setattr(settings, "organization_authorization_enabled", True)
        try:
            cookie_response = TestClient(app).get(
                "/api/v1/libraries",
                cookies={settings.cookie_name: token},
            )
            key_response = TestClient(app).get(
                "/api/v1/libraries",
                headers={"Authorization": f"Bearer {plain_key}"},
            )
        finally:
            app.dependency_overrides.clear()
            asyncio.run(auth_engine.dispose())
        assert cookie_response.status_code == key_response.status_code == 200
        cookie_payload = cookie_response.json()
        key_payload = key_response.json()
        cookie_payload.pop("request_id")
        key_payload.pop("request_id")
        assert cookie_payload == key_payload == {
            "contract_version": "public-libraries-v1",
            "libraries": [],
            "truncated": False,
        }
        assert credential_scopes[0] is None
        assert credential_scopes[1].organization_id == DEFAULT_ORGANIZATION_ID
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
