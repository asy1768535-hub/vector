from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import docx
import pytest

from app.services.import_parsing import parse_import_file
from app.services.import_uploads import (
    ImportUploadError,
    folder_path_for_job,
    normalize_relative_path,
    source_path_for_job,
)
from app.services.object_storage_local import LocalObjectStorageAdapter
from app.services.revision_files import prepare_managed_file_path


def _library(**overrides):
    values = {
        "chunk_size": 200,
        "chunk_overlap": 20,
        "ocr_enabled": False,
        "docx_table_aware": False,
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
