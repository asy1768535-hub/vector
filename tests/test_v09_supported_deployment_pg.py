from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.deployment_bootstrap import (
    BootstrapCommand,
    DeploymentBootstrapError,
    initialize_supported_deployment,
)
from app.services.organization_rollouts import (
    OrganizationRolloutError,
    list_rollouts,
    require_rollout_enabled,
    set_rollout,
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


def _configure(monkeypatch, name: str) -> URL:
    url = _admin_url().set(database=name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)
    return url


def test_0042_round_trip_bootstrap_and_rollout_contract(monkeypatch) -> None:
    name = f"vkt_v09_deploy_{uuid.uuid4().hex[:8]}"
    url = _configure(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0041")
        assert asyncio.run(
            execute_sql(
                url,
                "SELECT to_regclass('public.organization_capability_rollouts')",
            )
        )[0][0] is None
        command.upgrade(config, "0042")

        async def exercise() -> None:
            engine = create_async_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            bootstrap = BootstrapCommand(
                organization_slug="acme-private",
                organization_name="Acme Private",
                deployment_profile="private",
                admin_email="admin@acme.example",
                admin_username="admin",
                password_hash="hashed-secret",
            )
            async with sessions() as session:
                first = await initialize_supported_deployment(session, bootstrap)
                await session.commit()
                assert first.created is True
            async with sessions() as session:
                second = await initialize_supported_deployment(session, bootstrap)
                await session.commit()
                assert second.created is False
                assert second.organization_id == first.organization_id
                assert second.admin_user_id == first.admin_user_id
            async with sessions() as session:
                states = await list_rollouts(session, first.organization_id)
                assert all(not state.enabled and state.version == 0 for state in states)
                monkeypatch.setattr(settings, "deployment_profile", "private")
                with pytest.raises(OrganizationRolloutError, match="capability_disabled"):
                    await require_rollout_enabled(
                        session,
                        organization_id=first.organization_id,
                        capability="public_api_v1",
                    )
                changed = await set_rollout(
                    session,
                    organization_id=first.organization_id,
                    capability="public_api_v1",
                    enabled=True,
                    expected_version=0,
                    actor_user_id=first.admin_user_id,
                )
                await session.commit()
                assert changed.enabled is True and changed.version == 1
            async with sessions() as session:
                await require_rollout_enabled(
                    session,
                    organization_id=first.organization_id,
                    capability="public_api_v1",
                )

            async def first_write(enabled: bool) -> str:
                async with sessions() as session:
                    try:
                        await set_rollout(
                            session,
                            organization_id=first.organization_id,
                            capability="mcp_adapter",
                            enabled=enabled,
                            expected_version=0,
                            actor_user_id=first.admin_user_id,
                        )
                        await session.commit()
                        return "committed"
                    except OrganizationRolloutError as exc:
                        await session.rollback()
                        return exc.code

            concurrent = await asyncio.gather(first_write(True), first_write(False))
            assert sorted(concurrent) == ["committed", "rollout_version_conflict"]
            async with sessions() as session:
                with pytest.raises(
                    OrganizationRolloutError, match="rollout_version_conflict"
                ):
                    await set_rollout(
                        session,
                        organization_id=first.organization_id,
                        capability="public_api_v1",
                        enabled=False,
                        expected_version=0,
                        actor_user_id=first.admin_user_id,
                    )
                await session.rollback()
            async with sessions() as session:
                with pytest.raises(DeploymentBootstrapError, match="does not match"):
                    await initialize_supported_deployment(
                        session,
                        BootstrapCommand(
                            organization_slug="acme-private",
                            organization_name="Different",
                            deployment_profile="private",
                            admin_email="admin@acme.example",
                            admin_username="admin",
                            password_hash="other-hash",
                        ),
                    )
                await session.rollback()
            await engine.dispose()

        asyncio.run(exercise())
        command.downgrade(config, "0041")
        assert asyncio.run(
            execute_sql(
                url,
                "SELECT to_regclass('public.organization_capability_rollouts')",
            )
        )[0][0] is None
        command.upgrade(config, "0042")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
