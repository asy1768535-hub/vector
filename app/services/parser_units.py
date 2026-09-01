"""Domain-neutral intermediate parser units used before persistence.

Parser units deliberately do not contain database IDs.  They carry only
coordinates and provenance that the source parser can actually establish.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, Iterable, Literal, TypedDict

from pydantic import ValidationError

from app.schemas.evidence_locator import (
    EvidenceQualityV1,
    ParserProvenanceV1,
    SourceLocatorV1,
    TextRangeV1,
)


ParserSourceKind = Literal["text", "pdf", "doc", "docx", "xlsx", "xls", "csv", "json", "image"]
ParserUnitKind = Literal["section", "chunk", "structured_unit", "table", "row", "cell"]
PARSER_UNIT_CONTRACT_VERSION = "v1"
MAX_UNIT_KEY_LENGTH = 512


class ParserRangeV1(TypedDict):
    start: int
    end: int
    sha256: str


class ParserUnitV1(TypedDict, total=False):
    version: Literal["v1"]
    unit_key: str
    unit_kind: ParserUnitKind
    source_kind: ParserSourceKind
    ordinal: int
    parent_key: str | None
    section_path: list[str]
    parser: dict[str, str]
    source: dict[str, Any]
    quality: dict[str, Any]
    source_ranges: list[ParserRangeV1]
    text: str


class ParserSegmentV1(TypedDict, total=False):
    kind: str
    text: str
    rows: list[str]
    parser_unit: ParserUnitV1
    structured_units: list[ParserUnitV1]


def excel_column_name(number: int) -> str:
    if number < 1:
        raise ValueError("Excel column number must be positive")
    letters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def parser_config_hash(config: Mapping[str, Any] | None = None) -> str:
    payload = json.dumps(dict(config or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parser_provenance(name: str, version: str, config: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {
        "name": name,
        "version": version,
        "config_hash": parser_config_hash(config),
    }


def _unit_key(value: str, *, label: str = "unit key") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > MAX_UNIT_KEY_LENGTH or "\x00" in value:
        raise ValueError(f"{label} must be non-empty and at most {MAX_UNIT_KEY_LENGTH} characters")
    return value


def text_range(text: str, start: int, end: int) -> ParserRangeV1:
    if start < 0 or end < start or end > len(text):
        raise ValueError("parser text range is outside the supplied text")
    return {
        "start": start,
        "end": end,
        "sha256": hashlib.sha256(text[start:end].encode("utf-8")).hexdigest(),
    }


def _heading_path(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split("/") if part.strip()]
    return []


def source_from_location(
    location: Mapping[str, Any] | None,
    *,
    start: int | None = None,
    end: int | None = None,
    text: str | None = None,
    ranges: list[ParserRangeV1] | None = None,
) -> dict[str, Any]:
    """Map existing parser locations to the EvidenceLocator-shaped source part."""
    location = location or {}
    source: dict[str, Any] = {}
    if start is not None and end is not None:
        source["text"] = {
            "start": start,
            "end": end,
            "ranges": ranges or ([text_range(text, start, end)] if text is not None else []),
        }

    location_type = location.get("type")
    if location_type == "page" and location.get("page") is not None:
        page = int(location["page"])
        source["page"] = {"start": page, "end": page}
    elif location_type == "table":
        index = location.get("table_index")
        if index is not None:
            source["table"] = {"index": max(0, int(index) - 1)}
    elif location_type in {"sheet", "sheet_row"} and location.get("sheet"):
        source["sheet"] = {"name": str(location["sheet"])}
        if location_type == "sheet_row":
            source["row"] = {
                "start": int(location["start_row"]),
                "end": int(location["end_row"]),
            }
    elif location_type == "csv_row" and location.get("row") is not None:
        row = int(location["row"])
        source["row"] = {"start": row, "end": row}

    headings = _heading_path(location.get("heading"))
    if headings:
        source["heading_path"] = headings
    return source


def build_parser_unit(
    *,
    source_kind: ParserSourceKind,
    unit_kind: ParserUnitKind,
    ordinal: int,
    unit_key: str,
    parser: Mapping[str, str],
    location: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    source_text: str | None = None,
    source_start: int | None = None,
    source_end: int | None = None,
    source_ranges: list[ParserRangeV1] | None = None,
    parent_key: str | None = None,
    section_path: list[str] | None = None,
    quality: Mapping[str, Any] | None = None,
) -> ParserUnitV1:
    unit: ParserUnitV1 = {
        "version": PARSER_UNIT_CONTRACT_VERSION,
        "unit_key": _unit_key(unit_key),
        "unit_kind": unit_kind,
        "source_kind": source_kind,
        "ordinal": ordinal,
        "parent_key": _unit_key(parent_key, label="parent key") if parent_key is not None else None,
        "section_path": list(section_path or []),
        "parser": dict(parser),
        "source": source_from_location(
            location,
            start=source_start,
            end=source_end,
            text=source_text,
            ranges=source_ranges,
        ),
        "quality": dict(quality or {}),
    }
    if source:
        unit["source"].update(copy.deepcopy(dict(source)))
    if source_ranges:
        unit["source_ranges"] = list(source_ranges)
    return unit


def _ocr_parser(parser: Mapping[str, str], block: Mapping[str, Any]) -> dict[str, str]:
    name = block.get("parser")
    if not isinstance(name, str) or not name.strip():
        name = "ocr"
    return parser_provenance(
        name.strip(),
        "unknown",
        {"source_parser": parser.get("name"), "source_version": parser.get("version")},
    )


def build_ocr_parser_units(
    blocks: list[Mapping[str, Any]],
    *,
    source_kind: ParserSourceKind,
    parser: Mapping[str, str],
    parent_key: str | None,
    extraction_mode: Literal["ocr", "mixed"],
    unit_text: str,
    normalized_text: str | None = None,
    normalized_offset: int = 0,
    page: int | None = None,
) -> list[ParserUnitV1]:
    """Turn trusted OCR lines into bounded image-region parser units."""
    units: list[ParserUnitV1] = []
    cursor = 0
    for block in blocks:
        value = block.get("text")
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        local_start = unit_text.find(value, cursor)
        local_end = local_start + len(value) if local_start >= 0 else -1
        cursor = local_end if local_end >= 0 else cursor

        source: dict[str, Any] = {}
        if page is not None:
            source["page"] = {"start": page, "end": page}
        bbox = block.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                numbers = [float(item) for item in bbox]
            except (TypeError, ValueError):
                numbers = []
            if numbers and numbers[2] >= numbers[0] and numbers[3] >= numbers[1]:
                source["bbox"] = {
                    "x_min": numbers[0],
                    "y_min": numbers[1],
                    "x_max": numbers[2],
                    "y_max": numbers[3],
                    "coordinate_system": "image_pixels",
                }
        if "bbox" not in source:
            continue

        quality: dict[str, Any] = {"extraction_mode": extraction_mode}
        confidence = block.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            if math.isfinite(float(confidence)) and 0 <= float(confidence) <= 1:
                quality["ocr_confidence"] = float(confidence)

        kwargs: dict[str, Any] = {
            "source_kind": source_kind,
            "unit_kind": "image_region",
            "ordinal": len(units),
            "unit_key": f"{parent_key}:image:{len(units)}" if parent_key else f"{source_kind}:image:{len(units)}",
            "parser": _ocr_parser(parser, block),
            "source": source,
            "parent_key": parent_key,
            "quality": quality,
        }
        if normalized_text is not None and local_start >= 0:
            kwargs.update(
                source_text=normalized_text,
                source_start=normalized_offset + local_start,
                source_end=normalized_offset + local_end,
            )
        unit = build_parser_unit(**kwargs)
        unit["text"] = value
        units.append(unit)
    return units


def validate_parser_unit_contract(unit: Mapping[str, Any]) -> None:
    """Validate the runtime parts shared with EvidenceLocatorV1."""
    unit_kinds = {"section", "chunk", "structured_unit", "table", "row", "cell", "image_region"}
    if unit.get("version") != "v1":
        raise ValueError("parser unit version must be v1")
    _unit_key(unit.get("unit_key"), label="unit key")
    if unit.get("unit_kind") not in unit_kinds:
        raise ValueError("parser unit kind is not supported by EvidenceLocatorV1")
    ordinal = unit.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("parser unit ordinal must be a non-negative integer")
    source = unit.get("source") or {}
    quality = unit.get("quality") or {}
    if not isinstance(source, Mapping) or not isinstance(quality, Mapping):
        raise ValueError("parser unit source and quality must be mappings")
    quality_payload = {
        key: quality[key]
        for key in ("extraction_mode", "ocr_engine", "ocr_engine_version", "ocr_confidence")
        if key in quality
    }
    try:
        ParserProvenanceV1.model_validate(unit.get("parser") or {})
        SourceLocatorV1.model_validate({"kind": unit.get("source_kind"), **source})
        EvidenceQualityV1.model_validate(quality_payload)
        for item in unit.get("source_ranges") or []:
            TextRangeV1.model_validate(item)
    except ValidationError as exc:
        raise ValueError("parser unit does not satisfy EvidenceLocatorV1 contract") from exc
    if unit.get("unit_kind") == "image_region" and not source.get("bbox"):
        raise ValueError("image-region parser units require a bounding box")


def validate_parser_unit_hierarchy(units: Iterable[Mapping[str, Any]]) -> None:
    """Validate a flattened segment tree before any database IDs exist."""
    flattened = list(units)
    by_key: dict[str, Mapping[str, Any]] = {}
    for unit in flattened:
        validate_parser_unit_contract(unit)
        key = _unit_key(unit["unit_key"])
        if key in by_key:
            raise ValueError(f"duplicate parser unit key: {key}")
        by_key[key] = unit

    for unit in flattened:
        key = _unit_key(unit["unit_key"])
        parent = unit.get("parent_key")
        if parent is None:
            continue
        parent = _unit_key(parent, label="parent key")
        if parent == key:
            raise ValueError(f"parser unit cannot parent itself: {key}")
        if parent not in by_key:
            raise ValueError(f"dangling parser parent key: {parent}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError(f"parser unit hierarchy cycle: {key}")
        if key in visited:
            return
        visiting.add(key)
        parent = by_key[key].get("parent_key")
        if parent is not None:
            visit(_unit_key(parent, label="parent key"))
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


def rebase_parser_unit(unit: Mapping[str, Any], offset: int) -> ParserUnitV1:
    """Shift only text offsets when a segment is joined into normalized_text."""
    rebased = copy.deepcopy(dict(unit))
    source = rebased.get("source") or {}
    text_span = source.get("text")
    if isinstance(text_span, dict):
        text_span["start"] += offset
        text_span["end"] += offset
        for item in text_span.get("ranges") or []:
            item["start"] += offset
            item["end"] += offset
    for item in rebased.get("source_ranges") or []:
        item["start"] += offset
        item["end"] += offset
    return rebased


def ocr_result_text_and_blocks(result: Any) -> tuple[str, list[dict[str, Any]]]:
    """Accept legacy OCR strings and preserve structured OCR blocks when given."""
    if isinstance(result, str):
        return result.strip(), []
    if not isinstance(result, list):
        return "", []

    texts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        value = item.get("text")
        if not isinstance(value, str) or not value.strip():
            continue
        block: dict[str, Any] = {"text": value.strip()}
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence):
            if 0 <= float(confidence) <= 1:
                block["confidence"] = float(confidence)
        bbox = item.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                numbers = [float(value) for value in bbox]
            except (TypeError, ValueError):
                numbers = []
            if numbers and all(math.isfinite(value) for value in numbers) and numbers[2] >= numbers[0] and numbers[3] >= numbers[1]:
                block["bbox"] = numbers
        if isinstance(item.get("parser"), str) and item["parser"].strip():
            block["parser"] = item["parser"].strip()
        texts.append(block["text"])
        blocks.append(block)
    return "\n".join(texts), blocks
