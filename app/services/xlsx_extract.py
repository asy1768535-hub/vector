"""xlsx/xls 表格摄入：每个工作表 → 一个 table segment（首行作表头）。

产出与 docx_extract 同构的 table segment（kind/heading/caption/header/rows），
交给 splitter.chunk_segments 做表格感知切分。.xlsx 用 openpyxl；.xls 走 xlrd（best-effort）。
"""
from __future__ import annotations

import io
from pathlib import Path


def _rows_to_numbered_lines(rows) -> list[tuple[int, str]]:
    """二维单元格 → [(row_number, 'a | b | c'), ...]，去行尾空单元格、跳过空行。"""
    out: list[tuple[int, str]] = []
    for row_number, cells in rows:
        vals = [("" if c is None else str(c)).strip() for c in cells]
        while vals and vals[-1] == "":
            vals.pop()
        line = " | ".join(vals).strip(" |")
        if line:
            out.append((row_number, line))
    return out


def _seg(name: str, numbered_rows: list[tuple[int, str]]) -> dict:
    row_numbers = [n for n, _line in numbered_rows]
    rows = [line for _n, line in numbered_rows]
    return {
        "kind": "table",
        "heading": name,
        "caption": name,
        "header": rows[0],
        "rows": rows,
        "row_numbers": row_numbers,
        "location": {"type": "sheet", "sheet": name},
    }


def _read_xlsx(data: bytes | Path) -> list[dict]:
    import openpyxl

    source = data if isinstance(data, Path) else io.BytesIO(data)
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        segs: list[dict] = []
        for ws in wb.worksheets:
            rows = _rows_to_numbered_lines(enumerate(ws.iter_rows(values_only=True), start=1))
            if rows:
                segs.append(_seg(ws.title, rows))
        return segs
    finally:
        wb.close()


def _read_xls(data: bytes) -> list[dict]:
    try:
        import xlrd  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise ValueError(".xls 需要 xlrd（pip install xlrd），或先另存为 .xlsx") from exc
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    segs: list[dict] = []
    for sh in book.sheets():
        rows = _rows_to_numbered_lines((r + 1, sh.row_values(r)) for r in range(sh.nrows))
        if rows:
            segs.append(_seg(sh.name, rows))
    return segs


def extract_xlsx_segments(data: bytes | Path, *, is_xls: bool = False) -> list[dict]:
    """读电子表格 → table segment 列表（空表跳过）。"""
    if is_xls:
        return _read_xls(data.read_bytes() if isinstance(data, Path) else data)
    return _read_xlsx(data)
