from __future__ import annotations

import hashlib
import importlib
import asyncio
import subprocess
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services import doc_conversion
from app.services import import_staging_cleanup, import_uploads


OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def _config(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        import_staging_dir=str(tmp_path),
        doc_converter_binary="soffice-test",
        doc_conversion_timeout_seconds=180,
    )


def _valid_doc_bytes() -> bytes:
    header = bytearray(512)
    header[:8] = OLE_MAGIC
    header[0x1C:0x1E] = b"\xfe\xff"
    header[0x1E:0x20] = (9).to_bytes(2, "little")
    return bytes(header)


def _write_docx(path: Path, text: str = "converted") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", f"<document>{text}</document>")


def test_converted_staging_path_is_deterministic_and_confined(tmp_path: Path) -> None:
    config = _config(tmp_path)

    path = doc_conversion.converted_staging_path("0" * 32 + ".upload", config)

    assert path == tmp_path.resolve() / ("0" * 32 + ".converted.docx")
    with pytest.raises(doc_conversion.DocConversionError):
        doc_conversion.converted_staging_path("../outside.upload", config)


def test_doc_source_validation_rejects_spoofed_truncated_and_oversized_files(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.upload"
    valid.write_bytes(_valid_doc_bytes())
    doc_conversion.validate_doc_source(valid, max_bytes=512)

    spoofed = tmp_path / "spoofed.upload"
    spoofed.write_bytes(b"not a Word document" * 40)
    with pytest.raises(doc_conversion.DocConversionError, match="OLE"):
        doc_conversion.validate_doc_source(spoofed, max_bytes=1024)

    truncated = tmp_path / "truncated.upload"
    truncated.write_bytes(OLE_MAGIC)
    with pytest.raises(doc_conversion.DocConversionError, match="truncated"):
        doc_conversion.validate_doc_source(truncated, max_bytes=1024)

    with pytest.raises(doc_conversion.DocConversionError, match="200"):
        doc_conversion.validate_doc_source(valid, max_bytes=200)


def test_converted_docx_validation_requires_word_document_part(tmp_path: Path) -> None:
    valid = tmp_path / "valid.docx"
    _write_docx(valid)
    doc_conversion.validate_converted_docx(valid)

    invalid = tmp_path / "invalid.docx"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
    with pytest.raises(doc_conversion.DocConversionError, match="DOCX"):
        doc_conversion.validate_converted_docx(invalid)

    with pytest.raises(doc_conversion.DocConversionError, match="256"):
        doc_conversion.validate_converted_docx(valid, max_bytes=256)


def test_convert_doc_uses_fixed_argv_and_publishes_deterministic_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    staging_key = "1" * 32 + ".upload"
    source = tmp_path / staging_key
    source.write_bytes(_valid_doc_bytes())
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        outdir = Path(argv[argv.index("--outdir") + 1])
        _write_docx(outdir / "source.docx", "legacy")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(doc_conversion.subprocess, "run", run)
    monkeypatch.setattr(
        doc_conversion,
        "libreoffice_version",
        lambda _config: "LibreOffice 24.2.0",
    )

    artifact = doc_conversion.convert_doc(source, staging_key, config=config)

    assert artifact.path == tmp_path.resolve() / ("1" * 32 + ".converted.docx")
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert artifact.converter_version == "LibreOffice 24.2.0"
    argv, kwargs = calls[0]
    assert argv[0] == "soffice-test"
    assert "--headless" in argv
    assert argv[argv.index("--convert-to") + 1].startswith("docx")
    assert argv[-1].endswith("source.doc")
    assert any(value.startswith("-env:UserInstallation=file:") for value in argv)
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 180


def test_convert_doc_timeout_does_not_publish_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    staging_key = "2" * 32 + ".upload"
    source = tmp_path / staging_key
    source.write_bytes(_valid_doc_bytes())

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="soffice-test", timeout=180)

    monkeypatch.setattr(doc_conversion.subprocess, "run", timeout)

    with pytest.raises(doc_conversion.DocConversionError, match="timed out"):
        doc_conversion.convert_doc(source, staging_key, config=config)
    assert not doc_conversion.converted_staging_path(staging_key, config).exists()


def test_convert_doc_version_failure_removes_the_published_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    staging_key = "6" * 32 + ".upload"
    source = tmp_path / staging_key
    source.write_bytes(_valid_doc_bytes())

    def run(argv, **_kwargs):
        outdir = Path(argv[argv.index("--outdir") + 1])
        _write_docx(outdir / "source.docx")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(doc_conversion.subprocess, "run", run)
    monkeypatch.setattr(
        doc_conversion,
        "libreoffice_version",
        lambda _config: (_ for _ in ()).throw(
            doc_conversion.DocConversionError("version unavailable")
        ),
    )

    with pytest.raises(doc_conversion.DocConversionError, match="version unavailable"):
        doc_conversion.convert_doc(source, staging_key, config=config)
    assert not doc_conversion.converted_staging_path(staging_key, config).exists()


def test_complete_upload_rejects_a_spoofed_doc_before_queueing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    staging_key = "3" * 32 + ".upload"
    (tmp_path / staging_key).write_bytes(b"x" * 512)
    job = SimpleNamespace(
        file_name="spoofed.doc",
        status="uploading",
        upload_offset=512,
        size_bytes=512,
        staging_key=staging_key,
        sha256=None,
        current_stage="uploading",
        upload_completed_at=None,
    )

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_upload(
                SimpleNamespace(flush=AsyncMock()),
                job=job,
                config=config,
            )
        )
    assert exc_info.value.code == "invalid_doc_file"
    assert job.status == "uploading"


def test_both_staging_cleanup_paths_remove_the_converted_docx(tmp_path: Path) -> None:
    config = _config(tmp_path)
    staging_key = "4" * 32 + ".upload"
    original = tmp_path / staging_key
    converted = doc_conversion.converted_staging_path(staging_key, config)
    original.write_bytes(_valid_doc_bytes())
    _write_docx(converted)

    assert asyncio.run(import_uploads.remove_staging_file(staging_key, config)) is True
    assert not original.exists()
    assert not converted.exists()

    original.write_bytes(_valid_doc_bytes())
    _write_docx(converted)
    assert asyncio.run(import_staging_cleanup._unlink_staging_key(staging_key, config)) == "removed"
    assert not original.exists()
    assert not converted.exists()


class _SqlResult:
    rowcount = 0

    def __iter__(self):
        return iter(())


class _SqlDb:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return _SqlResult()

    async def commit(self):
        return None


def test_converter_and_importer_claim_disjoint_doc_jobs() -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    converter_db = _SqlDb()
    importer_db = _SqlDb()

    asyncio.run(worker._claim_jobs(converter_db, "converter", 2))
    asyncio.run(importlib.import_module("app.workers.importer")._claim_jobs(importer_db, "importer", 4))

    converter_sql = converter_db.calls[0][0].lower()
    importer_sql = importer_db.calls[0][0].lower()
    assert "lower(file_name) like '%.doc'" in converter_sql
    assert "conversion_sha256 is null" in converter_sql
    assert "conversion_attempt_count" in converter_sql
    assert "lower(file_name) not like '%.doc'" in importer_sql
    assert "conversion_sha256 is not null" in importer_sql


def test_converter_stale_lease_recovery_is_bounded_by_conversion_attempts() -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    db = _SqlDb()

    asyncio.run(worker._reset_stale_jobs(db))

    sql = "\n".join(statement.lower() for statement, _params in db.calls)
    assert "current_stage = 'converting'" in sql
    assert "conversion_attempt_count >= :max_attempts" in sql
    assert "conversion_attempt_count < :max_attempts" in sql


class _SessionContext:
    def __init__(self, db) -> None:
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def test_converter_failure_does_not_cancel_sibling_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    job_ids = [uuid.uuid4() for _ in range(3)]
    completed = []

    async def process(job_id, worker_id):
        assert worker_id == "converter-1"
        if job_id == job_ids[1]:
            raise ValueError("bad DOC")
        completed.append(job_id)

    mark_failed = AsyncMock()
    monkeypatch.setattr(worker, "_process_claimed_job", process)
    monkeypatch.setattr(worker, "_mark_failed", mark_failed)

    asyncio.run(worker._process_claimed_jobs(job_ids, "converter-1"))

    assert completed == [job_ids[0], job_ids[2]]
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.args[0] == job_ids[1]
    assert mark_failed.await_args.args[1] == "converter-1"


def test_converter_failure_does_not_mutate_a_reassigned_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    job = SimpleNamespace(
        status="processing",
        current_stage="converting",
        worker_id="converter-2",
        conversion_attempt_count=1,
        finished_at=None,
        claimed_at=object(),
        last_error=None,
    )

    class _Db:
        async def get(self, _model, _object_id):
            return job

        commit = AsyncMock()

    db = _Db()
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(db))

    asyncio.run(worker._mark_failed(uuid.uuid4(), "converter-1", ValueError("bad DOC")))

    assert job.status == "processing"
    assert job.worker_id == "converter-2"
    assert job.last_error is None
    db.commit.assert_not_awaited()


def test_converter_run_once_claims_only_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    monkeypatch.setattr(worker.settings, "doc_converter_concurrency", 2)
    monkeypatch.setattr(worker, "_worker_id", lambda: "converter-1")
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(object()))
    monkeypatch.setattr(worker, "_reset_stale_jobs", AsyncMock(return_value=0))
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "_claim_jobs", claim)
    process = AsyncMock()
    monkeypatch.setattr(worker, "_process_claimed_jobs", process)

    assert asyncio.run(worker.run_once()) == 0
    assert claim.await_args.args[2] == 2
    assert claim.await_args.args[1] == "converter-1"
    process.assert_awaited_once_with([], "converter-1")


def test_converter_success_requeues_job_for_importer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    job_id = uuid.uuid4()
    source = tmp_path / ("5" * 32 + ".upload")
    source.write_bytes(_valid_doc_bytes())
    artifact_path = tmp_path / ("5" * 32 + ".converted.docx")
    _write_docx(artifact_path)
    artifact = doc_conversion.DocConversionArtifact(
        path=artifact_path,
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        converter_version="LibreOffice 24.2.0",
    )
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        current_stage="converting",
        staging_key=source.name,
        conversion_sha256=None,
        converter_version=None,
        worker_id="converter",
        claimed_at=object(),
        last_error=None,
    )

    class _ReadDb:
        async def get(self, _model, _object_id):
            return job

    class _WriteResult:
        def scalars(self):
            return self

        def first(self):
            return job

    class _WriteDb:
        async def execute(self, _statement):
            return _WriteResult()

        commit = AsyncMock()

    sessions = iter([_ReadDb(), _WriteDb()])
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(next(sessions)))
    monkeypatch.setattr(worker, "staging_path", lambda _key: source)
    monkeypatch.setattr(worker, "convert_doc", lambda *_args: artifact)

    asyncio.run(worker._process_claimed_job(job_id, "converter"))

    assert job.status == "queued"
    assert job.current_stage == "conversion_ready"
    assert job.conversion_sha256 == artifact.sha256
    assert job.converter_version == artifact.converter_version
    assert job.worker_id is None


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
def test_converter_discards_artifact_when_job_finishes_during_conversion(
    terminal_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    job_id = uuid.uuid4()
    source = tmp_path / ("6" * 32 + ".upload")
    source.write_bytes(_valid_doc_bytes())
    artifact_path = tmp_path / ("6" * 32 + ".converted.docx")
    _write_docx(artifact_path)
    artifact = doc_conversion.DocConversionArtifact(
        path=artifact_path,
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        converter_version="LibreOffice 24.2.0",
    )
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        current_stage="converting",
        staging_key=source.name,
        conversion_sha256=None,
        converter_version=None,
        worker_id="converter-1",
    )

    class _Db:
        async def get(self, _model, _object_id):
            return job

        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: job),
            )

        commit = AsyncMock()

    db = _Db()
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(db))
    monkeypatch.setattr(worker, "staging_path", lambda _key: source)

    def convert(*_args):
        job.status = terminal_status
        return artifact

    monkeypatch.setattr(worker, "convert_doc", convert)

    asyncio.run(worker._process_claimed_job(job_id, "converter-1"))

    assert not artifact_path.exists()
    assert job.conversion_sha256 is None
    db.commit.assert_not_awaited()


def test_converter_does_not_publish_after_lease_is_reassigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = importlib.import_module("app.workers.doc_converter")
    job_id = uuid.uuid4()
    source = tmp_path / ("7" * 32 + ".upload")
    source.write_bytes(_valid_doc_bytes())
    artifact_path = tmp_path / ("7" * 32 + ".converted.docx")
    _write_docx(artifact_path)
    artifact = doc_conversion.DocConversionArtifact(
        path=artifact_path,
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        converter_version="LibreOffice 24.2.0",
    )
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        current_stage="converting",
        staging_key=source.name,
        conversion_sha256=None,
        converter_version=None,
        worker_id="converter-1",
    )

    class _Db:
        async def get(self, _model, _object_id):
            return job

        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: job),
            )

        commit = AsyncMock()

    db = _Db()
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(db))
    monkeypatch.setattr(worker, "staging_path", lambda _key: source)

    def convert(*_args):
        job.worker_id = "converter-2"
        return artifact

    monkeypatch.setattr(worker, "convert_doc", convert)

    asyncio.run(worker._process_claimed_job(job_id, "converter-1"))

    assert job.conversion_sha256 is None
    assert job.converter_version is None
    db.commit.assert_not_awaited()
