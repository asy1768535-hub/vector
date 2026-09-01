from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, settings
from app.models.public_api_operations import (
    PublicAPIAnswerLease,
    PublicAPIRateWindow,
    PublicAPIRequestRecord,
)
from app.services.public_api_operations import (
    PublicRateLimitExceeded,
    acquire_public_answer_lease,
    admit_public_request,
    record_public_operation,
    release_public_answer_lease,
    run_public_api_operations_maintenance,
)
from app.services.public_api_operations_contracts import (
    PublicAdmissionScope,
    PublicOperationRecord,
)
from tests.v08_pg_support import execute_sql


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="M7 acceptance requires VECTOR_KB_PG_TEST_DSN for disposable PostgreSQL",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


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


def _config(**overrides) -> Settings:
    values = {
        "public_api_operations_enabled": True,
        "public_api_limits_enabled": True,
        "public_api_organization_requests_per_minute": 5,
        "public_api_api_key_requests_per_minute": 3,
        "public_api_organization_concurrent_answers": 2,
        "public_api_api_key_concurrent_answers": 1,
        "public_api_answer_max_seconds": 1,
        "public_api_answer_lease_seconds": 31,
        "public_api_request_retention_days": 1,
        "public_api_cleanup_batch_size": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _request_record(
    request_id: str,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    finished_at: datetime,
) -> PublicOperationRecord:
    return PublicOperationRecord(
        request_id=request_id,
        organization_id=organization_id,
        user_id=user_id,
        endpoint_key="retrieval.search",
        http_method="POST",
        library_ids=(),
        started_at=finished_at - timedelta(milliseconds=5),
        finished_at=finished_at,
        duration_ms=5,
        http_status=200,
        outcome="completed",
    )


def test_0040_round_trip_creates_and_removes_only_operational_tables(monkeypatch):
    name = f"vkt_v09_ops_migration_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "0040")
        rows = asyncio.run(
            execute_sql(
                _database_url(name),
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname='public' AND tablename LIKE 'public_api_%'
                ORDER BY tablename;
                """,
            )
        )
        assert rows == [
            ("public_api_answer_leases",),
            ("public_api_rate_windows",),
            ("public_api_request_records",),
        ]
        command.downgrade(Config("alembic.ini"), "0039")
        assert asyncio.run(
            execute_sql(
                _database_url(name),
                "SELECT to_regclass('public.public_api_request_records'), "
                "to_regclass('public.public_api_rate_windows'), "
                "to_regclass('public.public_api_answer_leases'), "
                "to_regclass('public.sys_users');",
            )
        ) == [(None, None, None, "sys_users")]
        command.upgrade(Config("alembic.ini"), "0040")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_atomic_rate_lease_record_and_cleanup_races(monkeypatch):
    name = f"vkt_v09_ops_race_{uuid.uuid4().hex[:8]}"
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "0040")
        asyncio.run(
            execute_sql(
                _database_url(name),
                f"""
                INSERT INTO sys_users (
                    id, email, hashed_password, is_active, is_superuser, is_verified
                ) VALUES (
                    '{user_id}', 'ops@example.com', 'hash', true, false, true
                );
                INSERT INTO sys_organizations (
                    id, slug, name, deployment_profile, status
                ) VALUES (
                    '{organization_id}', 'ops-org', 'Operations Org', 'hosted', 'active'
                );
                INSERT INTO sys_api_keys (
                    id, organization_id, user_id, name, key_prefix, key_hash
                ) VALUES (
                    '{api_key_id}', '{organization_id}', '{user_id}',
                    'ops-key', 'vk_ops_race', 'hash'
                );
                """,
            )
        )

        async def exercise() -> None:
            engine = create_async_engine(_database_url(name), poolclass=NullPool)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            config = _config()
            key_scope = PublicAdmissionScope(
                organization_ids=(organization_id,),
                api_key_id=api_key_id,
            )
            cookie_scope = PublicAdmissionScope(
                organization_ids=(organization_id,),
            )

            async def rate_attempt(scope: PublicAdmissionScope) -> bool:
                try:
                    await admit_public_request(
                        scope,
                        session_factory=sessions,
                        config=config,
                        at=NOW,
                    )
                    return True
                except PublicRateLimitExceeded:
                    return False

            key_results = await asyncio.gather(
                *(rate_attempt(key_scope) for _ in range(12))
            )
            assert sum(key_results) == 3
            cookie_results = await asyncio.gather(
                *(rate_attempt(cookie_scope) for _ in range(6))
            )
            assert sum(cookie_results) == 2
            async with sessions() as db:
                rows = (
                    await db.execute(
                        select(
                            PublicAPIRateWindow.scope_kind,
                            PublicAPIRateWindow.request_count,
                        ).order_by(PublicAPIRateWindow.scope_kind)
                    )
                ).all()
            assert rows == [("api_key", 3), ("organization", 5)]

            async def lease_attempt(index: int):
                try:
                    return await acquire_public_answer_lease(
                        request_id=f"{index:032x}",
                        endpoint_key="answers.create",
                        organization_id=organization_id,
                        api_key_id=api_key_id,
                        session_factory=sessions,
                        config=config,
                        at=NOW,
                    )
                except PublicRateLimitExceeded:
                    return None

            lease_results = await asyncio.gather(
                *(lease_attempt(index) for index in range(1, 11))
            )
            grants = [result for result in lease_results if result is not None]
            assert len(grants) == 1
            first = grants[0]
            same = await acquire_public_answer_lease(
                request_id=first.request_id,
                endpoint_key="answers.create",
                organization_id=organization_id,
                api_key_id=api_key_id,
                session_factory=sessions,
                config=config,
                at=NOW,
            )
            assert same == first
            assert await release_public_answer_lease(
                first.request_id, session_factory=sessions
            )
            assert not await release_public_answer_lease(
                first.request_id, session_factory=sessions
            )

            crash_grant = await acquire_public_answer_lease(
                request_id="f" * 32,
                endpoint_key="answers.stream",
                organization_id=organization_id,
                api_key_id=api_key_id,
                session_factory=sessions,
                config=config,
                at=NOW,
            )
            assert crash_grant is not None
            recovered = await acquire_public_answer_lease(
                request_id="e" * 32,
                endpoint_key="answers.stream",
                organization_id=organization_id,
                api_key_id=api_key_id,
                session_factory=sessions,
                config=config,
                at=NOW + timedelta(seconds=32),
            )
            assert recovered is not None

            current_record = _request_record(
                "a" * 32,
                user_id=user_id,
                organization_id=organization_id,
                finished_at=NOW,
            )
            assert await record_public_operation(
                current_record, session_factory=sessions, config=config
            )
            assert not await record_public_operation(
                current_record, session_factory=sessions, config=config
            )
            for index in range(4):
                assert await record_public_operation(
                    _request_record(
                        f"{100 + index:032x}",
                        user_id=user_id,
                        organization_id=organization_id,
                        finished_at=NOW - timedelta(days=2),
                    ),
                    session_factory=sessions,
                    config=config,
                )
            async with sessions.begin() as db:
                for index in range(3):
                    db.add(
                        PublicAPIRateWindow(
                            scope_kind="organization",
                            scope_id=uuid.uuid4(),
                            window_started_at=NOW - timedelta(minutes=10 + index),
                            request_count=1,
                        )
                    )
                    db.add(
                        PublicAPIAnswerLease(
                            request_id=f"{200 + index:032x}",
                            organization_id=organization_id,
                            endpoint_key="answers.create",
                            acquired_at=NOW - timedelta(minutes=2),
                            expires_at=NOW - timedelta(minutes=1),
                        )
                    )

            cleanup_at = NOW + timedelta(seconds=100)

            async def maintain():
                async with sessions.begin() as db:
                    return await run_public_api_operations_maintenance(
                        db,
                        at=cleanup_at,
                        batch_size=2,
                        config=config,
                    )

            cleanup_results = await asyncio.gather(maintain(), maintain())
            assert sum(row.request_records_deleted for row in cleanup_results) == 4
            assert sum(row.rate_windows_deleted for row in cleanup_results) == 3
            assert sum(row.answer_leases_deleted for row in cleanup_results) == 4
            async with sessions() as db:
                assert (
                    await db.execute(select(func.count()).select_from(PublicAPIRequestRecord))
                ).scalar_one() == 1
                assert (
                    await db.execute(
                        select(func.count())
                        .select_from(PublicAPIRequestRecord)
                        .where(PublicAPIRequestRecord.request_id == "a" * 32)
                    )
                ).scalar_one() == 1
                assert (
                    await db.execute(
                        select(func.count())
                        .select_from(PublicAPIAnswerLease)
                        .where(PublicAPIAnswerLease.expires_at <= cleanup_at)
                    )
                ).scalar_one() == 0
                assert (
                    await db.execute(
                        select(func.count())
                        .select_from(PublicAPIRateWindow)
                        .where(
                            PublicAPIRateWindow.window_started_at
                            < cleanup_at.replace(second=0, microsecond=0)
                            - timedelta(minutes=2)
                        )
                    )
                ).scalar_one() == 0
            await engine.dispose()

        asyncio.run(exercise())
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
