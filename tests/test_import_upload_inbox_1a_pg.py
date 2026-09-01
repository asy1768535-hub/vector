from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import import_uploads as import_uploads_api
from app.schemas.documents import ImportSessionCreate
from app.services import import_uploads


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database",
)


_TABLE_DDL = """
CREATE TABLE document_import_jobs (
    id uuid PRIMARY KEY,
    library_id uuid NOT NULL,
    requested_by_user_id uuid,
    batch_id uuid NOT NULL,
    file_name varchar(512) NOT NULL,
    relative_path text,
    content_type varchar(255),
    size_bytes bigint NOT NULL,
    upload_offset bigint NOT NULL DEFAULT 0,
    last_modified_millis bigint,
    sha256 varchar(64),
    conversion_sha256 varchar(64),
    converter_version varchar(128),
    conversion_attempt_count integer NOT NULL DEFAULT 0,
    staging_key varchar(512) NOT NULL,
    external_id varchar(512),
    replace_document_id uuid,
    security_level varchar(64),
    graph_extraction_requested boolean NOT NULL DEFAULT false,
    status varchar(32) NOT NULL DEFAULT 'uploading',
    current_stage varchar(32) NOT NULL DEFAULT 'uploading',
    attempt_count integer NOT NULL DEFAULT 0,
    worker_id varchar(160),
    claimed_at timestamptz,
    upload_completed_at timestamptz,
    finished_at timestamptz,
    last_error text,
    result_operation varchar(32),
    document_id uuid,
    document_revision_id uuid,
    embedding_job_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


def _config(
    *,
    user_limit: int = 1,
    global_limit: int = 10,
    staging_dir: str = "storage/import_staging",
):
    return SimpleNamespace(
        import_upload_claim_stale_seconds=300,
        import_upload_claim_heartbeat_seconds=30,
        import_upload_retry_after_seconds=2,
        import_upload_user_inflight_limit=user_limit,
        import_upload_global_inflight_limit=global_limit,
        import_staging_dir=staging_dir,
        import_upload_chunk_bytes=1024,
    )


class _NoopLease:
    _lost_error = None
    thread_stop_event = None

    def ensure_current(self) -> None:
        return None

    async def stop_renewal(self) -> None:
        return None


@asynccontextmanager
async def _noop_claim_lease(*_args, **_kwargs):
    yield _NoopLease()


async def _prepare_schema(engine) -> str:
    schema = f"upload_1a_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(text(_TABLE_DDL))
    return schema


async def _insert_job(
    session,
    *,
    job_id: uuid.UUID,
    library_id: uuid.UUID,
    user_id: uuid.UUID,
    batch_id: uuid.UUID | None = None,
    file_name: str | None = None,
    worker_id: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO document_import_jobs (
                id, library_id, requested_by_user_id, batch_id, file_name,
                size_bytes, upload_offset, staging_key, worker_id, claimed_at
            ) VALUES (
                :id, :library_id, :user_id, :batch_id, :file_name,
                4, 0, :staging_key, CAST(:worker_id AS varchar),
                CASE WHEN CAST(:worker_id AS varchar) IS NULL THEN NULL ELSE NOW() END
            )
            """
        ),
        {
            "id": job_id,
            "library_id": library_id,
            "user_id": user_id,
            "batch_id": batch_id or uuid.uuid4(),
            "file_name": file_name or f"{job_id}.txt",
            "staging_key": f"{job_id.hex}.upload",
            "worker_id": worker_id,
        },
    )


def test_create_session_does_not_reuse_or_count_another_users_batch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        schema = await _prepare_schema(engine)
        workload_engine = create_async_engine(
            _DSN,
            connect_args={"server_settings": {"search_path": schema}},
        )
        sessions = async_sessionmaker(workload_engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        first_user_id = uuid.uuid4()
        second_user_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        existing_job_id = uuid.uuid4()
        file_name = "shared-name.txt"
        config = SimpleNamespace(
            import_staging_dir=str(tmp_path),
            import_staging_max_file_bytes=1024,
            import_selection_max_files=1,
            doc_conversion_max_bytes=1024,
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=existing_job_id,
                    library_id=library_id,
                    user_id=first_user_id,
                    batch_id=batch_id,
                    file_name=file_name,
                )

            async with sessions() as session:
                created = await import_uploads.create_session(
                    session,
                    library=SimpleNamespace(id=library_id),
                    user=SimpleNamespace(id=second_user_id),
                    payload=ImportSessionCreate(
                        batch_id=batch_id,
                        file_name=file_name,
                        size_bytes=4,
                    ),
                    config=config,
                )

                assert created.id != existing_job_id
                assert created.requested_by_user_id == second_user_id
        finally:
            await workload_engine.dispose()
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


def test_count_and_claim_are_serialized_across_connections() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        schema = await _prepare_schema(engine)
        library_id = uuid.uuid4()
        user_id = uuid.uuid4()
        first_job_id = uuid.uuid4()
        second_job_id = uuid.uuid4()
        actor = SimpleNamespace(id=user_id, is_superuser=False)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=first_job_id,
                    library_id=library_id,
                    user_id=user_id,
                )
                await _insert_job(
                    connection,
                    job_id=second_job_id,
                    library_id=library_id,
                    user_id=user_id,
                )

            first = sessions()
            second = sessions()
            try:
                await first.execute(text(f'SET search_path TO "{schema}"'))
                await second.execute(text(f'SET search_path TO "{schema}"'))
                claim = await import_uploads.claim_upload_operation(
                    first,
                    library_id=library_id,
                    job_id=first_job_id,
                    user=actor,
                    operation="content",
                    expected_offset=0,
                    config=_config(),
                )
                assert claim.owner_token is not None

                waiting = asyncio.create_task(
                    import_uploads.claim_upload_operation(
                        second,
                        library_id=library_id,
                        job_id=second_job_id,
                        user=actor,
                        operation="content",
                        expected_offset=0,
                        config=_config(),
                    )
                )
                await asyncio.sleep(0.1)
                assert not waiting.done()

                await first.commit()
                with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                    await asyncio.wait_for(waiting, timeout=2)
                assert exc_info.value.code == "upload_user_limit"
                assert exc_info.value.status_code == 429
                await second.rollback()
            finally:
                await first.close()
                await second.close()
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


def test_global_inflight_limit_counts_live_claims() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        schema = await _prepare_schema(engine)
        library_id = uuid.uuid4()
        target_user_id = uuid.uuid4()
        target_job_id = uuid.uuid4()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=target_job_id,
                    library_id=library_id,
                    user_id=target_user_id,
                )
                for _ in range(10):
                    user_id = uuid.uuid4()
                    await _insert_job(
                        connection,
                        job_id=uuid.uuid4(),
                        library_id=library_id,
                        user_id=user_id,
                        worker_id=(
                            f"upload:{user_id.hex}:content:{uuid.uuid4().hex}"
                        ),
                    )

            async with sessions() as session:
                await session.execute(text(f'SET search_path TO "{schema}"'))
                with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                    await import_uploads.claim_upload_operation(
                        session,
                        library_id=library_id,
                        job_id=target_job_id,
                        user=SimpleNamespace(id=target_user_id, is_superuser=False),
                        operation="content",
                        expected_offset=0,
                        config=_config(),
                    )
                assert exc_info.value.code == "upload_global_limit"
                assert exc_info.value.status_code == 429
                await session.rollback()
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


def test_global_limit_serializes_different_users_competing_for_last_slot() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        schema = await _prepare_schema(engine)
        library_id = uuid.uuid4()
        first_user_id = uuid.uuid4()
        second_user_id = uuid.uuid4()
        first_job_id = uuid.uuid4()
        second_job_id = uuid.uuid4()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=first_job_id,
                    library_id=library_id,
                    user_id=first_user_id,
                )
                await _insert_job(
                    connection,
                    job_id=second_job_id,
                    library_id=library_id,
                    user_id=second_user_id,
                )

            first = sessions()
            second = sessions()
            try:
                await first.execute(text(f'SET search_path TO "{schema}"'))
                await second.execute(text(f'SET search_path TO "{schema}"'))
                first_claim = await import_uploads.claim_upload_operation(
                    first,
                    library_id=library_id,
                    job_id=first_job_id,
                    user=SimpleNamespace(id=first_user_id, is_superuser=False),
                    operation="content",
                    expected_offset=0,
                    config=_config(global_limit=1),
                )
                assert first_claim.owner_token is not None

                waiting = asyncio.create_task(
                    import_uploads.claim_upload_operation(
                        second,
                        library_id=library_id,
                        job_id=second_job_id,
                        user=SimpleNamespace(id=second_user_id, is_superuser=False),
                        operation="content",
                        expected_offset=0,
                        config=_config(global_limit=1),
                    )
                )
                await asyncio.sleep(0.1)
                assert not waiting.done()

                await first.commit()
                with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                    await asyncio.wait_for(waiting, timeout=2)
                error = exc_info.value
                assert error.code == "upload_global_limit"
                assert error.status_code == 429
                assert error.upload_offset == 0
                assert error.retry_after_seconds == 2
                with pytest.raises(HTTPException) as http_exc_info:
                    import_uploads_api._raise_upload_error(error)
                assert http_exc_info.value.headers == {
                    "Upload-Offset": "0",
                    "Retry-After": "2",
                }
                await second.rollback()
            finally:
                await first.close()
                await second.close()
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_takeover_fences_old_owner_offset_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        schema = await _prepare_schema(engine)
        workload_engine = create_async_engine(
            _DSN,
            connect_args={"server_settings": {"search_path": schema}},
        )
        sessions = async_sessionmaker(workload_engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        user_id = uuid.uuid4()
        job_id = uuid.uuid4()
        old_owner = f"upload:{user_id.hex}:content:{uuid.uuid4().hex}"
        staging_key = f"{job_id.hex}.upload"
        path = tmp_path / staging_key
        path.write_bytes(b"")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=job_id,
                    library_id=library_id,
                    user_id=user_id,
                    worker_id=old_owner,
                )
                await connection.execute(
                    text(
                        "UPDATE document_import_jobs "
                        "SET claimed_at = NOW() - INTERVAL '10 minutes' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": job_id},
                )

            async with sessions() as session:
                await session.execute(text(f'SET search_path TO "{schema}"'))
                new_claim = await import_uploads.claim_upload_operation(
                    session,
                    library_id=library_id,
                    job_id=job_id,
                    user=SimpleNamespace(id=user_id, is_superuser=False),
                    operation="content",
                    expected_offset=0,
                    config=_config(staging_dir=str(tmp_path)),
                )
                await session.commit()
                assert new_claim.owner_token != old_owner

                old_claim = import_uploads.UploadOperationClaim(
                    job_id=job_id,
                    owner_token=old_owner,
                    operation="content",
                    staging_key=staging_key,
                    file_name=f"{job_id}.txt",
                    size_bytes=4,
                    upload_offset=0,
                )

                async def body():
                    yield b"data"

                monkeypatch.setattr(
                    import_uploads,
                    "keep_upload_claim_alive",
                    _noop_claim_lease,
                )
                with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                    await import_uploads.append_claimed_content(
                        session,
                        claim=old_claim,
                        body=body(),
                        config=_config(staging_dir=str(tmp_path)),
                    )
                assert exc_info.value.code == "upload_claim_lost"
                assert path.read_bytes() == b""

                row = (
                    await session.execute(
                        text(
                            "SELECT worker_id, upload_offset "
                            "FROM document_import_jobs WHERE id = :job_id"
                        ),
                        {"job_id": job_id},
                    )
                ).one()
                assert row.worker_id == new_claim.owner_token
                assert row.upload_offset == 0
        finally:
            await workload_engine.dispose()
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


def test_releasing_the_same_claim_twice_is_idempotent() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        schema = await _prepare_schema(engine)
        library_id = uuid.uuid4()
        user_id = uuid.uuid4()
        job_id = uuid.uuid4()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=job_id,
                    library_id=library_id,
                    user_id=user_id,
                )

            async with sessions() as session:
                await session.execute(text(f'SET search_path TO "{schema}"'))
                claim = await import_uploads.claim_upload_operation(
                    session,
                    library_id=library_id,
                    job_id=job_id,
                    user=SimpleNamespace(id=user_id, is_superuser=False),
                    operation="content",
                    expected_offset=0,
                    config=_config(),
                )
                await session.commit()

                await import_uploads.release_upload_claim(session, claim)
                await import_uploads.release_upload_claim(session, claim)
                row = (
                    await session.execute(
                        text(
                            "SELECT worker_id, claimed_at, upload_offset "
                            "FROM document_import_jobs WHERE id = :job_id"
                        ),
                        {"job_id": job_id},
                    )
                ).one()
                assert row.worker_id is None
                assert row.claimed_at is None
                assert row.upload_offset == 0
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("file_count", [100, 500], ids=["100-files", "500-files"])
def test_bounded_multi_user_upload_inbox_load(
    file_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        schema = await _prepare_schema(engine)
        workload_engine = create_async_engine(
            _DSN,
            connect_args={"server_settings": {"search_path": schema}},
        )
        sessions = async_sessionmaker(workload_engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        user_ids = [uuid.uuid4() for _ in range(10)]
        jobs_by_user: dict[uuid.UUID, list[uuid.UUID]] = {
            user_id: [] for user_id in user_ids
        }
        config = _config(staging_dir=str(tmp_path))
        expected_digest = hashlib.sha256(b"data").hexdigest()
        first_claims_ready = asyncio.Event()
        release_first_claims = asyncio.Event()
        first_claim_count = 0
        first_claim_lock = asyncio.Lock()
        tasks: list[asyncio.Task[None]] = []

        async def upload_user_files(user_id: uuid.UUID) -> None:
            nonlocal first_claim_count
            actor = SimpleNamespace(id=user_id, is_superuser=False)
            async with sessions() as session:
                for index, job_id in enumerate(jobs_by_user[user_id]):
                    claim = await import_uploads.claim_upload_operation(
                        session,
                        library_id=library_id,
                        job_id=job_id,
                        user=actor,
                        operation="content",
                        expected_offset=0,
                        config=config,
                    )
                    await session.commit()
                    if index == 0:
                        async with first_claim_lock:
                            first_claim_count += 1
                            if first_claim_count == len(user_ids):
                                first_claims_ready.set()
                        await release_first_claims.wait()

                    async def body():
                        yield b"data"

                    next_offset = await import_uploads.append_claimed_content(
                        session,
                        claim=claim,
                        body=body(),
                        config=config,
                    )
                    assert next_offset == 4

                    complete_claim = await import_uploads.claim_upload_operation(
                        session,
                        library_id=library_id,
                        job_id=job_id,
                        user=actor,
                        operation="complete",
                        config=config,
                    )
                    await session.commit()
                    assert not complete_claim.already_queued
                    digest = await import_uploads.complete_claimed_upload(
                        session,
                        claim=complete_claim,
                        config=config,
                    )
                    assert digest == expected_digest

        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                for index in range(file_count):
                    user_id = user_ids[index % len(user_ids)]
                    job_id = uuid.uuid4()
                    jobs_by_user[user_id].append(job_id)
                    await _insert_job(
                        connection,
                        job_id=job_id,
                        library_id=library_id,
                        user_id=user_id,
                    )

            tasks = [
                asyncio.create_task(upload_user_files(user_id))
                for user_id in user_ids
            ]
            try:
                await asyncio.wait_for(first_claims_ready.wait(), timeout=20)
                async with workload_engine.connect() as observer:
                    active_claims = (
                        await observer.execute(
                            text(
                                "SELECT COUNT(*) FROM document_import_jobs "
                                "WHERE status = 'uploading' "
                                "AND worker_id LIKE 'upload:%'"
                            )
                        )
                    ).scalar_one()
                assert active_claims == 10
            finally:
                release_first_claims.set()
            await asyncio.gather(*tasks)

            async with workload_engine.connect() as observer:
                row = (
                    await observer.execute(
                        text(
                            "SELECT COUNT(*) AS total, "
                            "COUNT(*) FILTER (WHERE status = 'queued') AS queued, "
                            "COUNT(*) FILTER (WHERE upload_offset = size_bytes) AS complete, "
                            "COUNT(*) FILTER (WHERE worker_id IS NULL "
                            "AND claimed_at IS NULL) AS released, "
                            "COUNT(*) FILTER (WHERE sha256 = :sha256) AS hashed "
                            "FROM document_import_jobs"
                        ),
                        {"sha256": expected_digest},
                    )
                ).one()
            assert tuple(row) == (
                file_count,
                file_count,
                file_count,
                file_count,
                file_count,
            )
            for job_ids in jobs_by_user.values():
                for job_id in job_ids:
                    assert (tmp_path / f"{job_id.hex}.upload").read_bytes() == b"data"
        finally:
            release_first_claims.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await workload_engine.dispose()
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    asyncio.run(exercise())


def test_hundred_file_retry_recovers_after_offset_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RollbackAndFailNextCommit:
        def __init__(self, session) -> None:
            self.session = session
            self.failure_count = 0
            self._armed = True

        async def execute(self, *args, **kwargs):
            return await self.session.execute(*args, **kwargs)

        async def rollback(self) -> None:
            await self.session.rollback()

        async def commit(self) -> None:
            if self._armed:
                self._armed = False
                self.failure_count += 1
                await self.session.rollback()
                raise ConnectionError("injected offset commit failure")
            await self.session.commit()

    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        schema = await _prepare_schema(engine)
        workload_engine = create_async_engine(
            _DSN,
            connect_args={"server_settings": {"search_path": schema}},
        )
        sessions = async_sessionmaker(workload_engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        user_id = uuid.uuid4()
        job_ids = [uuid.uuid4() for _ in range(100)]
        expected_content = b"data"
        expected_digest = hashlib.sha256(expected_content).hexdigest()
        config = _config(staging_dir=str(tmp_path))
        injected_failures = 0
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                for job_id in job_ids:
                    await _insert_job(
                        connection,
                        job_id=job_id,
                        library_id=library_id,
                        user_id=user_id,
                    )

            actor = SimpleNamespace(id=user_id, is_superuser=False)
            async with sessions() as session:
                for index, job_id in enumerate(job_ids):
                    claim = await import_uploads.claim_upload_operation(
                        session,
                        library_id=library_id,
                        job_id=job_id,
                        user=actor,
                        operation="content",
                        expected_offset=0,
                        config=config,
                    )
                    await session.commit()

                    async def body():
                        yield expected_content

                    path = tmp_path / claim.staging_key
                    if index % 5 == 0:
                        faulty_session = RollbackAndFailNextCommit(session)
                        with pytest.raises(
                            ConnectionError,
                            match="injected offset commit failure",
                        ):
                            await import_uploads.append_claimed_content(
                                faulty_session,
                                claim=claim,
                                body=body(),
                                config=config,
                            )
                        injected_failures += faulty_session.failure_count
                        failed_row = (
                            await session.execute(
                                text(
                                    "SELECT upload_offset, worker_id, claimed_at "
                                    "FROM document_import_jobs WHERE id = :job_id"
                                ),
                                {"job_id": job_id},
                            )
                        ).one()
                        assert tuple(failed_row) == (0, None, None)
                        assert path.read_bytes() == expected_content

                        claim = await import_uploads.claim_upload_operation(
                            session,
                            library_id=library_id,
                            job_id=job_id,
                            user=actor,
                            operation="content",
                            expected_offset=0,
                            config=config,
                        )
                        await session.commit()

                    assert (
                        await import_uploads.append_claimed_content(
                            session,
                            claim=claim,
                            body=body(),
                            config=config,
                        )
                        == len(expected_content)
                    )
                    complete_claim = await import_uploads.claim_upload_operation(
                        session,
                        library_id=library_id,
                        job_id=job_id,
                        user=actor,
                        operation="complete",
                        config=config,
                    )
                    await session.commit()
                    assert (
                        await import_uploads.complete_claimed_upload(
                            session,
                            claim=complete_claim,
                            config=config,
                        )
                        == expected_digest
                    )

            assert injected_failures == 20
            async with workload_engine.connect() as observer:
                result = (
                    await observer.execute(
                        text(
                            "SELECT COUNT(*) AS total, "
                            "COUNT(DISTINCT id) AS distinct_jobs, "
                            "COUNT(*) FILTER (WHERE status = 'queued') AS queued, "
                            "COUNT(*) FILTER (WHERE upload_offset = size_bytes) AS complete, "
                            "COUNT(*) FILTER (WHERE sha256 = :sha256) AS hashed, "
                            "COUNT(*) FILTER (WHERE worker_id IS NOT NULL "
                            "OR claimed_at IS NOT NULL) AS active_claims "
                            "FROM document_import_jobs"
                        ),
                        {"sha256": expected_digest},
                    )
                ).one()
            assert tuple(result) == (100, 100, 100, 100, 100, 0)
            for job_id in job_ids:
                assert (tmp_path / f"{job_id.hex}.upload").read_bytes() == expected_content
        finally:
            await workload_engine.dispose()
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    asyncio.run(exercise())


def test_slow_upload_body_holds_no_database_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        schema = await _prepare_schema(engine)
        application_name = f"upload-1a-slow-{uuid.uuid4().hex}"
        workload_engine = create_async_engine(
            _DSN,
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "application_name": application_name,
                }
            },
        )
        observer_engine = create_async_engine(_DSN, poolclass=NullPool)
        sessions = async_sessionmaker(workload_engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        user_id = uuid.uuid4()
        job_id = uuid.uuid4()
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        config = _config(staging_dir=str(tmp_path))
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await _insert_job(
                    connection,
                    job_id=job_id,
                    library_id=library_id,
                    user_id=user_id,
                )

            async with sessions() as session:
                backend_pid = (
                    await session.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                claim = await import_uploads.claim_upload_operation(
                    session,
                    library_id=library_id,
                    job_id=job_id,
                    user=SimpleNamespace(id=user_id, is_superuser=False),
                    operation="content",
                    expected_offset=0,
                    config=config,
                )
                await session.commit()

                async def slow_body():
                    body_started.set()
                    await release_body.wait()
                    yield b"data"

                upload = asyncio.create_task(
                    import_uploads.append_claimed_content(
                        session,
                        claim=claim,
                        body=slow_body(),
                        config=config,
                    )
                )
                try:
                    await asyncio.wait_for(body_started.wait(), timeout=5)
                    async with observer_engine.connect() as observer:
                        activity = (
                            await observer.execute(
                                text(
                                    "SELECT state, xact_start, application_name "
                                    "FROM pg_stat_activity WHERE pid = :pid"
                                ),
                                {"pid": backend_pid},
                            )
                        ).one()
                    assert activity.state == "idle"
                    assert activity.xact_start is None
                    assert activity.application_name == application_name
                finally:
                    release_body.set()
                assert await upload == 4

                complete_claim = await import_uploads.claim_upload_operation(
                    session,
                    library_id=library_id,
                    job_id=job_id,
                    user=SimpleNamespace(id=user_id, is_superuser=False),
                    operation="complete",
                    config=config,
                )
                await session.commit()
                await import_uploads.complete_claimed_upload(
                    session,
                    claim=complete_claim,
                    config=config,
                )
        finally:
            release_body.set()
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await workload_engine.dispose()
            await observer_engine.dispose()
            await engine.dispose()

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    asyncio.run(exercise())
