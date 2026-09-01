from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.import_parsing import ParsedImport
from app.workers import importer


class _StopAfterBoundaryProof(RuntimeError):
    pass


class _SessionContext:
    def __init__(self, db) -> None:
        self.db = db

    async def __aenter__(self):
        self.db.active = True
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        self.db.active = False
        return False


class _TransactionContext:
    def __init__(self, state: dict[str, bool]) -> None:
        self.state = state

    async def __aenter__(self):
        self.state["write_transaction"] = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.state["write_transaction"] = False
        return False


class _WriteResult:
    def __init__(self, job) -> None:
        self.job = job

    def scalars(self):
        return self

    def first(self):
        return self.job


def _install_worker_fakes(monkeypatch, tmp_path: Path):
    job_id = uuid.uuid4()
    library_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    source = tmp_path / "source.txt"
    source.write_text("value", encoding="utf-8")
    job = SimpleNamespace(
        id=job_id,
        library_id=library_id,
        file_name="source.txt",
        content_type="text/plain",
        size_bytes=source.stat().st_size,
        sha256="b" * 64,
        staging_key="staged/source.txt",
        external_id=None,
        requested_by_user_id=None,
        security_level=None,
        graph_extraction_requested=False,
        replace_document_id=None,
        status="processing",
        result_operation=None,
        document_id=None,
        document_revision_id=None,
        embedding_job_id=None,
        current_stage="parsing",
        worker_id="fixture-worker",
        claimed_at=None,
        finished_at=None,
        conversion_sha256=None,
        converter_version=None,
    )
    library = SimpleNamespace(
        id=library_id,
        deleted_at=None,
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=False,
        docx_table_aware=True,
    )
    parsed = ParsedImport(
        normalized_text="value",
        chunks=[{"text": "value", "source_start": 0, "source_end": 5}],
        splitter_name="text",
        segments=[],
    )
    document = SimpleNamespace(
        id=uuid.uuid4(),
        current_revision=1,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        content_hash="different",
        source_path=None,
        folder_id=None,
    )
    embedding_job = SimpleNamespace(id=uuid.uuid4(), document_revision_id=revision_id)
    transaction_state = {"write_transaction": False}

    class _ReadDb:
        active = False

        async def get(self, model, object_id):
            if model is importer.DocumentImportJob:
                return job
            if model is importer.Library:
                return library
            return None

    class _WriteDb:
        active = False

        def begin(self):
            return _TransactionContext(transaction_state)

        async def execute(self, statement):
            return _WriteResult(job)

        async def get(self, model, object_id):
            if model is importer.Library:
                return library
            return None

    read_db = _ReadDb()
    write_db = _WriteDb()
    sessions = iter([read_db, write_db])
    monkeypatch.setattr(
        importer,
        "async_session_factory",
        lambda: _SessionContext(next(sessions)),
    )
    monkeypatch.setattr(importer, "staging_path", lambda _key: source)
    monkeypatch.setattr(importer, "source_path_for_job", lambda _job: None)
    monkeypatch.setattr(importer, "folder_path_for_job", lambda _job: None)
    monkeypatch.setattr(importer, "_set_stage", AsyncMock())
    monkeypatch.setattr(importer, "_apply_import_revision_scope", AsyncMock())
    monkeypatch.setattr(
        importer.folders_service,
        "ensure_folder_path",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        importer.ingest_service,
        "ingest_text",
        AsyncMock(return_value=(document, embedding_job, 1, False)),
    )
    return SimpleNamespace(
        job_id=job_id,
        library=library,
        parsed=parsed,
        read_db=read_db,
        transaction_state=transaction_state,
        source=source,
    )


def test_importer_releases_read_session_before_staging_io(monkeypatch, tmp_path):
    context = _install_worker_fakes(monkeypatch, tmp_path)

    async def stop_at_staging(_path, _expected_size):
        assert context.read_db.active is False
        raise _StopAfterBoundaryProof

    monkeypatch.setattr(importer, "_wait_for_staging_file", stop_at_staging)

    with pytest.raises(_StopAfterBoundaryProof):
        asyncio.run(importer._process_claimed_job(context.job_id))


def test_importer_parses_with_detached_immutable_library_snapshot(monkeypatch, tmp_path):
    context = _install_worker_fakes(monkeypatch, tmp_path)

    def stop_at_parse(_path, parser_config, **_kwargs):
        assert context.read_db.active is False
        assert parser_config is not context.library
        assert (
            parser_config.chunk_size,
            parser_config.chunk_overlap,
            parser_config.ocr_enabled,
            parser_config.docx_table_aware,
        ) == (800, 80, False, True)
        raise _StopAfterBoundaryProof

    monkeypatch.setattr(importer, "parse_import_file", stop_at_parse)

    with pytest.raises(_StopAfterBoundaryProof):
        asyncio.run(importer._process_claimed_job(context.job_id))


def test_importer_prepares_managed_file_before_write_transaction(monkeypatch, tmp_path):
    context = _install_worker_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    monkeypatch.setattr(importer, "parse_import_file", lambda *_args, **_kwargs: context.parsed)
    monkeypatch.setattr(importer, "build_object_storage_adapter", lambda: object())

    async def stop_at_prepare(**_kwargs):
        assert context.transaction_state["write_transaction"] is False
        raise _StopAfterBoundaryProof

    monkeypatch.setattr(importer, "prepare_managed_file_path", stop_at_prepare)

    with pytest.raises(_StopAfterBoundaryProof):
        asyncio.run(importer._process_claimed_job(context.job_id))


def test_importer_copies_legacy_file_before_write_transaction(monkeypatch, tmp_path):
    context = _install_worker_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", False)
    monkeypatch.setattr(importer, "parse_import_file", lambda *_args, **_kwargs: context.parsed)
    monkeypatch.setattr(importer, "_legacy_files_root", lambda: tmp_path / "legacy")

    def stop_at_copy(_source, _destination):
        assert context.transaction_state["write_transaction"] is False
        raise _StopAfterBoundaryProof

    monkeypatch.setattr(importer.shutil, "copyfile", stop_at_copy)

    with pytest.raises(_StopAfterBoundaryProof):
        asyncio.run(importer._process_claimed_job(context.job_id))


def test_importer_removes_prepared_legacy_file_when_stage_update_fails(
    monkeypatch,
    tmp_path,
):
    context = _install_worker_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        importer,
        "parse_import_file",
        lambda *_args, **_kwargs: context.parsed,
    )
    prepared_path = tmp_path / "prepared.tmp"
    prepared_path.write_bytes(b"value")
    monkeypatch.setattr(
        importer,
        "_prepare_revision_file",
        AsyncMock(return_value=importer._PreparedLegacyFile(prepared_path)),
    )

    async def set_stage(_job_id, stage):
        if stage == "chunking":
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(importer, "_set_stage", set_stage)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(importer._process_claimed_job(context.job_id))

    assert not prepared_path.exists()


def test_run_once_processes_claimed_jobs_sequentially(monkeypatch):
    job_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    state = SimpleNamespace(active=0, maximum=0, started=[])
    db = SimpleNamespace(active=False)
    monkeypatch.setattr(importer, "async_session_factory", lambda: _SessionContext(db))
    monkeypatch.setattr(importer, "_worker_id", lambda: "worker-1")
    monkeypatch.setattr(importer, "_reset_stale_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(importer, "_maybe_cleanup_staging", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(importer, "_claim_jobs", AsyncMock(return_value=job_ids))

    async def process(job_id):
        state.active += 1
        state.maximum = max(state.maximum, state.active)
        state.started.append(job_id)
        try:
            await asyncio.sleep(0)
            if job_id == job_ids[1]:
                raise RuntimeError("invalid import")
        finally:
            state.active -= 1

    mark_failed = AsyncMock()
    monkeypatch.setattr(importer, "_process_claimed_job", process)
    monkeypatch.setattr(importer, "_mark_failed", mark_failed)

    assert asyncio.run(importer.run_once()) == len(job_ids)
    assert state.maximum == 1
    assert state.started == job_ids
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.args[0] == job_ids[1]
