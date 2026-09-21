"""MinerU 3.4.4 PDF parsing client and response adapter.

The adapter is intentionally independent from the import entry points.  It accepts
only content-list structures that can be represented by the existing parser-unit
and EvidenceLocator contracts.
"""
from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from app.services import splitter
from app.services.pdf_coverage import (
    PDF_COVERAGE_MAX_PAGE_NUMBER,
    attach_pdf_coverage_unit,
    create_pdf_coverage_report,
)
from app.services.parser_units import build_parser_unit, parser_provenance

EXPECTED_MINERU_VERSION = "3.4.4"
_QUESTION_RE = re.compile(r"^\s*\d+[.．、]\s*\S")
_TEXT_TYPES = {"text", "list", "header"}
_FORMULA_TYPES = {"equation", "interline_equation", "formula"}
_MAX_TABLE_SPAN = 64
_VISUAL_TYPES = {"image", "natural_image", "flowchart", "text_image", "diagram"}
_IGNORED_TYPES = {"footer", "page_number"} | _VISUAL_TYPES
_MAX_TABLE_ROWS = 1000
_MAX_TABLE_COLUMNS = 256
_MAX_TABLE_SLOTS = 10_000

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_CONTENT_ITEMS = 10_000
_MAX_CONTENT_STRING_CHARS = 10_000_000
_MAX_STRUCTURED_UNITS = 50_000
_MAX_EXPANDED_TABLE_TEXT_CHARS = 1_000_000
_MAX_PDF_PAGES = 500
_MAX_EMBEDDED_JSON_DEPTH = 128
_MAX_EMBEDDED_JSON_NODES = 100_000


def _bounded_string_chars(value: object, *, limit: int) -> int:
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item)
            if total > limit:
                raise MineruPdfError("MinerU response exceeds content limits")
        elif isinstance(item, Mapping):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return total

class MineruPdfError(ValueError):
    """MinerU invocation or response validation failed without input disclosure."""


class _TableHtmlParser(HTMLParser):
    """Extract and safely expand an HTML table into a rectangular grid."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._raw_rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_header = False
        self._cell_rowspan = 1
        self._cell_colspan = 1
        self._table_depth = 0

    @staticmethod
    def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
        raw = next((value for key, value in attrs if key.lower() == name), None)
        if raw is None:
            return 1
        value = raw.strip()
        if not value.isdigit():
            return 1
        number = int(value)
        return number if 1 <= number <= _MAX_TABLE_SPAN else 1

    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.lower()
        if name == "table":
            self._table_depth += 1
        elif name == "tr" and self._table_depth == 1:
            self._finish_row()
            self._row = []
        elif name in {"td", "th"} and self._table_depth == 1 and self._row is not None:
            self._finish_cell()
            self._cell_parts = []
            self._cell_header = name == "th"
            self._cell_rowspan = self._span(attrs, "rowspan")
            self._cell_colspan = self._span(attrs, "colspan")
        elif name == "br" and self._cell_parts is not None:
            self._cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"td", "th"} and self._table_depth == 1:
            self._finish_cell()
        elif name == "tr" and self._table_depth == 1:
            self._finish_row()
        elif name == "table" and self._table_depth:
            if self._table_depth == 1:
                self._finish_cell()
                self._finish_row()
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None and self._table_depth == 1:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_cell()
        self._finish_row()
        self.rows = _expand_table_rows(self._raw_rows)

    def _finish_cell(self) -> None:
        if self._cell_parts is None or self._row is None:
            return
        value = " ".join("".join(self._cell_parts).replace("\x00", "").split())
        self._row.append({
            "text": value,
            "header": self._cell_header,
            "rowspan": self._cell_rowspan,
            "colspan": self._cell_colspan,
        })
        self._cell_parts = None
        self._cell_header = False
        self._cell_rowspan = 1
        self._cell_colspan = 1

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row is not None and self._row:
            self._raw_rows.append(self._row)
        self._row = None


def _expand_table_rows(raw_rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Expand bounded row/column spans and retain their origin on every slot."""
    if len(raw_rows) > _MAX_TABLE_ROWS:
        raise ValueError("table row limit exceeded")
    grid: dict[tuple[int, int], dict[str, Any]] = {}
    expanded_chars = 0
    for row_index, raw_row in enumerate(raw_rows):
        column = 0
        for raw_cell in raw_row:
            rowspan = int(raw_cell.get("rowspan") or 1)
            colspan = int(raw_cell.get("colspan") or 1)
            expanded_chars += len(raw_cell["text"]) * rowspan * colspan
            if expanded_chars > _MAX_EXPANDED_TABLE_TEXT_CHARS:
                raise MineruPdfError("MinerU table exceeds content limits")
            while True:
                if column + colspan > _MAX_TABLE_COLUMNS:
                    raise ValueError("table column limit exceeded")
                positions = [
                    (row, col)
                    for row in range(row_index, row_index + rowspan)
                    for col in range(column, column + colspan)
                ]
                if not any(position in grid for position in positions):
                    break
                column += 1
            if row_index + rowspan > _MAX_TABLE_ROWS:
                raise ValueError("table row limit exceeded")
            if len(grid) + len(positions) > _MAX_TABLE_SLOTS:
                raise ValueError("table slot limit exceeded")
            for row, col in positions:
                grid[(row, col)] = {
                    "text": raw_cell["text"],
                    "header": bool(raw_cell["header"]),
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "span_origin_row": row_index,
                    "span_origin_column": column,
                    "is_span_copy": row != row_index or col != column,
                }
            column += colspan
    if not grid:
        return []
    row_count = max(row for row, _column in grid) + 1
    column_count = max(column for _row, column in grid) + 1
    if row_count * column_count > _MAX_TABLE_SLOTS:
        raise ValueError("table slot limit exceeded")
    return [
        [
            grid.get((row, column), {
                "text": "",
                "header": False,
                "rowspan": 1,
                "colspan": 1,
                "span_origin_row": row,
                "span_origin_column": column,
                "is_span_copy": False,
                "is_synthetic_empty": True,
            })
            for column in range(column_count)
        ]
        for row in range(row_count)
    ]


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def _page_number(item: Mapping[str, Any]) -> int | None:
    value = item.get("page_idx")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value + 1


def _bbox(item: Mapping[str, Any]) -> dict[str, Any] | None:
    value = item.get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(part) for part in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(part) for part in numbers):
        return None
    if numbers[2] < numbers[0] or numbers[3] < numbers[1]:
        return None
    return {
        "x_min": numbers[0],
        "y_min": numbers[1],
        "x_max": numbers[2],
        "y_max": numbers[3],
        "coordinate_system": "mineru_output_coordinates",
    }


def _source(item: Mapping[str, Any], *, table_index: int | None = None) -> dict[str, Any]:
    source: dict[str, Any] = {}
    page = _page_number(item)
    if page is not None:
        source["page"] = {"start": page, "end": page}
    if table_index is not None:
        source["table"] = {"index": table_index}
    bbox = _bbox(item)
    if bbox is not None:
        source["bbox"] = bbox
    return source


def _location(item: Mapping[str, Any], *, table_index: int | None = None) -> dict[str, Any]:
    location: dict[str, Any] = {"type": "page"}
    page = _page_number(item)
    if page is not None:
        location["page"] = page
    if table_index is not None:
        location["table_index"] = table_index + 1
    return location


def _content_text(item: Mapping[str, Any], item_type: str) -> str | None:
    if item_type == "list":
        values = item.get("list_items")
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            return None
        return _safe_text("\n".join(value for value in values if value.strip()))
    if item_type in _TEXT_TYPES | _FORMULA_TYPES:
        return _safe_text(item.get("text"))
    return None


def _parse_table_html(value: object) -> list[list[dict[str, Any]]]:
    html = _safe_text(value)
    if html is None:
        return []
    parser = _TableHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except MineruPdfError:
        raise
    except (ValueError, AssertionError):
        return []
    return parser.rows


def _table_html(item: Mapping[str, Any]) -> object:
    for key in ("table_body", "table_html", "html"):
        if key in item:
            return item[key]
    return None


def _parser(version: str) -> dict[str, str]:
    return parser_provenance(
        "mineru",
        version,
        {
            "backend": "hybrid-engine",
            "effort": "high",
            "parse_method": "ocr",
            "formula_enable": True,
            "table_enable": True,
            "image_analysis": True,
        },
    )


def _block_unit(
    item: Mapping[str, Any],
    *,
    item_index: int,
    parent_key: str,
    parser: Mapping[str, str],
    text: str,
    structure_type: str,
) -> dict[str, Any]:
    unit = build_parser_unit(
        source_kind="pdf",
        unit_kind="structured_unit",
        ordinal=item_index + 1,
        unit_key=f"mineru:item:{item_index}",
        parser=parser,
        source=_source(item),
        parent_key=parent_key,
        quality={"extraction_mode": "mixed"},
    )
    unit["text"] = text
    unit["structure_type"] = structure_type
    if structure_type == "formula":
        unit["latex"] = text
    return unit


def _prose_segment(
    item: Mapping[str, Any],
    *,
    item_index: int,
    item_type: str,
    text: str,
    parser: Mapping[str, str],
) -> dict[str, Any]:
    key = f"mineru:segment:{item_index}"
    kind = "formula" if item_type in _FORMULA_TYPES else item_type
    segment = {
        "kind": kind,
        "text": text,
        "source_kind": "pdf",
        "ordinal": item_index,
        "unit_key": key,
        "parser": dict(parser),
        "location": _location(item),
        "quality": {"extraction_mode": "mixed"},
        "structure": {
            "type": kind,
            **({"latex": text} if kind == "formula" else {}),
        },
        "structured_units": [
            _block_unit(
                item,
                item_index=item_index,
                parent_key=key,
                parser=parser,
                text=text,
                structure_type="formula" if item_type in _FORMULA_TYPES else item_type,
            )
        ],
        "mineru_source": _source(item),
    }
    return segment


def _table_row_lines(rows: list[list[dict[str, Any]]]) -> list[str]:
    return [" | ".join(cell["text"] for cell in row).strip(" |") for row in rows]


def _expanded_table_text_chars(rows: list[list[dict[str, Any]]]) -> int:
    total = max(0, len(rows) - 1)
    for row in rows:
        total += sum(len(str(cell.get("text") or "")) for cell in row)
        total += max(0, len(row) - 1) * 3
    return total


def _table_descendant_units(
    item: Mapping[str, Any],
    *,
    table_key: str,
    table_index: int,
    rows: list[list[dict[str, Any]]],
    parser: Mapping[str, str],
) -> list[dict[str, Any]]:
    row_lines = _table_row_lines(rows)
    units: list[dict[str, Any]] = []
    source = _source(item, table_index=table_index)
    ordinal = 1
    for row_index, row in enumerate(rows):
        row_key = f"{table_key}:row:{row_index}"
        row_unit = build_parser_unit(
            source_kind="pdf",
            unit_kind="row",
            ordinal=ordinal,
            unit_key=row_key,
            parser=parser,
            source=source,
            parent_key=table_key,
            quality={"extraction_mode": "mixed"},
        )
        row_unit["text"] = row_lines[row_index]
        row_unit["row_ordinal"] = row_index
        units.append(row_unit)
        ordinal += 1
        for cell_index, cell in enumerate(row):
            cell_unit = build_parser_unit(
                source_kind="pdf",
                unit_kind="cell",
                ordinal=ordinal,
                unit_key=f"{row_key}:cell:{cell_index}",
                parser=parser,
                source=source,
                parent_key=row_key,
                quality={"extraction_mode": "mixed"},
            )
            cell_unit.update({
                "text": cell["text"],
                "value": cell["text"],
                "row_ordinal": row_index,
                "cell_ordinal": cell_index,
                "header": bool(cell["header"]),
                "rowspan": int(cell["rowspan"]),
                "colspan": int(cell["colspan"]),
                "span_origin_row": int(cell["span_origin_row"]),
                "span_origin_column": int(cell["span_origin_column"]),
                "is_span_copy": bool(cell["is_span_copy"]),
            })
            if cell.get("is_synthetic_empty"):
                cell_unit["is_synthetic_empty"] = True
            units.append(cell_unit)
            ordinal += 1
    return units


def _question_segment(
    blocks: list[dict[str, Any]],
    *,
    parser: Mapping[str, str],
) -> dict[str, Any]:
    first = blocks[0]
    first_index = int(first["item_index"])
    first_item = first["item"]
    key = f"mineru:question:{first_index}"
    structured_units: list[dict[str, Any]] = []
    for block in blocks:
        item_index = int(block["item_index"])
        item = block["item"]
        item_type = str(block["item_type"])
        text = str(block["text"])
        if item_type == "table":
            table_index = int(block["table_index"])
            table_key = f"mineru:table:{table_index}:item:{item_index}"
            table_unit = build_parser_unit(
                source_kind="pdf",
                unit_kind="table",
                ordinal=item_index + 1,
                unit_key=table_key,
                parser=parser,
                source=_source(item, table_index=table_index),
                parent_key=key,
                quality={"extraction_mode": "mixed"},
            )
            table_unit["text"] = text
            table_unit["table_index"] = table_index
            structured_units.append(table_unit)
            structured_units.extend(_table_descendant_units(
                item,
                table_key=table_key,
                table_index=table_index,
                rows=block["rows"],
                parser=parser,
            ))
        else:
            structured_units.append(_block_unit(
                item,
                item_index=item_index,
                parent_key=key,
                parser=parser,
                text=text,
                structure_type="formula" if item_type in _FORMULA_TYPES else item_type,
            ))
    formulae = [block["text"] for block in blocks if block["item_type"] in _FORMULA_TYPES]
    return {
        "kind": "question",
        "text": "\n".join(str(block["text"]) for block in blocks),
        "source_kind": "pdf",
        "ordinal": first_index,
        "unit_key": key,
        "parser": dict(parser),
        "location": _location(first_item),
        "quality": {"extraction_mode": "mixed"},
        "structure": {
            "type": "question",
            "item_types": [block["item_type"] for block in blocks],
            "formulae": formulae,
        },
        "structured_units": structured_units,
        "mineru_source": _source(first_item),
    }


def _table_segment(
    item: Mapping[str, Any],
    *,
    item_index: int,
    table_index: int,
    rows: list[list[dict[str, Any]]],
    parser: Mapping[str, str],
) -> dict[str, Any]:
    table_key = f"mineru:table:{table_index}:item:{item_index}"
    row_lines = _table_row_lines(rows)
    source = _source(item, table_index=table_index)
    return {
        "kind": "table",
        "rows": row_lines,
        "header": row_lines[0],
        "heading": "",
        "caption": "",
        "source_kind": "pdf",
        "ordinal": item_index,
        "unit_key": table_key,
        "parser": dict(parser),
        "location": _location(item, table_index=table_index),
        "quality": {"extraction_mode": "mixed"},
        "structure": {"type": "table", "table_index": table_index},
        "structured_units": _table_descendant_units(
            item,
            table_key=table_key,
            table_index=table_index,
            rows=rows,
            parser=parser,
        ),
        "mineru_source": source,
    }


def _attach_root_sources(source: dict[str, Any]) -> dict[str, Any]:
    """Preserve MinerU page/table/bbox coordinates on splitter-created roots."""
    for segment in source.get("segments") or []:
        parser_unit = segment.get("parser_unit")
        mineru_source = segment.pop("mineru_source", None)
        if isinstance(parser_unit, dict) and isinstance(mineru_source, dict):
            parser_unit.setdefault("source", {}).update(mineru_source)
    return source


def map_mineru_content_list(
    content_list: object,
    *,
    chunk_size: int,
    chunk_overlap: int,
    version: str = EXPECTED_MINERU_VERSION,
    total_pages: int | None = None,
) -> dict[str, Any]:
    """Purely map a MinerU content list to normalized text, chunks, and segments."""
    if not isinstance(content_list, list):
        raise MineruPdfError("MinerU response is invalid")
    if len(content_list) > _MAX_CONTENT_ITEMS:
        raise MineruPdfError("MinerU response exceeds content limits")
    _bounded_string_chars(content_list, limit=_MAX_CONTENT_STRING_CHARS)
    if total_pages is not None and (
        isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total_pages < 1
        or total_pages > _MAX_PDF_PAGES
    ):
        raise MineruPdfError("MinerU response is invalid")

    parser = _parser(version)
    segments: list[dict[str, Any]] = []
    pending_question: list[dict[str, Any]] | None = None
    table_index = 0
    prose_tail_index = -2
    skipped_visual_pages: list[int] = []
    skipped_visual_block_count = 0
    mapping_complete = True
    mapped_text_chars = 0
    structured_unit_count = len(content_list)

    def flush_question() -> None:
        nonlocal pending_question
        if pending_question:
            segments.append(_question_segment(pending_question, parser=parser))
        pending_question = None

    for item_index, raw_item in enumerate(content_list):
        if not isinstance(raw_item, Mapping):
            mapping_complete = False
            continue
        item_type_value = raw_item.get("type")
        if not isinstance(item_type_value, str):
            mapping_complete = False
            continue
        page_number = _page_number(raw_item)
        if page_number is not None and page_number > _MAX_PDF_PAGES:
            raise MineruPdfError("MinerU response exceeds page limits")
        item_type = item_type_value.strip().lower()
        if (
            pending_question
            and _page_number(raw_item) != _page_number(pending_question[0]["item"])
        ):
            flush_question()
        if item_type in _VISUAL_TYPES:
            skipped_visual_block_count += 1
            page = _page_number(raw_item)
            if page is not None:
                skipped_visual_pages.append(page)
            continue
        if item_type in _IGNORED_TYPES:
            continue
        if item_type == "table":
            rows = _parse_table_html(_table_html(raw_item))
            if not rows or _page_number(raw_item) is None or _bbox(raw_item) is None:
                mapping_complete = False
                continue
            structured_unit_count += len(rows) + sum(len(row) for row in rows)
            if structured_unit_count > _MAX_STRUCTURED_UNITS:
                raise MineruPdfError("MinerU response exceeds content limits")
            mapped_text_chars += _expanded_table_text_chars(rows)
            if mapped_text_chars > _MAX_CONTENT_STRING_CHARS:
                raise MineruPdfError("MinerU response exceeds content limits")
            if pending_question:
                pending_question.append({
                    "item_index": item_index,
                    "item": raw_item,
                    "item_type": "table",
                    "text": "\n".join(_table_row_lines(rows)),
                    "rows": rows,
                    "table_index": table_index,
                })
            else:
                segments.append(_table_segment(
                    raw_item,
                    item_index=item_index,
                    table_index=table_index,
                    rows=rows,
                    parser=parser,
                ))
            table_index += 1
            continue
        if item_type not in _TEXT_TYPES | _FORMULA_TYPES:
            flush_question()
            mapping_complete = False
            continue
        text = _content_text(raw_item, item_type)
        if text is None:
            mapping_complete = False
            continue
        if item_type == "header":
            flush_question()
        mapped_text_chars += len(text)
        if mapped_text_chars > _MAX_CONTENT_STRING_CHARS:
            raise MineruPdfError("MinerU response exceeds content limits")
        block = {
            "item_index": item_index,
            "item": raw_item,
            "item_type": item_type,
            "text": text,
        }
        if item_type in _FORMULA_TYPES and pending_question:
            pending_question.append(block)
            continue
        if item_type in _TEXT_TYPES and _QUESTION_RE.match(text):
            flush_question()
            pending_question = [block]
            continue
        if pending_question:
            pending_question.append(block)
            continue
        # Keep a list with its introduction (and short adjoining prose). Independent
        # list chunks lose the subject that embedding/reranking needs to retrieve them.
        previous = segments[-1] if segments else None
        if (
            item_type in {"text", "list"}
            and prose_tail_index == item_index - 1
            and previous is not None
            and previous["kind"] in _TEXT_TYPES
            and _page_number(raw_item) is not None
            and previous["location"].get("page") == _page_number(raw_item)
            and len(previous["text"]) + 1 + len(text) <= chunk_size
        ):
            previous["text"] += "\n" + text
            previous["kind"] = "text"
            previous["structure"] = {"type": "text"}
            previous["structured_units"].append(_block_unit(
                raw_item, item_index=item_index, parent_key=previous["unit_key"],
                parser=parser, text=text, structure_type=item_type,
            ))
            root_bbox = previous["mineru_source"].get("bbox")
            block_bbox = _bbox(raw_item)
            if root_bbox is not None and block_bbox is not None:
                for axis in ("x", "y"):
                    root_bbox[f"{axis}_min"] = min(root_bbox[f"{axis}_min"], block_bbox[f"{axis}_min"])
                    root_bbox[f"{axis}_max"] = max(root_bbox[f"{axis}_max"], block_bbox[f"{axis}_max"])
            else:
                previous["mineru_source"].pop("bbox", None)
            prose_tail_index = item_index
            continue
        segments.append(
            _prose_segment(
                raw_item,
                item_index=item_index,
                item_type=item_type,
                text=text,
                parser=parser,
            )
        )
        prose_tail_index = item_index
    flush_question()
    if not segments:
        raise MineruPdfError("MinerU returned no importable structure")
    source = _attach_root_sources(
        splitter.build_structured_source_from_segments(
            segments,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    )
    if not source.get("chunks") or not str(source.get("normalized_text") or "").strip():
        raise MineruPdfError("MinerU returned no importable structure")
    processed_pages: set[int] = set()
    for segment in source.get("segments") or []:
        unit = segment.get("parser_unit") if isinstance(segment, Mapping) else None
        page = (unit.get("source") or {}).get("page") if isinstance(unit, Mapping) else None
        if isinstance(page, Mapping):
            start, end = page.get("start"), page.get("end")
            if (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 1 <= start <= end <= PDF_COVERAGE_MAX_PAGE_NUMBER
            ):
                processed_pages.update(range(start, end + 1))
    if skipped_visual_block_count:
        status = "partial"
    elif (
        mapping_complete
        and total_pages is not None
        and len(processed_pages) == total_pages
        and min(processed_pages, default=0) == 1
        and max(processed_pages, default=0) == total_pages
    ):
        status = "complete"
    else:
        status = "unknown"
    try:
        coverage = create_pdf_coverage_report(
            status=status,
            total_pages=total_pages,
            processed_pages=processed_pages,
            unprocessed_visual_pages=skipped_visual_pages,
            skipped_visual_block_count=skipped_visual_block_count,
            reasons=(
                ["visual_content_not_ingested"] if skipped_visual_block_count else []
            ),
        )
        attach_pdf_coverage_unit(source["segments"], coverage)
    except ValueError:
        raise MineruPdfError("MinerU response is invalid") from None
    source["coverage"] = coverage
    return source


def _validate_embedded_json_budget(value: str) -> None:
    depth = 0
    node_count = 0
    string_chars = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            string_chars += 1
            if string_chars > _MAX_CONTENT_STRING_CHARS:
                raise MineruPdfError("MinerU response exceeds content limits")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            node_count += 1
            if depth > _MAX_EMBEDDED_JSON_DEPTH:
                raise MineruPdfError("MinerU response exceeds content limits")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise MineruPdfError("MinerU response is invalid")
        elif character == ",":
            node_count += 1
        if node_count > _MAX_EMBEDDED_JSON_NODES:
            raise MineruPdfError("MinerU response exceeds content limits")
    if in_string or depth != 0:
        raise MineruPdfError("MinerU response is invalid")


def _embedded_json_list(value: object) -> list[object]:
    if not isinstance(value, str):
        raise MineruPdfError("MinerU response is invalid")
    _validate_embedded_json_budget(value)
    decoder = json.JSONDecoder()
    index = 0

    def skip_space(position: int) -> int:
        while position < len(value) and value[position].isspace():
            position += 1
        return position

    index = skip_space(index)
    if index >= len(value) or value[index] != "[":
        raise MineruPdfError("MinerU response is invalid")
    index += 1
    items: list[object] = []
    while True:
        index = skip_space(index)
        if index < len(value) and value[index] == "]":
            index = skip_space(index + 1)
            if index != len(value):
                raise MineruPdfError("MinerU response is invalid")
            return items
        if len(items) >= _MAX_CONTENT_ITEMS:
            raise MineruPdfError("MinerU response exceeds content limits")
        try:
            item, index = decoder.raw_decode(value, index)
        except json.JSONDecodeError:
            raise MineruPdfError("MinerU response is invalid") from None
        except RecursionError:
            raise MineruPdfError("MinerU response exceeds content limits") from None
        except ValueError:
            raise MineruPdfError("MinerU response is invalid") from None
        items.append(item)
        index = skip_space(index)
        if index >= len(value) or value[index] not in {",", "]"}:
            raise MineruPdfError("MinerU response is invalid")
        if value[index] == "]":
            index = skip_space(index + 1)
            if index != len(value):
                raise MineruPdfError("MinerU response is invalid")
            return items
        index += 1


def _response_result(
    payload: object,
    *,
    expected_version: str,
    total_pages: int | None = None,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise MineruPdfError("MinerU response is invalid")
    version = payload.get("version")
    if version != expected_version or expected_version != EXPECTED_MINERU_VERSION:
        raise MineruPdfError("MinerU service version mismatch")
    if "content_list" in payload:
        result = payload
    else:
        results = payload.get("results")
        if not isinstance(results, Mapping) or len(results) != 1:
            raise MineruPdfError("MinerU response is invalid")
        result = next(iter(results.values()))
        if not isinstance(result, Mapping):
            raise MineruPdfError("MinerU response is invalid")
    content_list = _embedded_json_list(result.get("content_list"))
    if not isinstance(result.get("middle_json"), str):
        raise MineruPdfError("MinerU response is invalid")
    return {"content_list": content_list, "total_pages": total_pages}

def map_mineru_response(
    payload: object,
    *,
    expected_version: str,
    chunk_size: int,
    chunk_overlap: int,
    total_pages: int | None = None,
) -> dict[str, Any]:
    """Validate a MinerU API envelope and map only trusted content-list blocks."""
    result = _response_result(
        payload,
        expected_version=expected_version,
        total_pages=total_pages,
    )
    return map_mineru_content_list(
        result["content_list"],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        version=expected_version,
        total_pages=result["total_pages"],
    )

def _endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    return value if value.endswith("/file_parse") else f"{value}/file_parse"


def _read_bounded_response(response: httpx.Response, max_response_bytes: int) -> bytearray:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            content_length = int(length)
        except ValueError:
            raise MineruPdfError("MinerU response is invalid") from None
        if content_length > max_response_bytes:
            raise MineruPdfError("MinerU response exceeds configured limit")
    buffer = bytearray()
    for part in response.iter_bytes():
        if len(part) > max_response_bytes - len(buffer):
            raise MineruPdfError("MinerU response exceeds configured limit")
        buffer.extend(part)
    return buffer
def parse_pdf_remote(
    data: bytes | Path,
    *,
    base_url: str,
    expected_version: str,
    timeout_seconds: float,
    max_response_bytes: int,
    chunk_size: int,
    chunk_overlap: int,
    total_pages: int | None = None,
) -> dict[str, Any]:
    """Synchronously invoke MinerU using a bounded multipart request and response."""
    max_response_bytes = min(max_response_bytes, _MAX_RESPONSE_BYTES)
    if not base_url.strip():
        raise MineruPdfError("MinerU service is not configured")
    if expected_version != EXPECTED_MINERU_VERSION:
        raise MineruPdfError("MinerU service version mismatch")
    handle: BinaryIO
    should_close = False
    if isinstance(data, Path):
        try:
            handle = data.open("rb")
            should_close = True
        except OSError:
            raise MineruPdfError("PDF input is unavailable") from None
    else:
        handle = io.BytesIO(data)
    form = {
        "backend": "hybrid-engine",
        "effort": "high",
        "parse_method": "ocr",
        "formula_enable": "true",
        "table_enable": "true",
        "image_analysis": "true",
        "return_content_list": "true",
        "return_middle_json": "true",
        "return_md": "false",
        "return_model_output": "false",
        "return_images": "false",
    }
    try:
        timeout = httpx.Timeout(timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client, client.stream(
            "POST",
            _endpoint(base_url),
            data=form,
            files={"files": ("document.pdf", handle, "application/pdf")},
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise MineruPdfError("MinerU request failed")
            raw = _read_bounded_response(response, max_response_bytes)
    except MineruPdfError:
        raise
    except httpx.TimeoutException:
        raise MineruPdfError("MinerU request timed out") from None
    except httpx.RequestError:
        raise MineruPdfError("MinerU request failed") from None
    except Exception:  # noqa: BLE001 - provider and file errors must not disclose internals
        raise MineruPdfError("MinerU request failed") from None
    finally:
        if should_close:
            try:
                handle.close()
            except OSError:
                pass
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MineruPdfError("MinerU response is invalid") from None
    return map_mineru_response(
        payload,
        expected_version=expected_version,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        total_pages=total_pages,
    )
