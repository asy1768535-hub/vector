from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.api import import_uploads as import_uploads_api
from app.services import import_staging_cleanup, import_uploads
from app.workers import importer


LIBRARY_ID = uuid.uuid4()


class _Result:
    def __init__(self, *, rows=(), scalar=0):
        self._rows = list(rows)
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _Db:
    def __init__(self, *results, commit_error=None):
        self.results = iter(results)
        self.statements = []
        self.added = []
        self.events = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.commit_error = commit_error

    async def execute(self, statement, params=None):
        self.events.append("execute")
        self.statements.append((statement, params))
        return next(self.results)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.events.append("flush")
        self.flush_count += 1

    async def commit(self):
        self.events.append("commit")
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.events.append("rollback")
        self.rollback_count += 1


class _PgBind:
    class dialect:
        name = "postgresql"


class _SqliteBind:
    class dialect:
        name = "sqlite"


class _UnknownBind:
    class dialect:
        name = "mysql"


class _SqliteDb(_Db):
    def get_bind(self):
        return _SqliteBind()


class _PgDb(_Db):
    def get_bind(self):
        return _PgBind()


class _UnknownDb(_Db):
    def get_bind(self):
        return _UnknownBind()


class _NoDialectDb(_Db):
    def get_bind(self):
        raise AttributeError("test fake has no dialect")


def _config(tmp_path: Path, **overrides) -> Settings:
    values = {
        "import_staging_dir": str(tmp_path),
        "import_staging_retention_seconds": 60,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _key(seed: int) -> str:
    return f"{seed:032x}.upload"


def _job(key: str, *, size: int, status: str = "uploading"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        staging_key=key,
        size_bytes=size,
        status=status,
        attempt_count=0,
        current_stage="uploading",
        finished_at=None,
        updated_at=None,
        library_id=LIBRARY_ID,
    )


def test_quota_sums_every_status_that_retains_a_staging_file(monkeypatch):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 100)
    db = _SqliteDb(_Result(scalar=90))

    asyncio.run(
        import_uploads._check_staging_quota(
            db,
            library_id=LIBRARY_ID,
            requested_bytes=10,
        )
    )
    statement = db.statements[0][0]
    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled).lower()
    assert "case when" in sql
    assert "lower(document_import_jobs.file_name) like" in sql
    assert "%.doc" in sql
    assert "* 2" in sql
    for status in ("uploading", "queued", "processing", "failed"):
        assert status in sql
    for stage in ("validating", "parsing", "chunking"):
        assert stage in sql
    assert "embedding" not in sql
    assert "graph" not in sql
    assert "succeeded" not in sql
    assert "cancelled" in sql
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING.lower() in sql
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_COMPLETE.lower() not in sql

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads._check_staging_quota(
                _SqliteDb(_Result(scalar=90)),
                library_id=LIBRARY_ID,
                requested_bytes=11,
            )
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "staging_quota_exceeded"


def test_cancel_upload_defers_file_removal_until_after_commit(tmp_path):
    config = _config(tmp_path)
    job = _job(_key(12), size=4)
    path = tmp_path / job.staging_key
    path.write_bytes(b"data")

    staging_key = asyncio.run(
        import_uploads.cancel_upload(_Db(), job=job, config=config)
    )

    assert staging_key == job.staging_key
    assert job.status == "cancelled"
    assert path.exists()


def test_cancel_api_commit_failure_never_removes_staging(monkeypatch):
    job = _job(_key(13), size=4)
    db = _Db(commit_error=RuntimeError("commit failed"))
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(
        import_uploads_api.import_uploads,
        "get_owned_job",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(
        import_uploads_api.import_uploads,
        "cancel_upload",
        AsyncMock(return_value=job.staging_key),
    )
    monkeypatch.setattr(import_uploads_api.import_uploads, "remove_staging_file", remove)

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(
            import_uploads_api.cancel_import_session(
                job.id,
                lib=SimpleNamespace(id=LIBRARY_ID),
                user=SimpleNamespace(id=uuid.uuid4(), is_superuser=True),
                db=db,
            )
        )

    remove.assert_not_awaited()


def test_quota_rejection_happens_before_job_is_added(monkeypatch):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 10)
    db = _SqliteDb(_Result(rows=()), _Result(scalar=10))
    payload = SimpleNamespace(
        size_bytes=1,
        file_name="new.txt",
        relative_path=None,
        batch_id=uuid.uuid4(),
        content_type="text/plain",
        last_modified_millis=None,
        external_id=None,
        replace_document_id=None,
        security_level=None,
        graph_extraction_requested=False,
    )
    library = SimpleNamespace(id=LIBRARY_ID)
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.create_session(
                db,
                library=library,
                user=user,
                payload=payload,
            )
        )
    assert exc_info.value.status_code == 429
    assert db.added == []


def test_create_session_does_not_leave_a_precommit_staging_file(monkeypatch, tmp_path):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 100)
    config = _config(tmp_path)
    db = _SqliteDb(_Result(rows=()), _Result(scalar=0), _Result(scalar=0))
    payload = SimpleNamespace(
        size_bytes=1,
        file_name="new.txt",
        relative_path=None,
        batch_id=uuid.uuid4(),
        content_type="text/plain",
        last_modified_millis=None,
        external_id=None,
        replace_document_id=None,
        security_level=None,
        graph_extraction_requested=False,
    )

    job = asyncio.run(
        import_uploads.create_session(
            db,
            library=SimpleNamespace(id=LIBRARY_ID),
            user=SimpleNamespace(id=uuid.uuid4()),
            payload=payload,
            config=config,
        )
    )

    assert not (tmp_path / job.staging_key).exists()


def test_postgres_quota_uses_transaction_advisory_lock(monkeypatch):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 100)
    db = _PgDb(_Result(scalar=0), _Result(scalar=0))

    asyncio.run(
        import_uploads._check_staging_quota(
            db,
            library_id=LIBRARY_ID,
            requested_bytes=1,
        )
    )
    lock_sql = str(db.statements[0][0]).lower()
    assert "pg_advisory_xact_lock" in lock_sql
    assert db.statements[0][1]["lock_key"] == import_uploads._staging_advisory_lock_key(LIBRARY_ID)


def test_missing_dialect_still_emits_advisory_sql_for_fakes(monkeypatch):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 100)
    db = _NoDialectDb(_Result(scalar=0), _Result(scalar=0))

    asyncio.run(
        import_uploads._check_staging_quota(
            db,
            library_id=LIBRARY_ID,
            requested_bytes=1,
        )
    )
    assert "pg_advisory_xact_lock" in str(db.statements[0][0]).lower()


def test_unknown_production_dialect_fails_closed(monkeypatch):
    monkeypatch.setattr(import_uploads, "STAGING_LIBRARY_MAX_BYTES", 100)
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads._check_staging_quota(
                _UnknownDb(),
                library_id=LIBRARY_ID,
                requested_bytes=1,
            )
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "staging_quota_unavailable"


def test_cleanup_uses_bounded_queries_and_only_locks_uploading(tmp_path):
    config = _config(tmp_path)
    now = import_staging_cleanup.datetime.now(import_staging_cleanup.timezone.utc)
    stale = _job(_key(1), size=3)
    stale.updated_at = now - import_staging_cleanup.timedelta(seconds=120)
    stale_path = tmp_path / stale.staging_key
    stale_path.write_bytes(b"old")
    db = _Db(_Result(rows=[stale]))

    transitioned, removed = asyncio.run(
        import_staging_cleanup.cleanup_staging(
            db,
            config=config,
            batch_size=256,
            now=now,
        )
    )

    assert (transitioned, removed) == (1, (stale.staging_key,))
    assert stale.status == "cancelled"
    assert stale.last_error == import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING
    assert stale_path.exists()
    assert db.flush_count == 1
    assert len(db.statements) == 1
    stale_stmt = db.statements[0][0]
    assert "LIMIT" in str(stale_stmt.compile()).upper()
    assert stale_stmt._limit_clause.value == 256
    assert stale_stmt._for_update_arg.skip_locked is True
    sql = str(stale_stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "uploading" in sql
    assert "failed" not in sql
    assert "iterdir" not in str(stale_stmt).lower()


def test_pending_expired_cleanup_retries_after_unlink_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    pending = _job(_key(18), size=3, status="cancelled")
    pending.last_error = import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING
    outcomes = iter(("failed", "removed"))

    async def unlink(*_args):
        return next(outcomes)

    monkeypatch.setattr(import_staging_cleanup, "_unlink_staging_key", unlink)

    first_db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))
    assert asyncio.run(
        import_staging_cleanup.reconcile_pending_expired_cleanups(
            first_db,
            config=_config(tmp_path),
        )
    ) == (0, 0)
    assert first_db.events == ["execute", "commit", "execute", "commit"]
    assert "expired upload staging cleanup failed" in caplog.text
    retry_update = first_db.statements[1][0]
    retry_sql = str(
        retry_update.compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "updated_at=" in retry_sql.replace(" ", "")
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_COMPLETE.lower() not in retry_sql

    second_db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))
    assert asyncio.run(
        import_staging_cleanup.reconcile_pending_expired_cleanups(
            second_db,
            config=_config(tmp_path),
        )
    ) == (1, 1)
    assert second_db.events == ["execute", "commit", "execute", "commit"]
    query_sql = str(
        second_db.statements[0][0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "cancelled" in query_sql
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING.lower() in query_sql
    update_sql = str(
        second_db.statements[1][0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING.lower() in update_sql
    assert import_uploads.EXPIRED_UPLOAD_CLEANUP_COMPLETE.lower() in update_sql


def test_pending_preflight_cleanup_is_reconciled_after_file_disappears(tmp_path):
    config = _config(tmp_path)
    pending = _job(_key(15), size=3, status="failed")
    pending.current_stage = "completed"
    pending.last_error = (
        "upload_preflight:v1:cleanup_pending:encrypted_office_file:"
        "Office file is encrypted"
    )
    db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))

    reconciled, removed = asyncio.run(
        import_staging_cleanup.reconcile_pending_preflight_cleanups(
            db,
            config=config,
        )
    )

    assert (reconciled, removed) == (1, 0)
    assert db.events == ["execute", "commit", "execute", "commit"]
    query = db.statements[0][0]
    assert query._limit_clause.value == import_staging_cleanup.MAX_CLEANUP_BATCH
    assert query._for_update_arg.skip_locked is True
    query_sql = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "failed" in query_sql
    assert "completed" in query_sql
    assert "upload_preflight:v1:cleanup_pending:%" in query_sql
    update_params = db.statements[1][0].compile().params
    assert any(
        isinstance(value, str)
        and value.startswith(
            "upload_preflight:v1:cleanup_complete:encrypted_office_file:"
        )
        for value in update_params.values()
    )


def test_pending_preflight_cleanup_stays_pending_when_unlink_fails(
    tmp_path,
    monkeypatch,
):
    pending = _job(_key(16), size=3, status="failed")
    pending.current_stage = "completed"
    pending.last_error = (
        "upload_preflight:v1:cleanup_pending:encrypted_office_file:"
        "Office file is encrypted"
    )
    db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))

    async def fail_unlink(*_args):
        return "failed"

    monkeypatch.setattr(import_staging_cleanup, "_unlink_staging_key", fail_unlink)

    assert asyncio.run(
        import_staging_cleanup.reconcile_pending_preflight_cleanups(
            db,
            config=_config(tmp_path),
        )
    ) == (0, 0)
    assert db.events == ["execute", "commit", "execute", "commit"]
    retry_update = db.statements[1][0]
    retry_sql = str(
        retry_update.compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "updated_at=" in retry_sql.replace(" ", "")
    assert "cleanup_complete" not in retry_sql


def test_pending_preflight_cleanup_does_not_overwrite_changed_marker(tmp_path):
    pending = _job(_key(17), size=3, status="failed")
    pending.current_stage = "completed"
    pending.last_error = (
        "upload_preflight:v1:cleanup_pending:encrypted_office_file:"
        "Office file is encrypted"
    )
    db = _Db(_Result(rows=[pending]), _Result(scalar=None))

    assert asyncio.run(
        import_staging_cleanup.reconcile_pending_preflight_cleanups(
            db,
            config=_config(tmp_path),
        )
    ) == (0, 0)
    assert db.events == ["execute", "commit", "execute", "commit"]


def test_pending_preflight_cleanup_retries_after_cas_commit_failure(tmp_path):
    pending = _job(_key(19), size=3, status="failed")
    pending.current_stage = "completed"
    pending.last_error = (
        "upload_preflight:v1:cleanup_pending:encrypted_office_file:"
        "Office file is encrypted"
    )
    path = tmp_path / pending.staging_key
    path.write_bytes(b"old")
    first_db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))

    async def fail_second_commit():
        first_db.events.append("commit")
        first_db.commit_count += 1
        if first_db.commit_count == 2:
            raise RuntimeError("commit failed")

    first_db.commit = fail_second_commit
    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(
            import_staging_cleanup.reconcile_pending_preflight_cleanups(
                first_db,
                config=_config(tmp_path),
            )
        )
    assert not path.exists()

    retry_db = _Db(_Result(rows=[pending]), _Result(scalar=pending.id))
    assert asyncio.run(
        import_staging_cleanup.reconcile_pending_preflight_cleanups(
            retry_db,
            config=_config(tmp_path),
        )
    ) == (1, 0)


def test_worker_runs_preflight_reconciliation_in_the_bounded_cleanup_cycle(
    monkeypatch,
    caplog,
):
    async def cleanup(_db):
        return 0, ()

    async def reconcile(_db, *, config):
        return 1, 0

    async def reconcile_expired(_db, *, config):
        return 0, 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile_expired,
    )
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        reconcile,
    )
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    caplog.set_level("INFO", logger=importer.log.name)

    assert asyncio.run(importer._maybe_cleanup_staging(_Db())) == (1, 0)
    assert "preflight_reconciled=1" in caplog.text


def test_worker_reports_prior_expired_cleanup_reconciliation(monkeypatch, caplog):
    async def cleanup(_db):
        return 0, ()

    async def reconcile_expired(_db, *, config):
        return 1, 0

    async def reconcile_preflight(_db, *, config):
        return 0, 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile_expired,
    )
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        reconcile_preflight,
    )
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    caplog.set_level("INFO", logger=importer.log.name)

    assert asyncio.run(importer._maybe_cleanup_staging(_Db())) == (1, 0)
    assert "expired_reconciled=1" in caplog.text


def test_unlink_reports_missing_without_counting_removed(tmp_path):
    config = _config(tmp_path)
    assert asyncio.run(
        import_staging_cleanup._unlink_staging_key(_key(4), config)
    ) == "missing"


def test_unlink_reports_removed_for_existing_file(tmp_path):
    config = _config(tmp_path)
    path = tmp_path / _key(5)
    path.write_bytes(b"content")

    assert asyncio.run(
        import_staging_cleanup._unlink_staging_key(path.name, config)
    ) == "removed"
    assert not path.exists()


def test_cleanup_rejects_unsafe_staging_key_without_leaving_root(tmp_path):
    config = _config(tmp_path)
    now = import_staging_cleanup.datetime.now(import_staging_cleanup.timezone.utc)
    outside = tmp_path.parent / "outside.upload"
    outside.write_bytes(b"keep")
    stale = _job("../outside.upload", size=3)
    stale.updated_at = now - import_staging_cleanup.timedelta(seconds=120)

    transitioned, removed = asyncio.run(
        import_staging_cleanup.cleanup_staging(
            _Db(_Result(rows=[stale])),
            config=config,
            now=now,
        )
    )

    assert (transitioned, removed) == (1, (stale.staging_key,))
    assert outside.exists()
    assert (
        asyncio.run(
            import_staging_cleanup._unlink_staging_key(stale.staging_key, config)
        )
        == "invalid"
    )


def test_cleanup_defers_failed_and_orphan_terminal_files(tmp_path):
    config = _config(tmp_path)
    now = import_staging_cleanup.datetime.now(import_staging_cleanup.timezone.utc)
    failed_key = _key(6)
    orphan_key = _key(7)
    failed_path = tmp_path / failed_key
    orphan_path = tmp_path / orphan_key
    failed_path.write_bytes(b"retry")
    orphan_path.write_bytes(b"orphan")
    failed = _job(failed_key, size=5, status="failed")
    failed.updated_at = now - import_staging_cleanup.timedelta(seconds=120)

    db = _Db(_Result(rows=[]))
    transitioned, removed = asyncio.run(
        import_staging_cleanup.cleanup_staging(
            db,
            config=config,
            now=now,
        )
    )

    assert (transitioned, removed) == (0, ())
    assert failed.status == "failed"
    assert failed_path.exists()
    assert orphan_path.exists()
    assert len(db.statements) == 1
    sql = str(db.statements[0][0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "failed" not in sql

    asyncio.run(import_uploads.retry_job(_Db(), job=failed, config=config))
    assert failed.status == "queued"
    assert failed_path.exists()


def test_cleanup_rejects_batch_over_256_without_query(tmp_path):
    db = _Db()
    with pytest.raises(import_uploads.ImportUploadError):
        asyncio.run(
            import_staging_cleanup.cleanup_staging(
                db,
                config=_config(tmp_path),
                batch_size=257,
            )
        )
    assert db.statements == []


def test_failed_staging_survives_retention_and_can_retry(tmp_path):
    config = _config(tmp_path)
    failed_key = _key(3)
    failed_path = tmp_path / failed_key
    failed_path.write_bytes(b"abc")
    failed_job = _job(failed_key, size=3, status="failed")
    failed_job.finished_at = import_staging_cleanup.datetime.now(
        import_staging_cleanup.timezone.utc
    ) - import_staging_cleanup.timedelta(seconds=120)

    db = _SqliteDb(_Result(rows=[]))
    asyncio.run(
        import_staging_cleanup.cleanup_staging(
            db,
            config=config,
            now=import_staging_cleanup.datetime.now(
                import_staging_cleanup.timezone.utc
            ),
        )
    )
    assert len(db.statements) == 1
    terminal_sql = str(db.statements[0][0].compile()).lower()
    assert "failed" not in terminal_sql
    assert failed_path.exists()

    asyncio.run(import_uploads.retry_job(_Db(), job=failed_job, config=config))
    assert failed_job.status == "queued"
    assert failed_job.current_stage == "queued"
    assert failed_path.exists()


def _without_pending_cleanups(monkeypatch):
    async def reconcile(_db, *, config):
        return 0, 0

    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile,
    )
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        reconcile,
    )


def test_worker_cleanup_is_monotonic_throttled(monkeypatch):
    calls = []

    async def cleanup(_db):
        calls.append(True)
        return 0, ()

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    _without_pending_cleanups(monkeypatch)
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    monkeypatch.setattr(importer, "STAGING_CLEANUP_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(importer.time, "monotonic", lambda: 100.0)

    assert asyncio.run(importer._maybe_cleanup_staging(_Db())) == (0, 0)
    assert asyncio.run(importer._maybe_cleanup_staging(_Db())) == (0, 0)
    assert len(calls) == 1


def test_worker_cleanup_failure_does_not_escape(monkeypatch):
    async def cleanup(_db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    db = _Db()

    assert asyncio.run(importer._maybe_cleanup_staging(db)) == (0, 0)
    assert db.rollback_count == 1


def test_worker_commits_before_unlink(monkeypatch):
    key = _key(8)
    db = _Db()

    async def cleanup(db):
        db.events.append("cleanup")
        return 1, (key,)

    async def unlink(staging_key, config):
        db.events.append(("unlink", staging_key))
        return "removed"

    async def reconcile_expired(_db, *, config):
        db.events.append("reconcile")
        result = await unlink(key, config)
        return 1, 1 if result == "removed" else 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile_expired,
    )
    async def no_preflight(_db, *, config):
        return 0, 0

    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        no_preflight,
    )
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)

    assert asyncio.run(importer._maybe_cleanup_staging(db)) == (2, 1)
    assert db.events == ["cleanup", "commit", "reconcile", ("unlink", key)]


def test_worker_commit_failure_never_unlinks(monkeypatch):
    key = _key(9)
    unlink_calls = []

    async def cleanup(_db):
        return 1, (key,)

    async def unlink(*args):
        unlink_calls.append(args)
        return "removed"

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(importer.import_staging_cleanup, "_unlink_staging_key", unlink)
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    db = _Db(commit_error=RuntimeError("commit failed"))

    assert asyncio.run(importer._maybe_cleanup_staging(db)) == (0, 0)
    assert unlink_calls == []
    assert db.events == ["commit", "rollback"]


def test_worker_missing_file_is_not_counted_as_removed(monkeypatch):
    key = _key(10)

    async def cleanup(_db):
        return 1, (key,)

    async def reconcile_expired(_db, *, config):
        return 1, 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile_expired,
    )
    async def no_preflight(_db, *, config):
        return 0, 0

    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        no_preflight,
    )
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)

    db = _Db()
    assert asyncio.run(importer._maybe_cleanup_staging(db)) == (2, 0)
    assert db.commit_count == 1
    assert db.rollback_count == 0


def test_worker_pending_cleanup_failure_does_not_rollback_transition(monkeypatch):
    key = _key(11)

    async def cleanup(_db):
        return 1, (key,)

    async def reconcile_expired(_db, *, config):
        return 0, 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_expired_cleanups",
        reconcile_expired,
    )
    async def no_preflight(_db, *, config):
        return 0, 0

    monkeypatch.setattr(
        importer.import_staging_cleanup,
        "reconcile_pending_preflight_cleanups",
        no_preflight,
    )
    monkeypatch.setattr(importer, "_last_staging_cleanup_monotonic", None)
    db = _Db()

    assert asyncio.run(importer._maybe_cleanup_staging(db)) == (1, 0)
    assert db.commit_count == 1
    assert db.rollback_count == 0


def test_import_transaction_removes_only_after_successful_exit(monkeypatch):
    events = []

    class _Transaction:
        async def __aenter__(self):
            events.append("begin")

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("commit")

    class _TransactionDb:
        def begin(self):
            return _Transaction()

    async def remove(key):
        events.append(("unlink", key))
        return True

    monkeypatch.setattr(importer, "remove_staging_file", remove)
    key = _key(14)

    async def run():
        async with importer._import_transaction(_TransactionDb(), key, lambda: True):
            events.append("write")

    asyncio.run(run())
    assert events == ["begin", "write", "commit", ("unlink", key)]


def test_import_transaction_failure_never_removes(monkeypatch):
    remove = AsyncMock(return_value=True)

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            raise RuntimeError("commit failed")

    class _TransactionDb:
        def begin(self):
            return _Transaction()

    monkeypatch.setattr(importer, "remove_staging_file", remove)

    async def run():
        async with importer._import_transaction(
            _TransactionDb(), _key(15), lambda: True,
        ):
            pass

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(run())
    remove.assert_not_awaited()
