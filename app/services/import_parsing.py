from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.models.library import Library
from app.services import docx_extract, pdf_extract, splitter, xlsx_extract
from app.services.parser_units import (
    build_parser_unit,
    excel_column_name,
    parser_provenance,
    text_range,
)


@dataclass(frozen=True, slots=True)
class ParsedImport:
    normalized_text: str
    chunks: list[dict]
    splitter_name: str
    segments: list[dict] = field(default_factory=list)


RESOURCE_LIMIT_ERROR = "import content exceeds parser resource limits"

# These limits protect the two structured formats without changing the shared
# application settings or the chunked staging upload contract.
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 1_024
MAX_CSV_CELLS = 1_000_000
MAX_FANOUT_DOCUMENTS = 1_000
MAX_NORMALIZED_TEXT_CHARS = 10_000_000


class ImportResourceLimitError(ValueError):
    """Raised when structured input exceeds a parser resource budget."""

    status_code = 413

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__(RESOURCE_LIMIT_ERROR)


def validate_json_resource_budget(value: object) -> None:
    """Validate JSON shape before building parser units or ingest documents."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise ImportResourceLimitError
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def validate_csv_row_budget(
    row_number: int,
    row: object,
    total_cells: int,
) -> int:
    """Return the new cell count after validating one parsed CSV row."""
    try:
        column_count = len(row)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ImportResourceLimitError from exc
    if row_number > MAX_CSV_ROWS or column_count > MAX_CSV_COLUMNS:
        raise ImportResourceLimitError
    total_cells += column_count
    if total_cells > MAX_CSV_CELLS:
        raise ImportResourceLimitError
    return total_cells


def validate_fanout_document_count(count: int) -> None:
    if count > MAX_FANOUT_DOCUMENTS:
        raise ImportResourceLimitError


def validate_normalized_text_budget(total_chars: int) -> None:
    if total_chars > MAX_NORMALIZED_TEXT_CHARS:
        raise ImportResourceLimitError


def _read_utf8(path: Path) -> str:
    parts: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        while data := handle.read(1024 * 1024):
            parts.append(data)
    return "".join(parts)


def _structured_text(
    text: str,
    library: Library,
    *,
    source_type: str,
    segments: list[dict] | None = None,
) -> ParsedImport:
    chunks = splitter.split_structured_text(
        text,
        chunk_size=library.chunk_size,
        chunk_overlap=library.chunk_overlap,
        splitter="text",
        base_location={"type": source_type},
    )
    if not chunks:
        raise ValueError("file contains no importable text")
    if segments is None:
        parser = parser_provenance("builtin-text", "v1", {"source_kind": source_type})
        segments = [{
            "kind": "prose",
            "text": text,
            "source_kind": source_type,
            "ordinal": 0,
            "unit_key": f"{source_type}:segment:0",
            "parser": parser,
            "location": {"type": source_type},
            "parser_unit": build_parser_unit(
                source_kind=source_type,
                unit_kind="section",
                ordinal=0,
                unit_key=f"{source_type}:segment:0",
                parser=parser,
                location={"type": source_type},
                source_text=text,
                source_start=0,
                source_end=len(text),
            ),
        }]
    return ParsedImport(text, chunks, "text", segments)


def _parse_csv(path: Path, library: Library) -> ParsedImport:
    rows: list[str] = []
    segments: list[dict] = []
    total_cells = 0
    normalized_chars = 0
    parser = parser_provenance("python-csv", "v1", {"encoding": "utf-8-sig"})
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row_number, row in enumerate(reader, start=1):
            total_cells = validate_csv_row_budget(row_number, row, total_cells)
            line = " | ".join(cell.strip() for cell in row).strip(" |")
            if line:
                normalized_chars += len(line) + (1 if rows else 0)
                validate_normalized_text_budget(normalized_chars)
                rows.append(line)
                cells: list[dict] = []
                row_key = f"csv:row:{row_number}"
                for column, cell in enumerate(row, start=1):
                    if not cell.strip():
                        continue
                    cells.append({
                        **build_parser_unit(
                            source_kind="csv",
                            unit_kind="cell",
                            ordinal=column - 1,
                            unit_key=f"{row_key}:cell:{excel_column_name(column)}{row_number}",
                            parser=parser,
                            parent_key=row_key,
                            location={"type": "csv_row", "row": row_number},
                            source={
                                "row": {"start": row_number, "end": row_number},
                                "column": {"start": column, "end": column},
                                "cell": {
                                    "start": f"{excel_column_name(column)}{row_number}",
                                    "end": f"{excel_column_name(column)}{row_number}",
                                },
                            },
                        ),
                        "value": cell.strip(),
                    })
                segment = {
                    "kind": "prose",
                    "text": line,
                    "source_kind": "csv",
                    "ordinal": len(segments),
                    "unit_key": row_key,
                    "parser": parser,
                    "location": {"type": "csv_row", "row": row_number},
                    "structured_units": cells,
                }
                segment["parser_unit"] = build_parser_unit(
                    source_kind="csv",
                    unit_kind="row",
                    ordinal=len(segments),
                    unit_key=row_key,
                    parser=parser,
                    location=segment["location"],
                )
                segments.append(segment)
    normalized_text = "\n".join(rows)
    offset = 0
    for segment in segments:
        end = offset + len(segment["text"])
        segment["parser_unit"]["source"]["text"] = {
            "start": offset,
            "end": end,
            "ranges": [text_range(normalized_text, offset, end)],
        }
        offset = end + 1
    return _structured_text(normalized_text, library, source_type="csv", segments=segments)


def _json_unit_key(pointer: str) -> str:
    if not pointer:
        return "json:root"
    return f"json:pointer:{hashlib.sha256(pointer.encode('utf-8')).hexdigest()[:24]}"


def _bounded_json_node(value) -> tuple[object, str]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) <= 4096:
        return value, encoded
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    summary = {
        "truncated": True,
        "sha256": digest,
        "json_value_kind": type(value).__name__,
    }
    return summary, f"<truncated {type(value).__name__} sha256={digest}>"


def _json_pointer_units(value, *, parser: dict[str, str]) -> list[dict]:
    units: list[dict] = []

    def walk(node, pointer: str, parent_key: str | None) -> None:
        unit_key = _json_unit_key(pointer)
        json_value, text = _bounded_json_node(node)
        units.append({
            **build_parser_unit(
                source_kind="json",
                unit_kind="structured_unit",
                ordinal=len(units),
                unit_key=unit_key,
                parser=parser,
                source={"json_pointer": pointer},
                parent_key=parent_key,
            ),
            "json_value_kind": type(node).__name__,
            "json_value": json_value,
            "text": text,
        })
        if isinstance(node, dict):
            for key, child in node.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                child_pointer = f"{pointer}/{escaped}" if pointer else f"/{escaped}"
                walk(child, child_pointer, unit_key)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                child_pointer = f"{pointer}/{index}" if pointer else f"/{index}"
                walk(child, child_pointer, unit_key)

    walk(value, "", "json:document")
    return units


def _parse_json(path: Path, library: Library) -> ParsedImport:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
    except RecursionError as exc:
        raise ImportResourceLimitError from exc
    validate_json_resource_budget(value)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except RecursionError as exc:
        raise ImportResourceLimitError from exc
    validate_normalized_text_budget(len(text))
    parser = parser_provenance("python-json", "v1", {"indent": 2, "ensure_ascii": False})
    segment = {
        "kind": "prose",
        "text": text,
        "source_kind": "json",
        "ordinal": 0,
        "unit_key": "json:document",
        "parser": parser,
        "location": {"type": "json"},
        "structured_units": _json_pointer_units(value, parser=parser),
    }
    segment["parser_unit"] = build_parser_unit(
        source_kind="json",
        unit_kind="section",
        ordinal=0,
        unit_key="json:document",
        parser=parser,
        location=segment["location"],
        source_text=text,
        source_start=0,
        source_end=len(text),
    )
    return _structured_text(text, library, source_type="json", segments=[segment])


def parse_import_file(
    path: Path,
    library: Library,
    *,
    file_name: str | None = None,
) -> ParsedImport:
    suffix = Path(file_name).suffix.lower() if file_name else path.suffix.lower()
    if suffix == ".pdf":
        from app.services import ocr as ocr_service

        ocr_enabled = (
            library.ocr_enabled
            if library.ocr_enabled is not None
            else settings.ocr_enabled
        )
        ocr_callback = (
            ocr_service.ocr_image_blocks
            if ocr_enabled and ocr_service.is_available()
            else None
        )
        source = pdf_extract.build_pdf_source(
            path,
            chunk_size=library.chunk_size,
            chunk_overlap=library.chunk_overlap,
            ocr_enabled=bool(ocr_enabled),
            ocr=ocr_callback,
            min_text_chars=settings.pdf_ocr_min_text_chars,
            render_dpi=settings.pdf_ocr_render_dpi,
            max_ocr_pages=settings.pdf_ocr_max_pages,
        )
        return ParsedImport(source["normalized_text"], source["chunks"], "text", source.get("segments", []))
    if suffix == ".docx":
        from app.services import ocr as ocr_service

        ocr_enabled = (
            library.ocr_enabled
            if library.ocr_enabled is not None
            else settings.ocr_enabled
        )
        ocr_callback = (
            ocr_service.ocr_image_blocks
            if ocr_enabled and ocr_service.is_available()
            else None
        )
        table_aware = (
            library.docx_table_aware
            if library.docx_table_aware is not None
            else settings.docx_table_aware
        )
        if table_aware:
            source = splitter.build_structured_source_from_segments(
                docx_extract.extract_docx_segments(path, ocr=ocr_callback),
                chunk_size=library.chunk_size,
                chunk_overlap=library.chunk_overlap,
            )
            if not source["chunks"]:
                raise ValueError("DOCX contains no importable text")
            return ParsedImport(
                source["normalized_text"], source["chunks"], "docx", source.get("segments", [])
            )
        return _structured_text(
            docx_extract.extract_docx_text(path, ocr=ocr_callback),
            library,
            source_type="docx",
        )
    if suffix == ".xlsx":
        source = splitter.build_structured_source_from_segments(
            xlsx_extract.extract_xlsx_segments(path),
            chunk_size=library.chunk_size,
            chunk_overlap=library.chunk_overlap,
        )
        if not source["chunks"]:
            raise ValueError("spreadsheet contains no importable text")
        return ParsedImport(source["normalized_text"], source["chunks"], "docx", source.get("segments", []))
    if suffix == ".csv":
        return _parse_csv(path, library)
    if suffix == ".json":
        return _parse_json(path, library)
    if suffix in {".txt", ".md", ".markdown"}:
        return _structured_text(_read_utf8(path), library, source_type="text")
    raise ValueError(f"unsupported file type: {suffix or '(none)'}")
