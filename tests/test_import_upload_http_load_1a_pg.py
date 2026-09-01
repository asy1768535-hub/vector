from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import Header, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db as app_db
from app.api import health as health_api
from app.auth.backend import current_active_user
from app.config import settings
from app.main import create_app
from app.models.organization import DEFAULT_ORGANIZATION_ID
from app.models.user import User


_ADMIN_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _ADMIN_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)

_CONTENT = b"data"
_CONTENT_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_USER_COUNT = 10
_FILES_PER_USER = 50


@dataclass(frozen=True, slots=True)
class PgSample:
    captured_at: float
    connection_count: int
    transaction_count: int
    idle_in_transaction_count: int
    max_transaction_age_seconds: float
    max_idle_transaction_age_seconds: float
    granted_lock_count: int
    waiting_lock_count: int


def _admin_url() -> URL:
    assert _ADMIN_DSN is not None
    return make_url(_ADMIN_DSN).set(drivername="postgresql+asyncpg")


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


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


def _configure_database(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password or "")
    monkeypatch.setattr(settings, "db_name", name)


async def _seed_database(database_url: URL) -> tuple[str, dict[str, User]]:
    library_slug = f"http-load-{uuid.uuid4().hex[:8]}"
    library_id = uuid.uuid4()
    user_ids = [uuid.uuid4() for _ in range(_USER_COUNT)]
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO sys_users (
                        id, email, hashed_password, is_active, is_superuser, is_verified
                    ) VALUES (
                        :id, :email, 'test-only', true, true, true
                    )
                    """
                ),
                [
                    {"id": user_id, "email": f"upload-{index}@example.test"}
                    for index, user_id in enumerate(user_ids)
                ],
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sys_libraries (
                        id, organization_id, slug, name, embedding_model, embedding_dim,
                        vector_distance, chunk_size, chunk_overlap, qdrant_collection
                    ) VALUES (
                        :id, :organization_id, :slug, :name, 'bge-m3', 1024,
                        'cosine', 1000, 120, :collection
                    )
                    """
                ),
                {
                    "id": library_id,
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "slug": library_slug,
                    "name": "HTTP upload load acceptance",
                    "collection": f"http_load_{library_id.hex}",
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sys_organization_memberships (
                        id, organization_id, user_id, role, status
                    ) VALUES (
                        :id, :organization_id, :user_id, 'organization_admin', 'active'
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "user_id": user_ids[0],
                },
            )
    finally:
        await engine.dispose()

    users = {
        str(user_id): User(
            id=user_id,
            email=f"upload-{index}@example.test",
            hashed_password="test-only",
            is_active=True,
            is_superuser=True,
            is_verified=True,
        )
        for index, user_id in enumerate(user_ids)
    }
    return library_slug, users


@asynccontextmanager
async def _serve(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="warning",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
        listener.close()


async def _read_pg_sample(connection, application_name: str) -> PgSample:
    row = (
        await connection.execute(
            text(
                """
                WITH workload AS (
                    SELECT pid, state, xact_start
                    FROM pg_stat_activity
                    WHERE application_name = :application_name
                )
                SELECT
                    (SELECT COUNT(*) FROM workload) AS connection_count,
                    (SELECT COUNT(*) FROM workload WHERE xact_start IS NOT NULL)
                        AS transaction_count,
                    (SELECT COUNT(*) FROM workload WHERE state = 'idle in transaction')
                        AS idle_in_transaction_count,
                    COALESCE((
                        SELECT MAX(EXTRACT(EPOCH FROM clock_timestamp() - xact_start))
                        FROM workload WHERE xact_start IS NOT NULL
                    ), 0) AS max_transaction_age_seconds,
                    COALESCE((
                        SELECT MAX(EXTRACT(EPOCH FROM clock_timestamp() - xact_start))
                        FROM workload
                        WHERE state = 'idle in transaction' AND xact_start IS NOT NULL
                    ), 0) AS max_idle_transaction_age_seconds,
                    (SELECT COUNT(*) FROM pg_locks
                     WHERE pid IN (SELECT pid FROM workload) AND granted)
                        AS granted_lock_count,
                    (SELECT COUNT(*) FROM pg_locks
                     WHERE pid IN (SELECT pid FROM workload) AND NOT granted)
                        AS waiting_lock_count
                """
            ),
            {"application_name": application_name},
        )
    ).one()
    return PgSample(
        captured_at=time.monotonic(),
        connection_count=int(row.connection_count),
        transaction_count=int(row.transaction_count),
        idle_in_transaction_count=int(row.idle_in_transaction_count),
        max_transaction_age_seconds=float(row.max_transaction_age_seconds),
        max_idle_transaction_age_seconds=float(
            row.max_idle_transaction_age_seconds
        ),
        granted_lock_count=int(row.granted_lock_count),
        waiting_lock_count=int(row.waiting_lock_count),
    )


async def _sample_postgres(
    observer_engine,
    application_name: str,
    stop: asyncio.Event,
    samples: list[PgSample],
) -> None:
    async with observer_engine.connect() as connection:
        while not stop.is_set():
            samples.append(await _read_pg_sample(connection, application_name))
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.025)
            except TimeoutError:
                pass


async def _sample_api_pg_readiness(
    client: httpx.AsyncClient,
    stop: asyncio.Event,
    samples: list[tuple[int, int, dict]],
) -> None:
    while not stop.is_set():
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        samples.append((live.status_code, ready.status_code, ready.json()))
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            pass


async def _wait_for_live_upload_claims(observer_engine, expected: int) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    async with observer_engine.connect() as connection:
        while True:
            count = (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM document_import_jobs "
                        "WHERE status = 'uploading' "
                        "AND worker_id LIKE 'upload:%' "
                        "AND claimed_at IS NOT NULL"
                    )
                )
            ).scalar_one()
            if count == expected:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"expected {expected} live upload claims, observed {count}"
                )
            await asyncio.sleep(0.01)


async def _upload_user_files(
    client: httpx.AsyncClient,
    *,
    library_slug: str,
    user_id: str,
    user_index: int,
    slow_body_waiting: asyncio.Event,
    release_slow_bodies: asyncio.Event,
) -> None:
    headers = {"X-Upload-Test-User": user_id}
    batch_id = str(uuid.uuid4())
    for file_index in range(_FILES_PER_USER):
        file_name = f"user-{user_index:02d}-file-{file_index:03d}.txt"
        created = await client.post(
            f"/libraries/{library_slug}/import-sessions",
            headers=headers,
            json={
                "batch_id": batch_id,
                "file_name": file_name,
                "relative_path": f"user-{user_index:02d}/{file_name}",
                "content_type": "text/plain",
                "size_bytes": len(_CONTENT),
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        content: bytes | object = _CONTENT
        if file_index == 0:
            async def slow_body():
                yield _CONTENT[:1]
                slow_body_waiting.set()
                await release_slow_bodies.wait()
                yield _CONTENT[1:]

            content = slow_body()

        uploaded = await client.put(
            f"/libraries/{library_slug}/import-sessions/{job_id}/content",
            headers={**headers, "Upload-Offset": "0"},
            content=content,
        )
        assert uploaded.status_code == 204, uploaded.text
        assert uploaded.headers["Upload-Offset"] == str(len(_CONTENT))

        completed = await client.post(
            f"/libraries/{library_slug}/import-sessions/{job_id}/complete",
            headers=headers,
        )
        assert completed.status_code == 202, completed.text
        payload = completed.json()
        assert payload["status"] == "queued"
        assert payload["upload_offset"] == len(_CONTENT)


async def _final_job_counts(observer_engine) -> tuple[int, ...]:
    async with observer_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(DISTINCT id) AS distinct_jobs,
                        COUNT(DISTINCT requested_by_user_id) AS distinct_users,
                        COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                        COUNT(*) FILTER (WHERE upload_offset = size_bytes AND size_bytes = 4)
                            AS complete_offsets,
                        COUNT(*) FILTER (WHERE sha256 = :sha256) AS hashed,
                        COUNT(*) FILTER (
                            WHERE worker_id IS NOT NULL OR claimed_at IS NOT NULL
                        ) AS live_claims
                    FROM document_import_jobs
                    """
                ),
                {"sha256": _CONTENT_SHA256},
            )
        ).one()
    return tuple(int(value) for value in row)


def test_t1_real_http_500_file_upload_with_api_pg_readiness_sampling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1 characterization, not a production-readiness or throughput claim.

    Real boundaries: Uvicorn TCP/HTTP, FastAPI routes, upload service, full
    migrations, PostgreSQL, and local storage readiness. Replaced boundaries:
    request identity plus Qdrant/embedding/rerank availability probes.
    """
    database_name = f"vkt_http_upload_{uuid.uuid4().hex[:8]}"
    database_url = _database_url(database_name)
    upload_application_name = f"upload-http-{uuid.uuid4().hex}"
    observer_application_name = f"observer-http-{uuid.uuid4().hex}"
    staging_dir = tmp_path / "staging"
    storage_dir = tmp_path / "document-files"
    staging_dir.mkdir(parents=True)
    storage_dir.mkdir(parents=True)

    _configure_database(monkeypatch, database_name)
    asyncio.run(_admin(f'CREATE DATABASE "{database_name}"'))
    try:
        command.upgrade(Config("alembic.ini"), "head")
        library_slug, users = asyncio.run(_seed_database(database_url))

        monkeypatch.setattr(settings, "deployment_profile", "private")
        monkeypatch.setattr(settings, "organization_authorization_enabled", False)
        monkeypatch.setattr(settings, "import_staging_dir", str(staging_dir))
        monkeypatch.setattr(settings, "import_selection_max_files", 1000)
        monkeypatch.setattr(settings, "import_upload_user_inflight_limit", 1)
        monkeypatch.setattr(settings, "import_upload_global_inflight_limit", 10)
        monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
        monkeypatch.setattr(settings, "document_storage_provider", "local")
        monkeypatch.setattr(settings, "document_files_dir", str(storage_dir))

        async def exercise() -> None:
            upload_engine = create_async_engine(
                database_url,
                pool_size=2,
                max_overflow=8,
                pool_timeout=30,
                connect_args={
                    "server_settings": {"application_name": upload_application_name}
                },
            )
            observer_engine = create_async_engine(
                database_url,
                poolclass=NullPool,
                isolation_level="AUTOCOMMIT",
                connect_args={
                    "server_settings": {"application_name": observer_application_name}
                },
            )
            upload_sessions = async_sessionmaker(upload_engine, expire_on_commit=False)
            monkeypatch.setattr(app_db, "async_session_factory", upload_sessions)
            monkeypatch.setattr(health_api, "async_session_factory", upload_sessions)

            async def dependency_available() -> bool:
                return True

            async def rerank_available() -> str:
                return "ok"

            monkeypatch.setattr(health_api, "_check_qdrant", dependency_available)
            monkeypatch.setattr(health_api, "_check_embedding", dependency_available)
            monkeypatch.setattr(health_api, "_check_rerank", rerank_available)

            async def test_user(
                x_upload_test_user: str = Header(alias="X-Upload-Test-User"),
            ) -> User:
                user = users.get(x_upload_test_user)
                if user is None:
                    raise HTTPException(401, "unknown test user")
                return user

            app = create_app()
            app.dependency_overrides[current_active_user] = test_user
            pg_samples: list[PgSample] = []
            readiness_samples: list[tuple[int, int, dict]] = []
            stop_sampling = asyncio.Event()
            release_slow_bodies = asyncio.Event()
            slow_body_events = [asyncio.Event() for _ in range(_USER_COUNT)]
            tasks: list[asyncio.Task] = []
            sampler_tasks: list[asyncio.Task] = []
            try:
                async with _serve(app) as base_url:
                    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
                    timeout = httpx.Timeout(60, connect=10)
                    async with (
                        httpx.AsyncClient(
                            base_url=base_url,
                            limits=limits,
                            timeout=timeout,
                        ) as upload_client,
                        httpx.AsyncClient(
                            base_url=base_url,
                            limits=httpx.Limits(max_connections=2),
                            timeout=timeout,
                        ) as health_client,
                    ):
                        tasks = [
                            asyncio.create_task(
                                _upload_user_files(
                                    upload_client,
                                    library_slug=library_slug,
                                    user_id=user_id,
                                    user_index=index,
                                    slow_body_waiting=slow_body_events[index],
                                    release_slow_bodies=release_slow_bodies,
                                )
                            )
                            for index, user_id in enumerate(users)
                        ]
                        sampler_tasks = [
                            asyncio.create_task(
                                _sample_postgres(
                                    observer_engine,
                                    upload_application_name,
                                    stop_sampling,
                                    pg_samples,
                                )
                            ),
                            asyncio.create_task(
                                _sample_api_pg_readiness(
                                    health_client,
                                    stop_sampling,
                                    readiness_samples,
                                )
                            ),
                        ]
                        await asyncio.wait_for(
                            asyncio.gather(*(event.wait() for event in slow_body_events)),
                            timeout=60,
                        )
                        await _wait_for_live_upload_claims(
                            observer_engine,
                            _USER_COUNT,
                        )
                        gate_started = time.monotonic()
                        await asyncio.sleep(2)
                        gate_finished = time.monotonic()
                        release_slow_bodies.set()
                        await asyncio.gather(*tasks)
                        stop_sampling.set()
                        await asyncio.gather(*sampler_tasks)

                assert pg_samples
                assert readiness_samples
                assert any(sample.connection_count > 0 for sample in pg_samples)
                assert max(sample.connection_count for sample in pg_samples) <= 10
                gated_samples = [
                    sample
                    for sample in pg_samples
                    if gate_started <= sample.captured_at <= gate_finished
                ]
                assert gated_samples
                assert any(sample.connection_count > 0 for sample in gated_samples)
                gate_metrics = {
                    "max_connections": max(
                        sample.connection_count for sample in gated_samples
                    ),
                    "max_transaction_age_seconds": max(
                        sample.max_transaction_age_seconds for sample in gated_samples
                    ),
                    "max_idle_transaction_age_seconds": max(
                        sample.max_idle_transaction_age_seconds
                        for sample in gated_samples
                    ),
                    "max_waiting_locks": max(
                        sample.waiting_lock_count for sample in gated_samples
                    ),
                }
                assert gate_metrics["max_connections"] <= 10, gate_metrics
                assert gate_metrics["max_transaction_age_seconds"] < 1.0, gate_metrics
                assert gate_metrics["max_idle_transaction_age_seconds"] < 1.0, gate_metrics
                assert gate_metrics["max_waiting_locks"] == 0, gate_metrics
                assert all(sample.max_transaction_age_seconds >= 0 for sample in pg_samples)
                assert all(
                    sample.max_idle_transaction_age_seconds >= 0
                    for sample in pg_samples
                )
                assert all(sample.granted_lock_count >= 0 for sample in pg_samples)
                assert all(sample.waiting_lock_count >= 0 for sample in pg_samples)

                for live_status, ready_status, payload in readiness_samples:
                    assert live_status == 200
                    assert ready_status == 200
                    assert payload["status"] == "ready"
                    assert payload["components"] == {
                        "database": "ok",
                        "embedding": "ok",
                        "initialization": "ok",
                        "migrations": "ok",
                        "object_storage": "ok",
                        "qdrant": "ok",
                        "rerank": "ok",
                    }

                assert await _final_job_counts(observer_engine) == (
                    500,
                    500,
                    10,
                    500,
                    500,
                    500,
                    0,
                )
                staged_files = list(staging_dir.glob("*.upload"))
                assert len(staged_files) == 500
                assert all(path.read_bytes() == _CONTENT for path in staged_files)
                assert upload_engine.sync_engine.pool.checkedout() == 0
            finally:
                release_slow_bodies.set()
                stop_sampling.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if sampler_tasks:
                    await asyncio.gather(*sampler_tasks, return_exceptions=True)
                app.dependency_overrides.clear()
                await upload_engine.dispose()
                async with observer_engine.connect() as connection:
                    remaining = (
                        await connection.execute(
                            text(
                                "SELECT COUNT(*) FROM pg_stat_activity "
                                "WHERE application_name = :upload_name"
                            ),
                            {"upload_name": upload_application_name},
                        )
                    ).scalar_one()
                assert remaining == 0
                await observer_engine.dispose()

        asyncio.run(exercise())
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        shutil.rmtree(tmp_path, ignore_errors=True)
