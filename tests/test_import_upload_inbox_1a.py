from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import struct
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from app.api import import_uploads as import_uploads_api
from app.config import Settings
from app.schemas.documents import ImportJobProgressRequest
from app.services import import_uploads


def _minimal_word_document_cfb() -> bytes:
    def directory_entry(
        name: str,
        *,
        object_type: int,
        child: int = 0xFFFFFFFF,
    ) -> bytes:
        entry = bytearray(128)
        encoded_name = (name + "\x00").encode("utf-16le")
        entry[: len(encoded_name)] = encoded_name
        struct.pack_into("<H", entry, 64, len(encoded_name))
        entry[66] = object_type
        entry[67] = 1
        struct.pack_into("<III", entry, 68, 0xFFFFFFFF, 0xFFFFFFFF, child)
        struct.pack_into("<I", entry, 116, 0xFFFFFFFE)
        return bytes(entry)

    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4, 0xFFFFFFFF)
    struct.pack_into("<I", header, 76, 1)

    directory = (
        directory_entry("Root Entry", object_type=5, child=1)
        + directory_entry("WordDocument", object_type=2)
    ).ljust(512, b"\x00")
    fat = bytearray(b"\xff" * 512)
    struct.pack_into("<I", fat, 0, 0xFFFFFFFE)
    struct.pack_into("<I", fat, 4, 0xFFFFFFFD)
    return bytes(header) + directory + bytes(fat)


def _hold_upload_file_lock(
    path_value: str,
    ready,
    release,
) -> None:
    path = Path(path_value)
    with path.open("r+b", buffering=0) as handle:
        import_uploads._try_lock_file(handle)
        ready.set()
        try:
            release.wait(10)
        finally:
            import_uploads._unlock_file(handle)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _JobRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _ConditionalDb:
    def __init__(self, returned_offset: int | None):
        self.returned_offset = returned_offset
        self.execute_calls = []
        self.commit_count = 0
        self.rollback_count = 0
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)

    async def execute(self, statement):
        self.execute_calls.append(statement)
        return _ScalarResult(self.returned_offset)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


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


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        import_staging_dir=str(tmp_path),
        import_upload_chunk_bytes=1024,
        import_upload_claim_heartbeat_seconds=30,
        import_upload_claim_stale_seconds=300,
        import_upload_retry_after_seconds=2,
        doc_conversion_max_bytes=1024,
        document_storage_provider="local",
        document_files_dir=str(tmp_path / "objects"),
        document_storage_endpoint_ref="primary",
        document_storage_max_read_bytes=1024 * 1024,
    )


def _claim(*, offset: int = 4, size: int = 7, operation: str = "content"):
    return import_uploads.UploadOperationClaim(
        job_id=uuid.uuid4(),
        owner_token=f"upload:{uuid.uuid4().hex}:content:{uuid.uuid4().hex}",
        operation=operation,
        staging_key=f"{uuid.uuid4().hex}.upload",
        file_name="sample.txt",
        size_bytes=size,
        upload_offset=offset,
        already_queued=False,
        library_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        relative_path="folder/sample.txt",
        content_type="text/plain",
    )


def test_upload_inflight_defaults_are_bounded() -> None:
    config = Settings(_env_file=None)

    assert config.import_upload_file_concurrency == 1
    assert config.import_upload_user_inflight_limit == 1
    assert config.import_upload_global_inflight_limit == 10
    assert config.import_upload_claim_heartbeat_seconds < config.import_upload_claim_stale_seconds


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "import_upload_user_inflight_limit": 2,
            "import_upload_global_inflight_limit": 1,
        },
        {
            "import_upload_claim_heartbeat_seconds": 60,
            "import_upload_claim_stale_seconds": 120,
        },
    ],
)
def test_upload_inflight_configuration_fails_closed(overrides) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)


def test_retryable_upload_error_exposes_offset_and_retry_headers() -> None:
    error = import_uploads.ImportUploadError(
        "upload_busy",
        "upload is busy",
        status_code=409,
        upload_offset=37,
        retry_after_seconds=2,
    )

    with pytest.raises(HTTPException) as exc_info:
        import_uploads_api._raise_upload_error(error)

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Upload-Offset": "37", "Retry-After": "2"}


def test_progress_query_only_projects_the_requested_owned_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_id = uuid.uuid4()
    user_id = uuid.uuid4()
    requested_id = uuid.uuid4()
    job = SimpleNamespace(id=requested_id, library_id=library_id, requested_by_user_id=user_id)
    captured: list[list[object]] = []

    class Db:
        async def execute(self, _statement):
            return _JobRows([job])

    async def project(_db, jobs):
        captured.append(jobs)
        return [{"id": requested_id}]

    monkeypatch.setattr(import_uploads_api.import_uploads, "job_projections", project)

    result = asyncio.run(
        import_uploads_api.list_import_job_progress(
            body=ImportJobProgressRequest(job_ids=[requested_id]),
            lib=SimpleNamespace(id=library_id),
            user=SimpleNamespace(id=user_id, is_superuser=False),
            db=Db(),
        )
    )

    assert result == [{"id": requested_id}]
    assert captured == [[job]]


def test_append_repairs_uncommitted_tail_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!uncommitted-tail")
    db = _ConditionalDb(returned_offset=7)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    async def body():
        yield b"new"

    next_offset = asyncio.run(
        import_uploads.append_claimed_content(
            db,
            claim=claim,
            body=body(),
            config=_config(tmp_path),
        )
    )

    assert next_offset == 7
    assert path.read_bytes() == b"old!new"
    assert db.commit_count == 1
    assert db.rollback_count == 0


def test_append_open_failure_releases_claim_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    db = _ConditionalDb(returned_offset=claim.upload_offset)

    class BrokenPath:
        def open(self, *_args, **_kwargs):
            raise OSError("staging open failed")

    async def body():
        yield b"new"

    monkeypatch.setattr(import_uploads, "staging_path", lambda *_args: BrokenPath())

    with pytest.raises(OSError, match="staging open failed"):
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    assert len(db.execute_calls) == 1
    assert db.commit_count == 1


def test_lost_owner_rolls_back_uncommitted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!")
    db = _ConditionalDb(returned_offset=None)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    async def body():
        yield b"new"

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == "upload_claim_lost"
    assert path.read_bytes() == b"old!"
    assert db.rollback_count >= 1


def test_os_file_lock_fences_a_live_writer_after_db_lease_age(tmp_path: Path) -> None:
    path = tmp_path / "lease-expired.upload"
    path.write_bytes(b"")
    first = path.open("r+b", buffering=0)
    second = path.open("r+b", buffering=0)
    try:
        import_uploads._try_lock_file(first)
        with pytest.raises(import_uploads.ImportUploadError) as exc_info:
            import_uploads._try_lock_file(second, upload_offset=11)
        assert exc_info.value.code == "upload_busy"
        assert exc_info.value.upload_offset == 11
    finally:
        import_uploads._unlock_file(first)
        first.close()
        second.close()
        path.unlink(missing_ok=True)


def test_os_file_lock_fences_a_writer_in_another_process(tmp_path: Path) -> None:
    path = tmp_path / "cross-process.upload"
    path.write_bytes(b"\0")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_upload_file_lock,
        args=(str(path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10), f"lock holder exited with {process.exitcode}"
        with path.open("r+b", buffering=0) as handle:
            with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                import_uploads._try_lock_file(
                    handle,
                    upload_offset=11,
                    retry_after_seconds=2,
                )
        assert exc_info.value.code == "upload_busy"
        assert exc_info.value.upload_offset == 11
        assert exc_info.value.retry_after_seconds == 2
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0


def test_unknown_offset_commit_outcome_preserves_fsynced_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!")

    class Db(_ConditionalDb):
        async def commit(self):
            self.commit_count += 1
            if self.commit_count == 1:
                raise ConnectionError("commit result unknown")

    db = Db(returned_offset=7)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    async def body():
        yield b"new"

    with pytest.raises(ConnectionError, match="commit result unknown"):
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    # A retry reconciles this tail against whichever offset PostgreSQL committed.
    assert path.read_bytes() == b"old!new"


def test_body_failure_releases_claim_and_restores_committed_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!")
    db = _ConditionalDb(returned_offset=1)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    async def body():
        yield b"new"
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    assert path.read_bytes() == b"old!"
    assert db.commit_count == 1


def test_fsync_failure_releases_claim_and_restores_committed_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!")
    db = _ConditionalDb(returned_offset=claim.upload_offset)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    def fail_fsync(_fd):
        raise OSError("fsync failed")

    async def body():
        yield b"new"

    monkeypatch.setattr(import_uploads.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    assert path.read_bytes() == b"old!"
    assert len(db.execute_calls) == 1
    assert db.commit_count == 1


def test_short_write_releases_claim_and_restores_committed_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    path = tmp_path / claim.staging_key
    path.write_bytes(b"old!")
    db = _ConditionalDb(returned_offset=claim.upload_offset)

    class ShortWriteHandle:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, chunk):
            return self.handle.write(chunk[:-1])

    class ShortWritePath:
        def open(self, *args, **kwargs):
            return ShortWriteHandle(path.open(*args, **kwargs))

    async def body():
        yield b"new"

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(import_uploads, "staging_path", lambda *_args: ShortWritePath())

    with pytest.raises(OSError, match="short write"):
        asyncio.run(
            import_uploads.append_claimed_content(
                db,
                claim=claim,
                body=body(),
                config=_config(tmp_path),
            )
        )

    assert path.read_bytes() == b"old!"
    assert len(db.execute_calls) == 1
    assert db.commit_count == 1


def test_complete_validates_file_before_conditional_queued_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(offset=7, size=7, operation="complete")
    path = tmp_path / claim.staging_key
    path.write_bytes(b"content")
    events: list[str] = []

    class Db(_ConditionalDb):
        async def execute(self, statement):
            events.append("conditional-update")
            return _ScalarResult(claim.job_id)

        async def commit(self):
            events.append("queued-commit")

    real_fstat = import_uploads.os.fstat

    def fstat(fd):
        events.append("stat")
        return real_fstat(fd)

    real_sha256 = import_uploads._sha256_handle

    def sha256(_handle, _stop_event=None):
        events.append("sha256")
        return real_sha256(_handle, _stop_event)

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(import_uploads.os, "fstat", fstat)
    monkeypatch.setattr(import_uploads, "_sha256_handle", sha256)

    digest = asyncio.run(
        import_uploads.complete_claimed_upload(
            Db(returned_offset=None),
            claim=claim,
            config=_config(tmp_path),
        )
    )

    assert digest == hashlib.sha256(b"content").hexdigest()
    assert events == [
        "stat",
        "sha256",
        "stat",
        "conditional-update",
        "queued-commit",
    ]


def test_complete_hash_failure_releases_claim_without_queuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(offset=7, size=7, operation="complete")
    path = tmp_path / claim.staging_key
    path.write_bytes(b"content")
    db = _ConditionalDb(returned_offset=1)

    def fail_hash(_handle, _stop_event=None):
        raise OSError("disk read failed")

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(import_uploads, "_sha256_handle", fail_hash)

    with pytest.raises(OSError, match="disk read failed"):
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert len(db.execute_calls) == 1
    assert db.commit_count == 1


@pytest.mark.parametrize("failure_point", ["path", "open", "validation"])
def test_complete_path_failure_releases_claim_immediately(
    failure_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(offset=7, size=7, operation="complete")
    db = _ConditionalDb(returned_offset=claim.upload_offset)

    class BrokenPath:
        def open(self, *_args, **_kwargs):
            raise OSError("staging open failed")

    expected_code = "staging_size_mismatch"
    if failure_point == "path":
        def fail_staging_path(*_args):
            raise OSError("staging path failed")

        monkeypatch.setattr(import_uploads, "staging_path", fail_staging_path)
    elif failure_point == "open":
        monkeypatch.setattr(import_uploads, "staging_path", lambda *_args: BrokenPath())
    else:
        expected_code = "invalid_staging_key"

        def reject_staging_path(*_args):
            raise import_uploads.ImportUploadError(
                expected_code,
                "staging key is invalid",
            )

        monkeypatch.setattr(import_uploads, "staging_path", reject_staging_path)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == expected_code
    assert len(db.execute_calls) == 1
    assert db.commit_count == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "processing", "succeeded", "failed", "superseded"],
)
def test_complete_claim_is_idempotent_after_upload_has_finished(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        status=status,
        staging_key=f"{uuid.uuid4().hex}.upload",
        file_name="sample.txt",
        relative_path=None,
        content_type="text/plain",
        size_bytes=7,
        upload_offset=7,
    )

    async def no_lock(_db):
        return None

    async def get_job(*_args, **_kwargs):
        return job

    monkeypatch.setattr(import_uploads, "_lock_upload_claim_capacity", no_lock)
    monkeypatch.setattr(import_uploads, "get_owned_job", get_job)

    claim = asyncio.run(
        import_uploads.claim_upload_operation(
            SimpleNamespace(),
            library_id=uuid.uuid4(),
            job_id=job.id,
            user=SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
            operation="complete",
            config=SimpleNamespace(),
        )
    )

    assert claim.already_queued is True
    assert claim.owner_token is None


def test_complete_claim_does_not_treat_cancelled_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        status="cancelled",
        staging_key=f"{uuid.uuid4().hex}.upload",
        file_name="sample.txt",
        relative_path=None,
        content_type="text/plain",
        size_bytes=7,
        upload_offset=7,
    )

    async def no_lock(_db):
        return None

    async def get_job(*_args, **_kwargs):
        return job

    monkeypatch.setattr(import_uploads, "_lock_upload_claim_capacity", no_lock)
    monkeypatch.setattr(import_uploads, "get_owned_job", get_job)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.claim_upload_operation(
                SimpleNamespace(),
                library_id=uuid.uuid4(),
                job_id=job.id,
                user=SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
                operation="complete",
                config=SimpleNamespace(),
            )
        )

    assert exc_info.value.code == "upload_not_active"


def test_complete_does_not_parse_office_before_resource_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _minimal_word_document_cfb()
    base = _claim(offset=len(payload), size=len(payload), operation="complete")
    claim = import_uploads.UploadOperationClaim(
        job_id=base.job_id,
        owner_token=base.owner_token,
        operation=base.operation,
        staging_key=base.staging_key,
        file_name="legacy.doc",
        size_bytes=base.size_bytes,
        upload_offset=base.upload_offset,
        library_id=base.library_id,
        uploaded_by_user_id=base.uploaded_by_user_id,
        relative_path=base.relative_path,
        content_type=base.content_type,
    )
    (tmp_path / claim.staging_key).write_bytes(payload)
    events: list[str] = []

    class Db(_ConditionalDb):
        async def execute(self, statement):
            events.append("conditional-update")
            return _ScalarResult(claim.job_id)

    real_sha256 = import_uploads._sha256_handle

    def sha256(_handle, _stop_event=None):
        events.append("sha256")
        return real_sha256(_handle, _stop_event)

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(import_uploads, "_sha256_handle", sha256)

    asyncio.run(
        import_uploads.complete_claimed_upload(
            Db(returned_offset=None),
            claim=claim,
            config=_config(tmp_path),
        )
    )

    assert events == ["sha256", "conditional-update"]


def test_claim_keeps_cross_user_job_hidden() -> None:
    owner_id = uuid.uuid4()
    intruder = SimpleNamespace(id=uuid.uuid4(), is_superuser=False)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=owner_id,
    )

    class Result:
        def __init__(self, value=None):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Db:
        def __init__(self):
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return Result(job if self.calls == 2 else None)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.claim_upload_operation(
                Db(),
                library_id=job.library_id,
                job_id=job.id,
                user=intruder,
                operation="content",
                expected_offset=0,
                config=SimpleNamespace(
                    import_upload_claim_stale_seconds=300,
                    import_upload_retry_after_seconds=2,
                    import_upload_global_inflight_limit=10,
                    import_upload_user_inflight_limit=1,
                ),
            )
        )

    assert exc_info.value.code == "job_not_found"
    assert exc_info.value.status_code == 404


def test_cancel_does_not_remove_a_file_owned_by_live_upload() -> None:
    job = SimpleNamespace(
        status="uploading",
        worker_id=f"upload:{uuid.uuid4().hex}:content:{uuid.uuid4().hex}",
        claimed_at=datetime.now(timezone.utc),
        upload_offset=9,
    )

    class Db:
        async def flush(self):
            raise AssertionError("live upload must not be cancelled")

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.cancel_upload(
                Db(),
                job=job,
                config=SimpleNamespace(
                    import_upload_claim_stale_seconds=300,
                    import_upload_retry_after_seconds=2,
                ),
            )
        )

    assert exc_info.value.code == "upload_busy"
    assert exc_info.value.upload_offset == 9


def test_content_route_commits_claim_before_consuming_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim = SimpleNamespace(already_queued=False)

    class Db:
        async def commit(self):
            events.append("claim-commit")

        async def rollback(self):
            events.append("rollback")

    async def claim_operation(*_args, **_kwargs):
        events.append("claim")
        return claim

    async def append_content(_db, *, claim, body, config=None):
        assert events == ["claim", "claim-commit"]
        async for _chunk in body:
            events.append("body")
        return 4

    async def body():
        yield b"data"

    monkeypatch.setattr(import_uploads_api.import_uploads, "claim_upload_operation", claim_operation)
    monkeypatch.setattr(import_uploads_api.import_uploads, "append_claimed_content", append_content)
    response = Response()

    asyncio.run(
        import_uploads_api.append_import_content(
            uuid.uuid4(),
            SimpleNamespace(stream=lambda: body()),
            response,
            0,
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
            Db(),
        )
    )

    assert events == ["claim", "claim-commit", "body"]
    assert response.headers["Upload-Offset"] == "4"


def test_content_route_releases_committed_claim_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = SimpleNamespace(owner_token="upload:owner", already_queued=False)
    released = []

    class Db:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def claim_operation(*_args, **_kwargs):
        return claim

    async def append_content(*_args, **_kwargs):
        raise RuntimeError("request stream failed")

    async def release(_db, actual_claim):
        released.append(actual_claim)

    monkeypatch.setattr(import_uploads_api.import_uploads, "claim_upload_operation", claim_operation)
    monkeypatch.setattr(import_uploads_api.import_uploads, "append_claimed_content", append_content)
    monkeypatch.setattr(
        import_uploads_api.import_uploads,
        "release_upload_claim",
        release,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="request stream failed"):
        asyncio.run(
            import_uploads_api.append_import_content(
                uuid.uuid4(),
                SimpleNamespace(stream=lambda: None),
                Response(),
                0,
                SimpleNamespace(id=uuid.uuid4()),
                SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
                Db(),
            )
        )

    assert released == [claim]


def test_content_route_tolerates_service_and_adapter_releasing_the_same_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    db = _ConditionalDb(returned_offset=claim.upload_offset)

    async def claim_operation(*_args, **_kwargs):
        return claim

    async def append_content(actual_db, **_kwargs):
        await import_uploads.release_upload_claim(actual_db, claim)
        raise import_uploads.ImportUploadError(
            "staging_size_mismatch",
            "staging file is invalid",
            status_code=409,
            upload_offset=claim.upload_offset,
        )

    monkeypatch.setattr(import_uploads_api.import_uploads, "claim_upload_operation", claim_operation)
    monkeypatch.setattr(import_uploads_api.import_uploads, "append_claimed_content", append_content)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            import_uploads_api.append_import_content(
                claim.job_id,
                SimpleNamespace(stream=lambda: None),
                Response(),
                claim.upload_offset,
                SimpleNamespace(id=uuid.uuid4()),
                SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
                db,
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Upload-Offset": str(claim.upload_offset)}
    assert len(db.execute_calls) == 2
    assert db.commit_count == 3


def test_complete_retry_for_queued_job_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid.uuid4()
    queued_claim = SimpleNamespace(already_queued=True)
    queued_job = SimpleNamespace(id=job_id)
    complete_calls = 0

    class Db:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def claim_operation(*_args, **_kwargs):
        return queued_claim

    async def get_job(*_args, **_kwargs):
        return queued_job

    async def complete(*_args, **_kwargs):
        nonlocal complete_calls
        complete_calls += 1

    async def projection(_db, job):
        return {"id": job.id, "status": "queued"}

    monkeypatch.setattr(import_uploads_api.import_uploads, "claim_upload_operation", claim_operation)
    monkeypatch.setattr(import_uploads_api.import_uploads, "get_owned_job", get_job)
    monkeypatch.setattr(import_uploads_api.import_uploads, "complete_claimed_upload", complete)
    monkeypatch.setattr(import_uploads_api.import_uploads, "job_projection", projection)

    result = asyncio.run(
        import_uploads_api.complete_import_session(
            job_id,
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4(), is_superuser=False),
            Db(),
        )
    )

    assert result == {"id": job_id, "status": "queued"}
    assert complete_calls == 0


def test_active_claim_heartbeat_renews_during_slow_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewals = 0
    claim = _claim()
    config = SimpleNamespace(
        import_upload_claim_heartbeat_seconds=0.001,
        import_upload_retry_after_seconds=2,
    )

    async def renew(_claim):
        nonlocal renewals
        renewals += 1
        return True

    monkeypatch.setattr(import_uploads, "_renew_upload_claim", renew)

    async def exercise():
        async with import_uploads.keep_upload_claim_alive(claim, config=config) as lease:
            await asyncio.sleep(0.01)
            lease.ensure_current()

    asyncio.run(exercise())

    assert renewals >= 1


def test_failed_heartbeat_cancels_slow_body_and_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    (tmp_path / claim.staging_key).write_bytes(b"old!")
    db = _ConditionalDb(returned_offset=1)
    config = _config(tmp_path)
    config.import_upload_claim_heartbeat_seconds = 0.001

    async def lose_claim(_claim):
        return False

    async def slow_body():
        await asyncio.sleep(60)
        yield b"new"

    monkeypatch.setattr(import_uploads, "_renew_upload_claim", lose_claim)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            asyncio.wait_for(
                import_uploads.append_claimed_content(
                    db,
                    claim=claim,
                    body=slow_body(),
                    config=config,
                ),
                timeout=1,
            )
        )

    assert exc_info.value.code == "upload_claim_lost"
    assert db.commit_count == 1
