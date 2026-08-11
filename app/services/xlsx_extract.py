"""xlsx/xls 表格摄入：每个工作表 → 一个 table segment（首行作表头）。

产出与 docx_extract 同构的 table segment（kind/heading/caption/header/rows），
交给 splitter.chunk_segments 做表格感知切分。.xlsx 用 openpyxl；.xls 走 xlrd（best-effort）。
"""
from __future__ import annotations

import io
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from app.services.parser_units import (
    build_parser_unit,
    excel_column_name,
    parser_provenance,
)


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


def _cell_units(
    sheet_name: str,
    rows: list[tuple[Any, ...]],
    *,
    source_kind: str,
    parser: dict[str, str],
    parent_key: str,
    formula_rows: list[tuple[Any, ...]] | None = None,
) -> list[dict]:
    units: list[dict] = []
    for row_number, cells in enumerate(rows, start=1):
        formula_cells = formula_rows[row_number - 1] if formula_rows and row_number <= len(formula_rows) else ()
        for column, value in enumerate(cells, start=1):
            formula_value = formula_cells[column - 1] if column <= len(formula_cells) else None
            formula = formula_value if isinstance(formula_value, str) and formula_value.startswith("=") else None
            if value is None and formula is None:
                continue
            cell_name = f"{excel_column_name(column)}{row_number}"
            unit = build_parser_unit(
                source_kind=source_kind,
                unit_kind="cell",
                ordinal=len(units),
                unit_key=f"{parent_key}:cell:{cell_name}",
                parser=parser,
                source={
                    "sheet": {"name": sheet_name},
                    "row": {"start": row_number, "end": row_number},
                    "column": {"start": column, "end": column},
                    "cell": {"start": cell_name, "end": cell_name},
                },
                parent_key=parent_key,
            )
            unit["value"] = value
            unit["formula"] = formula
            units.append(unit)
    return units


def _seg(
    name: str,
    numbered_rows: list[tuple[int, str]],
    *,
    ordinal: int,
    source_kind: str,
    parser: dict[str, str],
    structured_units: list[dict],
) -> dict:
    row_numbers = [n for n, _line in numbered_rows]
    rows = [line for _n, line in numbered_rows]
    return {
        "kind": "table",
        "heading": name,
        "caption": name,
        "header": rows[0],
        "rows": rows,
        "row_numbers": row_numbers,
        "source_kind": source_kind,
        "ordinal": ordinal,
        "unit_key": f"{source_kind}:sheet:{ordinal}",
        "parser": parser,
        "structured_units": structured_units,
        "location": {"type": "sheet", "sheet": name},
    }


def _read_xlsx(data: bytes | Path) -> list[dict]:
    import openpyxl

    def open_source():
        return data.open("rb") if isinstance(data, Path) else io.BytesIO(data)

    with ExitStack() as stack:
        source = stack.enter_context(open_source())
        formula_source = stack.enter_context(open_source())
        wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
        stack.callback(wb.close)
        formula_wb = openpyxl.load_workbook(
            formula_source, read_only=True, data_only=False
        )
        stack.callback(formula_wb.close)
        segs: list[dict] = []
        parser = parser_provenance("openpyxl", "v1", {"data_only": True})
        for ordinal, (ws, formula_ws) in enumerate(zip(wb.worksheets, formula_wb.worksheets, strict=True)):
            data_rows = list(ws.iter_rows(values_only=True))
            formula_rows = list(formula_ws.iter_rows(values_only=True))
            rows = _rows_to_numbered_lines(enumerate(data_rows, start=1))
            if rows:
                segs.append(
                    _seg(
                        ws.title,
                        rows,
                        ordinal=ordinal,
                        source_kind="xlsx",
                        parser=parser,
                        structured_units=_cell_units(
                            ws.title,
                            data_rows,
                            source_kind="xlsx",
                            parser=parser,
                            parent_key=f"xlsx:sheet:{ordinal}",
                            formula_rows=formula_rows,
                        ),
                    )
                )
        return segs


def _read_xls(data: bytes) -> list[dict]:
    try:
        import xlrd  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise ValueError(".xls 需要 xlrd（pip install xlrd），或先另存为 .xlsx") from exc
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    segs: list[dict] = []
    parser = parser_provenance("xlrd", "v1", {"data_only": True})
    for ordinal, sh in enumerate(book.sheets()):
        raw_rows = [tuple(sh.row_values(r)) for r in range(sh.nrows)]
        rows = _rows_to_numbered_lines(enumerate(raw_rows, start=1))
        if rows:
            segs.append(
                _seg(
                    sh.name,
                    rows,
                    ordinal=ordinal,
                    source_kind="xls",
                    parser=parser,
                    structured_units=_cell_units(
                        sh.name,
                        raw_rows,
                        source_kind="xls",
                        parser=parser,
                        parent_key=f"xls:sheet:{ordinal}",
                    ),
                )
            )
    return segs


def extract_xlsx_segments(data: bytes | Path, *, is_xls: bool = False) -> list[dict]:
    """读电子表格 → table segment 列表（空表跳过）。"""
    if is_xls:
        return _read_xls(data.read_bytes() if isinstance(data, Path) else data)
    return _read_xlsx(data)
