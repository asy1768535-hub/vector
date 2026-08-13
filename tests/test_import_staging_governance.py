from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
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


class _Db:
    def __init__(self, *results):
        self.results = iter(results)
        self.statements = []
        self.added = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        return next(self.results)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
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


def test_quota_sums_only_active_uploading_and_queued(monkeypatch):
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
    assert "sum(document_import_jobs.size_bytes)" in sql
    assert "uploading" in sql and "queued" in sql
    assert "succeeded" not in sql
    assert "failed" not in sql

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
    terminal_key = _key(2)
    (tmp_path / terminal_key).write_bytes(b"old")
    db = _Db(_Result(rows=[stale]), _Result(rows=[terminal_key]))

    transitioned, removed = asyncio.run(
        import_staging_cleanup.cleanup_staging(
            db,
            config=config,
            batch_size=256,
            now=now,
        )
    )

    assert (transitioned, removed) == (1, 1)
    assert stale.status == "cancelled"
    assert not (tmp_path / terminal_key).exists()
    stale_stmt = db.statements[0][0]
    terminal_stmt = db.statements[1][0]
    assert "LIMIT" in str(stale_stmt.compile()).upper()
    assert stale_stmt._limit_clause.value == 256
    assert stale_stmt._for_update_arg.skip_locked is True
    assert terminal_stmt._for_update_arg is None
    assert "iterdir" not in str(stale_stmt).lower()


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

    db = _SqliteDb(_Result(rows=[]), _Result(rows=[]))
    asyncio.run(
        import_staging_cleanup.cleanup_staging(
            db,
            config=config,
            now=import_staging_cleanup.datetime.now(
                import_staging_cleanup.timezone.utc
            ),
        )
    )
    terminal_sql = str(db.statements[1][0].compile()).lower()
    assert "failed" not in terminal_sql
    assert failed_path.exists()

    asyncio.run(import_uploads.retry_job(_Db(), job=failed_job, config=config))
    assert failed_job.status == "queued"
    assert failed_job.current_stage == "queued"
    assert failed_path.exists()


def test_worker_cleanup_is_monotonic_throttled(monkeypatch):
    calls = []

    async def cleanup(_db):
        calls.append(True)
        return 0, 0

    monkeypatch.setattr(importer.import_staging_cleanup, "cleanup_staging", cleanup)
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
