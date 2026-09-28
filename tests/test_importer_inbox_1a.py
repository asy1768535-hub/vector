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
        slug="pdf_routing",
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
            parser_config.slug,
            parser_config.chunk_size,
            parser_config.chunk_overlap,
            parser_config.ocr_enabled,
            parser_config.docx_table_aware,
        ) == ("pdf_routing", 800, 80, False, True)
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


def test_importer_processes_single_page_pdf_with_real_parsing_pipeline(monkeypatch, tmp_path):
    """Worker 链路经过真实 parse_import_file 与级联门控，不替身化解析函数。"""
    from unittest.mock import patch
    from app.services.parser_units import build_parser_unit, parser_provenance

    context = _install_worker_fakes(monkeypatch, tmp_path)
    context.library.ocr_enabled = True

    pdf_source = tmp_path / "document.pdf"
    pdf_source.write_bytes(b"%PDF-1.4 fake")

    # 更新 worker 视角的 job 与 source 路径
    monkeypatch.setattr(importer, "staging_path", lambda _key: pdf_source)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_routing")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr("app.services.ocr.is_available", lambda: True)

    # 让 job.file_name 为 .pdf
    orig_get = context.read_db.get
    async def mock_get(model, object_id):
        obj = await orig_get(model, object_id)
        if model is importer.DocumentImportJob and obj is not None:
            obj.file_name = "document.pdf"
            obj.content_type = "application/pdf"
            obj.size_bytes = pdf_source.stat().st_size
        return obj
    context.read_db.get = mock_get

    blocks = [
        {"text": "操作系统虚拟内存管理机制将物理内存抽象为离散页面", "bbox": [10.0, 10.0, 200.0, 30.0], "confidence": 0.96},
        {"text": "通过多级页表结构有效降低了大地址空间的页表常驻开销", "bbox": [10.0, 40.0, 200.0, 60.0], "confidence": 0.95},
        {"text": "缺页异常处理程序按需从后备交换空间将目标页置换载入", "bbox": [10.0, 70.0, 200.0, 90.0], "confidence": 0.97},
        {"text": "页面置换策略平衡了内存命中率与磁盘读写吞吐开销", "bbox": [10.0, 100.0, 200.0, 120.0], "confidence": 0.94},
        {"text": "硬件内存管理单元协同操作系统内核维护快表转换缓存", "bbox": [10.0, 130.0, 200.0, 150.0], "confidence": 0.95},
        {"text": "保障了多道程序并发执行时地址空间隔离与高效地址映射", "bbox": [10.0, 160.0, 200.0, 180.0], "confidence": 0.96},
    ]
    full_text = "".join(b["text"] for b in blocks)
    candidate = {
        "normalized_text": f"【第 1 页】\\n{full_text}",
        "chunks": [{
            "text": f"【第 1 页】\\n{full_text}",
            "source_start": 0,
            "source_end": len(full_text) + 8,
            "location": {"type": "page", "page": 1},
        }],
        "segments": [{
            "kind": "prose",
            "text": full_text,
            "location": {"type": "page", "page": 1},
            "quality": {
                "extraction_mode": "ocr",
                "visual_content_unparsed": False,
                "ocr_blocks": blocks,
            },
            "parser_unit": build_parser_unit(
                source_kind="pdf",
                unit_kind="section",
                ordinal=0,
                unit_key="pdf:0:section:0",
                parser=parser_provenance("builtin-pdf", "v1"),
                source={"page": {"start": 1, "end": 1}},
            ),
            "structured_units": [],
        }],
        "coverage": {
            "status": "complete",
            "total_pages": 1,
            "pages": [{"page": 1, "status": "complete", "visual_content_unparsed": False}],
        },
    }

    stages_recorded = []
    async def record_stage(job_id, stage):
        stages_recorded.append(stage)
        if stage == "chunking":
            raise _StopAfterBoundaryProof

    monkeypatch.setattr(importer, "_set_stage", record_stage)
    monkeypatch.setattr(importer, "_prepare_revision_file", AsyncMock(return_value=None))

    with (
        patch("app.services.pdf_preflight.preflight_pdf") as mock_preflight,
        patch("app.services.pdf_extract.build_pdf_source") as mock_extract,
        patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru,
    ):
        mock_preflight.return_value = {
            "contract_version": "pdf-preflight-v1",
            "status": "complete",
            "total_pages": 1,
            "page_count_known": True,
            "page_limit_exceeded": False,
            "image_limit_exceeded": False,
            "has_mixed_content": False,
            "pages": [{"page": 1, "native_text_chars": 0, "embedded_image_count": 1, "has_visual_content": True, "low_text": True}],
            "unknown_reason": None,
        }
        mock_extract.return_value = candidate

        with pytest.raises(_StopAfterBoundaryProof):
            asyncio.run(importer._process_claimed_job(context.job_id))

        assert stages_recorded == ["parsing", "chunking"]
        assert mock_extract.call_count == 1
        assert mock_mineru.call_count == 0
