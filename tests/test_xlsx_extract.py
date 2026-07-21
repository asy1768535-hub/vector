"""xlsx 摄入单测：工作表 → 表格 segment。"""
from __future__ import annotations

import io

import openpyxl

from app.services.xlsx_extract import extract_xlsx_segments


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
