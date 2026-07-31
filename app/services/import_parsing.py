from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.models.library import Library
from app.services import docx_extract, pdf_extract, splitter, xlsx_extract


@dataclass(frozen=True, slots=True)
class ParsedImport:
    normalized_text: str
    chunks: list[dict]
    splitter_name: str


def _read_utf8(path: Path) -> str:
    parts: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        while data := handle.read(1024 * 1024):
            parts.append(data)
    return "".join(parts)


def _structured_text(text: str, library: Library, *, source_type: str) -> ParsedImport:
    chunks = splitter.split_structured_text(
        text,
        chunk_size=library.chunk_size,
        chunk_overlap=library.chunk_overlap,
        splitter="text",
        base_location={"type": source_type},
    )
    if not chunks:
        raise ValueError("file contains no importable text")
    return ParsedImport(text, chunks, "text")


def _parse_csv(path: Path, library: Library) -> ParsedImport:
    rows: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            line = " | ".join(cell.strip() for cell in row).strip(" |")
            if line:
                rows.append(line)
    return _structured_text("\n".join(rows), library, source_type="csv")


def _parse_json(path: Path, library: Library) -> ParsedImport:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return _structured_text(text, library, source_type="json")


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
            ocr_service.ocr_image
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
        return ParsedImport(source["normalized_text"], source["chunks"], "text")
    if suffix == ".docx":
        from app.services import ocr as ocr_service

        ocr_enabled = (
            library.ocr_enabled
            if library.ocr_enabled is not None
            else settings.ocr_enabled
        )
        ocr_callback = (
            ocr_service.ocr_image
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
                source["normalized_text"], source["chunks"], "docx"
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
        return ParsedImport(source["normalized_text"], source["chunks"], "docx")
    if suffix == ".csv":
        return _parse_csv(path, library)
    if suffix == ".json":
        return _parse_json(path, library)
    if suffix in {".txt", ".md", ".markdown"}:
        return _structured_text(_read_utf8(path), library, source_type="text")
    raise ValueError(f"unsupported file type: {suffix or '(none)'}")
