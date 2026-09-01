"""Typed, source-agnostic EvidenceLocatorV1 contract.

This module is intentionally pure.  M0 defines and validates the contract but
does not attach it to import, evidence, embedding, or retrieval write paths.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Sha256Hex = str
EVIDENCE_LOCATOR_CONTRACT_VERSION = "v1"
SourceKind = Literal["text", "pdf", "doc", "docx", "xlsx", "xls", "csv", "json", "image"]
UnitKind = Literal[
    "section",
    "chunk",
    "structured_unit",
    "table",
    "row",
    "cell",
    "image_region",
]
ExtractionMode = Literal["native", "ocr", "mixed", "unparsed", "not_applicable"]
ProvenanceStatus = Literal["verified", "legacy_unverified"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JSON_POINTER_ESCAPE = re.compile(r"(?:[^~]|~[01])*")
_EXCEL_COLUMN = re.compile(r"^[A-Z]{1,3}$")
_CELL = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")


def _non_blank(value: str, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be nonblank and bounded")
    return value


def _sha256(value: str, *, label: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def sha256_text(value: str) -> str:
    """Return the digest used for exact unit and quote text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _LocatorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextRangeV1(_LocatorModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    sha256: Sha256Hex | None = None

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        return _sha256(value, label="text range hash") if value is not None else None

    @model_validator(mode="after")
    def validate_range(self) -> TextRangeV1:
        if self.end < self.start:
            raise ValueError("text range end must be greater than or equal to start")
        return self


class TextSpanV1(_LocatorModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    ranges: list[TextRangeV1] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_span(self) -> TextSpanV1:
        if self.end < self.start:
            raise ValueError("text span end must be greater than or equal to start")
        previous_end = self.start
        for item in self.ranges:
            if item.start < self.start or item.end > self.end:
                raise ValueError("text ranges must be contained by the text span")
            if item.start < previous_end:
                raise ValueError("text ranges must be ordered and non-overlapping")
            previous_end = item.end
        return self


class PageSpanV1(_LocatorModel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> PageSpanV1:
        if self.end < self.start:
            raise ValueError("page end must be greater than or equal to start")
        return self


class TableLocatorV1(_LocatorModel):
    index: int = Field(ge=0)
    name: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return _non_blank(value, label="table name", limit=512) if value is not None else None


class SheetLocatorV1(_LocatorModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _non_blank(value, label="sheet name", limit=255)


class RowSpanV1(_LocatorModel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> RowSpanV1:
        if self.end < self.start:
            raise ValueError("row end must be greater than or equal to start")
        return self


def _normalize_column(value: int | str) -> int | str:
    if isinstance(value, bool):
        raise ValueError("column must be a positive integer or Excel column key")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("column number must be positive")
        return value
    if not isinstance(value, str):
        raise ValueError("column must be a positive integer or Excel column key")
    value = value.strip().upper()
    if value.isdigit():
        number = int(value)
        if number < 1:
            raise ValueError("column number must be positive")
        return number
    if not _EXCEL_COLUMN.fullmatch(value):
        raise ValueError("column key must contain one to three letters")
    return value


def _column_number(value: int | str) -> int:
    if isinstance(value, int):
        return value
    number = 0
    for letter in value:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _cell_coordinates(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", value)
    if match is None:  # pragma: no cover - CellSpanV1 validates this first.
        raise ValueError("cell must use an A1-style coordinate")
    return _column_number(match.group(1)), int(match.group(2))


class ColumnSpanV1(_LocatorModel):
    start: int | str
    end: int | str

    @field_validator("start", "end", mode="before")
    @classmethod
    def normalize_columns(cls, value: int | str) -> int | str:
        return _normalize_column(value)

    @model_validator(mode="after")
    def validate_range(self) -> ColumnSpanV1:
        if type(self.start) is not type(self.end):
            raise ValueError("column range endpoints must use the same coordinate form")
        if _column_number(self.end) < _column_number(self.start):
            raise ValueError("column end must be greater than or equal to start")
        return self


class CellSpanV1(_LocatorModel):
    start: str = Field(min_length=2, max_length=32)
    end: str = Field(min_length=2, max_length=32)

    @field_validator("start", "end")
    @classmethod
    def normalize_cell(cls, value: str) -> str:
        value = _non_blank(value, label="cell", limit=32).upper()
        if not _CELL.fullmatch(value):
            raise ValueError("cell must use an A1-style coordinate")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> CellSpanV1:
        start_column, start_row = _cell_coordinates(self.start)
        end_column, end_row = _cell_coordinates(self.end)
        if end_column < start_column or end_row < start_row:
            raise ValueError("cell end must not precede cell start")
        return self


class BoundingBoxV1(_LocatorModel):
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    coordinate_system: str = Field(min_length=1, max_length=64)
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)

    @field_validator("coordinate_system")
    @classmethod
    def normalize_coordinate_system(cls, value: str) -> str:
        return _non_blank(value, label="coordinate system", limit=64)

    @model_validator(mode="after")
    def validate_box(self) -> BoundingBoxV1:
        values = (self.x_min, self.y_min, self.x_max, self.y_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("bounding box coordinates must be finite")
        if self.x_max < self.x_min or self.y_max < self.y_min:
            raise ValueError("bounding box maximums must not precede minimums")
        return self


class SourceLocatorV1(_LocatorModel):
    kind: SourceKind
    file_name: str | None = Field(default=None, max_length=512)
    page: PageSpanV1 | None = None
    text: TextSpanV1 | None = None
    heading_path: list[str] = Field(default_factory=list, max_length=64)
    table: TableLocatorV1 | None = None
    sheet: SheetLocatorV1 | None = None
    row: RowSpanV1 | None = None
    column: ColumnSpanV1 | None = None
    cell: CellSpanV1 | None = None
    json_pointer: str | None = Field(default=None, max_length=4096)
    bbox: BoundingBoxV1 | None = None

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str | None) -> str | None:
        return _non_blank(value, label="file name", limit=512) if value is not None else None

    @field_validator("heading_path")
    @classmethod
    def normalize_heading_path(cls, values: list[str]) -> list[str]:
        return [_non_blank(value, label="heading", limit=512) for value in values]

    @field_validator("json_pointer")
    @classmethod
    def validate_json_pointer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 4096 or (value and not value.startswith("/")):
            raise ValueError("JSON Pointer must be empty or start with '/'")
        if not _JSON_POINTER_ESCAPE.fullmatch(value.replace("/", "")):
            raise ValueError("JSON Pointer contains an invalid escape")
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> SourceLocatorV1:
        if self.sheet is not None and self.kind not in {"xlsx", "xls"}:
            raise ValueError("sheet locator requires an XLSX or XLS source")
        if any(value is not None for value in (self.row, self.column, self.cell)):
            if self.kind not in {"xlsx", "xls", "csv"} and not (
                self.kind in {"doc", "docx"} and self.table is not None
            ):
                raise ValueError("row, column, and cell locators require a tabular source or DOCX table")
        if self.json_pointer is not None and self.kind != "json":
            raise ValueError("JSON Pointer requires a JSON source")
        if self.bbox is not None and self.kind not in {"pdf", "doc", "docx", "image"}:
            raise ValueError("bounding boxes require a page or image source")
        if self.page is not None and self.kind not in {"text", "pdf", "docx", "image"}:
            raise ValueError("page locator requires a paged source")
        return self


class ParserProvenanceV1(_LocatorModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    config_hash: Sha256Hex | None = None

    @field_validator("name", "version")
    @classmethod
    def normalize_value(cls, value: str, info) -> str:
        return _non_blank(value, label=info.field_name, limit=128 if info.field_name == "name" else 64)

    @field_validator("config_hash")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        return _sha256(value, label="parser config hash") if value is not None else None


class EvidenceQualityV1(_LocatorModel):
    extraction_mode: ExtractionMode = "not_applicable"
    ocr_engine: str | None = Field(default=None, max_length=128)
    ocr_engine_version: str | None = Field(default=None, max_length=64)
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("ocr_engine", "ocr_engine_version")
    @classmethod
    def normalize_engine(cls, value: str | None, info) -> str | None:
        return _non_blank(value, label=info.field_name, limit=128 if info.field_name == "ocr_engine" else 64) if value is not None else None

    @model_validator(mode="after")
    def validate_ocr_shape(self) -> EvidenceQualityV1:
        if self.ocr_confidence is not None and self.extraction_mode not in {"ocr", "mixed"}:
            raise ValueError("OCR confidence requires OCR or mixed extraction mode")
        if self.extraction_mode not in {"ocr", "mixed"} and (
            self.ocr_engine is not None or self.ocr_engine_version is not None
        ):
            raise ValueError("OCR engine metadata requires OCR or mixed extraction mode")
        return self


class EvidenceLocatorV1(_LocatorModel):
    locator_version: Literal["v1"] = EVIDENCE_LOCATOR_CONTRACT_VERSION
    document_id: UUID
    document_revision_id: UUID
    revision_no: int = Field(ge=1)
    document_revision_file_id: UUID | None = None
    raw_file_sha256: Sha256Hex | None = None
    normalized_content_hash: Sha256Hex | None = None
    unit_id: UUID
    parent_unit_id: UUID | None = None
    unit_kind: UnitKind
    ordinal: int = Field(ge=0)
    parser: ParserProvenanceV1
    source: SourceLocatorV1
    quality: EvidenceQualityV1 = Field(default_factory=EvidenceQualityV1)
    unit_text_sha256: Sha256Hex | None = None
    quote_sha256: Sha256Hex | None = None
    provenance_status: ProvenanceStatus = "verified"

    @field_validator("raw_file_sha256", "normalized_content_hash", "unit_text_sha256", "quote_sha256")
    @classmethod
    def validate_hash(cls, value: str | None, info) -> str | None:
        return _sha256(value, label=info.field_name) if value is not None else None

    @model_validator(mode="after")
    def validate_identity(self) -> EvidenceLocatorV1:
        if (self.document_revision_file_id is None) != (self.raw_file_sha256 is None):
            raise ValueError("revision file ID and raw file hash must be provided together")
        if self.provenance_status == "verified" and (
            self.unit_text_sha256 is None or self.quote_sha256 is None
        ):
            raise ValueError("verified locator requires unit and quote hashes")
        if self.parent_unit_id == self.unit_id:
            raise ValueError("a locator cannot be its own parent")
        return self


def normalize_evidence_locator_payload(
    value: EvidenceLocatorV1 | Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a locator and return JSON-safe, null-free normalized data."""
    locator = value if isinstance(value, EvidenceLocatorV1) else EvidenceLocatorV1.model_validate(value)
    return locator.model_dump(mode="json", exclude_none=True)


def validate_parent_relationship(parent: EvidenceLocatorV1, child: EvidenceLocatorV1) -> None:
    """Validate the cross-row parent invariant without database access."""
    if parent.document_id != child.document_id or parent.document_revision_id != child.document_revision_id:
        raise ValueError("parent and child must belong to the same document revision")
    if parent.revision_no != child.revision_no:
        raise ValueError("parent and child must use the same revision number")
    if child.parent_unit_id != parent.unit_id:
        raise ValueError("child parent_unit_id must identify the supplied parent")
    if parent.unit_id == child.unit_id:
        raise ValueError("a locator cannot be its own parent")
    if parent.ordinal >= child.ordinal:
        raise ValueError("parent must precede child in document order")


def legacy_evidence_fallback(
    record: Mapping[str, Any],
    *,
    parser_name: str = "legacy",
    parser_version: str = "legacy",
) -> EvidenceLocatorV1:
    """Normalize a legacy scalar evidence row without fabricating provenance.

    Missing raw-file identity and text/quote hashes are allowed only because the
    returned locator is explicitly marked ``legacy_unverified``.
    """
    metadata = record.get("evidence_metadata") or record.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    position = record.get("position") or metadata.get("location") or {}
    if not isinstance(position, Mapping):
        position = {}

    source_kind = record.get("source_kind") or metadata.get("source_kind") or "text"
    source: dict[str, Any] = {"kind": source_kind}
    title_path = record.get("title_path") or metadata.get("title_path")
    if isinstance(title_path, list):
        source["heading_path"] = title_path
    page_start = record.get("page_start") or position.get("page_start") or position.get("page")
    page_end = record.get("page_end") or position.get("page_end") or page_start
    if page_start is not None:
        source["page"] = {"start": page_start, "end": page_end}
    source_start = record.get("source_start")
    source_end = record.get("source_end")
    if source_start is not None and source_end is not None:
        source["text"] = {"start": source_start, "end": source_end}

    text_quote = record.get("text_quote")
    unit_text = record.get("unit_text") or record.get("text")
    return EvidenceLocatorV1(
        document_id=record["document_id"],
        document_revision_id=record["document_revision_id"],
        revision_no=record["revision_no"],
        document_revision_file_id=record.get("document_revision_file_id"),
        raw_file_sha256=record.get("raw_file_sha256"),
        normalized_content_hash=record.get("normalized_content_hash"),
        unit_id=record.get("unit_id") or record["evidence_id"],
        parent_unit_id=record.get("parent_unit_id") or record.get("document_block_id"),
        unit_kind=record.get("unit_kind") or record.get("evidence_kind", "chunk"),
        ordinal=record.get("ordinal", 0),
        parser=ParserProvenanceV1(name=parser_name, version=parser_version),
        source=source,
        unit_text_sha256=(sha256_text(unit_text) if isinstance(unit_text, str) else None),
        quote_sha256=(
            record.get("quote_sha256")
            or record.get("text_quote_hash")
            or (sha256_text(text_quote) if isinstance(text_quote, str) else None)
        ),
        provenance_status="legacy_unverified",
    )
