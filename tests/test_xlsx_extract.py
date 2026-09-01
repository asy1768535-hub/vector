"""xlsx 摄入单测：工作表 → 表格 segment。"""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl
import pytest

from app.config import settings
from app.services import xlsx_extract
from app.services.xlsx_extract import XlsxLimitError, extract_xlsx_segments


def _xlsx_bytes(*rows):
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fake_zip(infos):
    class FakeZip:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def infolist(self):
            return infos

    return FakeZip()


def test_xlsx_sheet_becomes_table_segment():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "技术选型"
    ws.append(["类别", "方案"])
    ws.append(["数据库", "MySQL"])
    ws.append(["缓存", "Redis"])
    buf = io.BytesIO()
    wb.save(buf)

    segs = extract_xlsx_segments(buf.getvalue())
    assert len(segs) == 1
    s = segs[0]
    assert s["kind"] == "table"
    assert s["heading"] == "技术选型"          # 工作表名作章节/表名
    assert s["header"] == "类别 | 方案"         # 首行作表头
    assert "数据库 | MySQL" in s["rows"]
    assert "缓存 | Redis" in s["rows"]
    assert s["row_numbers"] == [1, 2, 3]
    assert s["location"] == {"type": "sheet", "sheet": "技术选型"}


def test_xlsx_empty_sheet_skipped():
    wb = openpyxl.Workbook()
    wb.active.title = "空表"
    buf = io.BytesIO()
    wb.save(buf)
    assert extract_xlsx_segments(buf.getvalue()) == []


def test_xlsx_staging_path_does_not_require_xlsx_suffix(tmp_path):
    path = tmp_path / "00000000000000000000000000000000.upload"
    wb = openpyxl.Workbook()
    wb.active.append(["name", "value"])
    wb.active.append(["asset", "42"])
    wb.save(path)

    segments = extract_xlsx_segments(path)

    assert segments[0]["rows"] == ["name | value", "asset | 42"]


def test_xls_rejects_oversized_input_before_reading_parser(tmp_path, monkeypatch):
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"toolarge")
    monkeypatch.setattr(settings, "xls_max_input_bytes", 3)

    with pytest.raises(XlsxLimitError, match="XLS input exceeds"):
        extract_xlsx_segments(path, is_xls=True)


def test_xlsx_rejects_zip_entry_count_before_openpyxl(monkeypatch):
    monkeypatch.setattr(settings, "xlsx_max_zip_entries", 1)
    archive = _fake_zip([SimpleNamespace(file_size=1, compress_size=1)] * 2)

    with patch.object(xlsx_extract.zipfile, "ZipFile", return_value=archive):
        with pytest.raises(XlsxLimitError, match="entry limit"):
            xlsx_extract._validate_xlsx_archive(b"fixture")


@pytest.mark.parametrize(
    ("info", "attribute", "limit", "message"),
    [
        (SimpleNamespace(file_size=1, compress_size=11), "xlsx_max_zip_compressed_bytes", 10, "compressed-size"),
        (SimpleNamespace(file_size=11, compress_size=1), "xlsx_max_zip_uncompressed_bytes", 10, "uncompressed-size"),
        (SimpleNamespace(file_size=11, compress_size=1), "xlsx_max_zip_entry_bytes", 10, "entry exceeds"),
        (SimpleNamespace(file_size=11, compress_size=1), "xlsx_max_zip_compression_ratio", 10, "compression-ratio"),
    ],
)
def test_xlsx_rejects_zip_bomb_metadata(monkeypatch, info, attribute, limit, message):
    monkeypatch.setattr(settings, attribute, limit)
    archive = _fake_zip([info])

    with patch.object(xlsx_extract.zipfile, "ZipFile", return_value=archive):
        with pytest.raises(XlsxLimitError, match=message):
            xlsx_extract._validate_xlsx_archive(b"fixture")


def test_xlsx_rejects_declared_sheet_dimensions_without_large_fixture(monkeypatch):
    monkeypatch.setattr(settings, "xlsx_max_rows", 1)

    with pytest.raises(XlsxLimitError, match="row limit"):
        extract_xlsx_segments(_xlsx_bytes(["header"], ["value"]))


def test_xlsx_rejects_sheet_count_without_large_fixture(monkeypatch):
    monkeypatch.setattr(settings, "xlsx_max_sheets", 1)
    wb = openpyxl.Workbook()
    wb.create_sheet("second")
    buf = io.BytesIO()
    wb.save(buf)

    with pytest.raises(XlsxLimitError, match="sheet limit"):
        extract_xlsx_segments(buf.getvalue())


def test_xlsx_rejects_cell_and_text_limits_without_large_fixture(monkeypatch):
    monkeypatch.setattr(settings, "xlsx_max_cells", 1)
    with pytest.raises(XlsxLimitError, match="cell limit"):
        extract_xlsx_segments(_xlsx_bytes(["header", "second"]))

    monkeypatch.setattr(settings, "xlsx_max_cells", 1_000_000)
    monkeypatch.setattr(settings, "xlsx_max_cell_text_chars", 3)
    with pytest.raises(XlsxLimitError, match="cell text"):
        extract_xlsx_segments(_xlsx_bytes(["secret-content"]))
