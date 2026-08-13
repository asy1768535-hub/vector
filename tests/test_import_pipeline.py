from __future__ import annotations

import asyncio
import hashlib
import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import docx
import pytest

from app.models.embedding_job import EmbeddingJob
from app.models.document_revision import DocumentRevision
from app.schemas.documents import ImportJobRead
from app.services.import_parsing import parse_import_file
from app.services.import_uploads import (
    ImportUploadError,
    folder_path_for_job,
    job_projection,
    normalize_relative_path,
    source_path_for_job,
)
from app.services.object_storage_local import LocalObjectStorageAdapter
from app.services.revision_files import prepare_managed_file_path
from app.workers.importer import _apply_import_revision_scope


def _library(**overrides):
    values = {
        "chunk_size": 200,
        "chunk_overlap": 20,
        "ocr_enabled": False,
        "docx_table_aware": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_background_import_parser_remains_synchronous():
    assert inspect.iscoroutinefunction(parse_import_file) is False


class _Scalars:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return _Scalars(self.value)


class _ProjectionDb:
    def __init__(self, *, embedding_job, graph_job=None):
        self.embedding_job = embedding_job
        self.graph_job = graph_job
        self.execute_calls = 0

    async def get(self, model, object_id):
        if model is EmbeddingJob and object_id == self.embedding_job.id:
            return self.embedding_job
        return None

    async def execute(self, statement):
        self.execute_calls += 1
        return _Result(self.graph_job)


class _ScopeDb:
    def __init__(self, *, revision):
        self.revision = revision

    async def get(self, model, object_id):
        if model is DocumentRevision and object_id == self.revision.id:
            return self.revision
        return None


def _processing_import_job(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "id": uuid.uuid4(),
        "library_id": uuid.uuid4(),
        "batch_id": uuid.uuid4(),
        "file_name": "sample.md",
        "relative_path": None,
        "size_bytes": 12,
        "upload_offset": 12,
        "status": "processing",
        "current_stage": "embedding",
        "attempt_count": 1,
        "last_error": None,
        "result_operation": "created",
        "document_id": uuid.uuid4(),
        "document_revision_id": uuid.uuid4(),
        "embedding_job_id": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
        "upload_completed_at": now,
        "claimed_at": now,
        "finished_at": None,
        "graph_extraction_requested": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_relative_path_preserves_nested_folder_hierarchy():
    relative = normalize_relative_path(
        "项目甲\\合同\\主合同.md",
        "主合同.md",
    )
    job = SimpleNamespace(relative_path=relative)

    assert relative == "项目甲/合同/主合同.md"
    assert source_path_for_job(job) == "/项目甲/合同/主合同.md"
    assert folder_path_for_job(job) == "/项目甲/合同"


def test_job_projection_reports_schema_wait_when_graph_job_is_not_created_yet():
    now = datetime.now(timezone.utc)
    job = _processing_import_job()
    embedding_job = SimpleNamespace(
        id=job.embedding_job_id,
        status="done",
        finished_at=now,
        last_error=None,
    )
    db = _ProjectionDb(embedding_job=embedding_job)

    projection = asyncio.run(job_projection(db, job))

    assert projection["status"] == "processing"
    assert projection["current_stage"] == "graph"
    assert projection["schema_discovery_state"] == "waiting_schema"
    assert ImportJobRead.model_validate(projection).schema_discovery_state == (
        "waiting_schema"
    )


def test_job_projection_keeps_processing_when_graph_job_already_exists():
    now = datetime.now(timezone.utc)
    job = _processing_import_job()
    embedding_job = SimpleNamespace(
        id=job.embedding_job_id,
        status="done",
        finished_at=now,
        last_error=None,
    )
    graph_job = SimpleNamespace(
        id=uuid.uuid4(),
        status="processing",
        finished_at=None,
        error_message=None,
    )
    db = _ProjectionDb(embedding_job=embedding_job, graph_job=graph_job)

    projection = asyncio.run(job_projection(db, job))

    assert projection["status"] == "processing"
    assert projection["current_stage"] == "graph"


@pytest.mark.parametrize(
    ("graph_status", "expected_status"),
    [("processing", "processing"), ("succeeded", "succeeded")],
)
def test_failed_graph_import_projects_latest_rerun_state(
    graph_status,
    expected_status,
):
    now = datetime.now(timezone.utc)
    job = _processing_import_job(
        status="failed",
        current_stage="graph",
        last_error="graph extraction failed",
        finished_at=now,
    )
    embedding_job = SimpleNamespace(
        id=job.embedding_job_id,
        status="done",
        finished_at=now,
        last_error=None,
    )
    graph_job = SimpleNamespace(
        id=uuid.uuid4(),
        status=graph_status,
        finished_at=now if graph_status == "succeeded" else None,
        error_message=None,
    )
    db = _ProjectionDb(embedding_job=embedding_job, graph_job=graph_job)

    projection = asyncio.run(job_projection(db, job))

    assert projection["status"] == expected_status
    assert projection["current_stage"] == (
        "completed" if graph_status == "succeeded" else "graph"
    )
    assert projection["last_error"] is None
    assert projection["retry_target_type"] is None
    assert projection["retry_target_id"] is None


def test_partially_succeeded_graph_remains_visible_and_routes_graph_retry():
    now = datetime.now(timezone.utc)
    job = _processing_import_job()
    embedding_job = SimpleNamespace(
        id=job.embedding_job_id,
        status="done",
        finished_at=now,
        last_error=None,
    )
    graph_job = SimpleNamespace(
        id=uuid.uuid4(),
        status="partially_succeeded",
        finished_at=now,
        error_message="unit_failures",
    )
    db = _ProjectionDb(embedding_job=embedding_job, graph_job=graph_job)

    projection = asyncio.run(job_projection(db, job))

    assert projection["status"] == "failed"
    assert projection["current_stage"] == "graph"
    assert projection["last_error"] == "unit_failures"
    assert projection["retry_target_type"] == "graph"
    assert projection["retry_target_id"] == graph_job.id
    assert ImportJobRead.model_validate(projection).retry_target_type == "graph"


def test_non_graph_import_failure_is_not_hidden_by_projection():
    job = _processing_import_job(
        status="failed",
        current_stage="parsing",
        last_error="parse failed",
    )
    db = _ProjectionDb(embedding_job=SimpleNamespace(id=job.embedding_job_id))

    projection = asyncio.run(job_projection(db, job))

    assert projection["status"] == "failed"
    assert projection["current_stage"] == "parsing"
    assert projection["last_error"] == "parse failed"
    assert projection["retry_target_type"] == "import"
    assert projection["retry_target_id"] == job.id
    assert db.execute_calls == 0


def test_import_revision_scope_syncs_security_level_and_graph_request():
    revision_id = uuid.uuid4()
    job = SimpleNamespace(
        security_level="internal",
        graph_extraction_requested=True,
    )
    document = SimpleNamespace(security_level=None)
    revision = SimpleNamespace(
        id=revision_id,
        security_level=None,
        parser_config={"parser": "plain"},
    )

    asyncio.run(
        _apply_import_revision_scope(
            _ScopeDb(revision=revision),
            job=job,
            document=document,
            revision_id=revision_id,
        )
    )

    assert document.security_level == "internal"
    assert revision.security_level == "internal"
    assert revision.parser_config == {
        "parser": "plain",
        "graph_extraction_requested": True,
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "../secret.txt",
        "folder/../secret.txt",
        "/absolute/secret.txt",
        "C:/secret.txt",
        r"\\server\share\secret.txt",
    ],
)
def test_relative_path_rejects_traversal_and_absolute_paths(relative_path):
    with pytest.raises(ImportUploadError):
        normalize_relative_path(relative_path, "secret.txt")


def test_text_and_csv_are_parsed_from_paths(tmp_path):
    text_path = tmp_path / "notes.md"
    text_path.write_text("# 标题\n正文内容", encoding="utf-8")
    csv_path = tmp_path / "records.csv"
    csv_path.write_text("name,value\n甲,1\n乙,2\n", encoding="utf-8")

    text_result = parse_import_file(text_path, _library())
    csv_result = parse_import_file(csv_path, _library())

    assert "# 标题" in text_result.normalized_text
    assert text_result.chunks
    assert "甲 | 1" in csv_result.normalized_text
    assert csv_result.chunks


def test_docx_is_parsed_directly_from_path(tmp_path):
    path = tmp_path / "sample.docx"
    document = docx.Document()
    document.add_paragraph("路径读取的 Word 正文")
    document.save(path)

    result = parse_import_file(path, _library())

    assert "路径读取的 Word 正文" in result.normalized_text
    assert result.chunks


def test_docx_staging_file_uses_original_file_name_for_type(tmp_path):
    path = tmp_path / "00000000000000000000000000000000.upload"
    document = docx.Document()
    document.add_paragraph("DOCX content from a chunked upload")
    document.save(path)

    result = parse_import_file(path, _library(), file_name="nested/sample.docx")

    assert "DOCX content from a chunked upload" in result.normalized_text
    assert result.chunks


def test_local_object_storage_put_file_streams_and_reuses_content(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"streamed-content" * 4096)
    adapter = LocalObjectStorageAdapter(
        root=tmp_path / "objects",
        endpoint_ref="primary",
        max_read_bytes=1024 * 1024,
    )

    first = asyncio.run(
        adapter.put_file("libraries/test/objects/item.bin", source, None)
    )
    second = asyncio.run(
        adapter.put_file("libraries/test/objects/item.bin", source, None)
    )

    assert first == second
    assert adapter.path_for_read("libraries/test/objects/item.bin").read_bytes() == (
        source.read_bytes()
    )


def test_prepare_managed_file_path_validates_expected_hash(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("large file source", encoding="utf-8")
    adapter = LocalObjectStorageAdapter(
        root=tmp_path / "objects",
        endpoint_ref="primary",
        max_read_bytes=1024,
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    prepared = asyncio.run(
        prepare_managed_file_path(
            adapter=adapter,
            library_id=SimpleNamespace(hex="library"),
            file_name="source.txt",
            content_type="text/plain",
            source_path=source,
            expected_sha256=digest,
        )
    )

    assert prepared.sha256 == digest
    assert prepared.size_bytes == source.stat().st_size


def test_prepare_managed_file_path_rejects_changed_source(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("changed", encoding="utf-8")
    adapter = LocalObjectStorageAdapter(
        root=tmp_path / "objects",
        endpoint_ref="primary",
        max_read_bytes=1024,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            prepare_managed_file_path(
                adapter=adapter,
                library_id=SimpleNamespace(hex="library"),
                file_name="source.txt",
                content_type="text/plain",
                source_path=source,
                expected_sha256="0" * 64,
            )
        )
