"""Bounded, non-secret projection of an EvidenceLocatorV1 for vector points."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.schemas.evidence_locator import EvidenceLocatorV1, SourceLocatorV1, sha256_text


LOCATOR_PROJECTION_VERSION = "v1"
MAX_LOCATOR_PROJECTION_BYTES = 4096
MAX_PROJECTED_RANGES = 32
MAX_PROJECTED_HEADINGS = 16
MAX_PROJECTED_HEADING_CHARS = 200
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _string_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


def _safe_source(locator: EvidenceLocatorV1) -> dict[str, Any]:
    source = locator.source.model_dump(mode="json", exclude_none=True)
    projected: dict[str, Any] = {"kind": source["kind"]}
    if "file_name" in source:
        projected["file_name"] = source["file_name"]
    for key in ("page", "table", "sheet", "row", "column", "cell", "json_pointer", "bbox"):
        if key in source:
            projected[key] = source[key]
    if "heading_path" in source:
        projected["heading_path"] = [
            item[:MAX_PROJECTED_HEADING_CHARS]
            for item in source["heading_path"][:MAX_PROJECTED_HEADINGS]
        ]
    text = source.get("text")
    if isinstance(text, Mapping):
        projected["text"] = {
            key: text[key]
            for key in ("start", "end")
            if key in text
        }
        ranges = text.get("ranges")
        if isinstance(ranges, list):
            projected["text"]["ranges"] = [
                {
                    key: item[key]
                    for key in ("start", "end", "sha256")
                    if key in item
                }
                for item in ranges[:MAX_PROJECTED_RANGES]
                if isinstance(item, Mapping)
            ]
    return projected


def _base_projection(
    locator: EvidenceLocatorV1,
    *,
    chunk_id: Any = None,
    block_id: Any = None,
    evidence_id: Any = None,
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "locator_version": LOCATOR_PROJECTION_VERSION,
        "document_id": str(locator.document_id),
        "document_revision_id": str(locator.document_revision_id),
        "revision_no": locator.revision_no,
        "unit_kind": locator.unit_kind,
        "source_kind": locator.source.kind,
        "source": _safe_source(locator),
    }
    for key, value in (
        ("document_revision_file_id", locator.document_revision_file_id),
        ("chunk_id", chunk_id),
        ("block_id", block_id),
        ("evidence_id", evidence_id),
    ):
        normalized = _string_id(value)
        if normalized is not None:
            projection[key] = normalized
    return projection


def _bounded_projection(projection: dict[str, Any]) -> dict[str, Any]:
    if len(_canonical_json(projection).encode("utf-8")) <= MAX_LOCATOR_PROJECTION_BYTES:
        return projection

    source = projection.get("source")
    if isinstance(source, dict):
        # Keep identity, source kind and exact normalized offsets before dropping
        # optional display coordinates and parser-derived detail.
        compact_source = {"kind": source.get("kind")}
        text = source.get("text")
        if isinstance(text, Mapping):
            compact_source["text"] = {
                key: text[key]
                for key in ("start", "end")
                if key in text
            }
        projection = {**projection, "source": compact_source}
    return projection


def project_locator(
    locator: EvidenceLocatorV1,
    *,
    chunk_id: Any = None,
    block_id: Any = None,
    evidence_id: Any = None,
) -> dict[str, Any]:
    """Return a deterministic bounded projection without content or storage data."""
    locator_hash = sha256_text(
        _canonical_json(locator.model_dump(mode="json", exclude_none=True))
    )
    projection = _bounded_projection(
        _base_projection(
            locator,
            chunk_id=chunk_id,
            block_id=block_id,
            evidence_id=evidence_id,
        )
    )
    projection["projection_hash"] = sha256_text(_canonical_json(projection))
    projection["locator_hash"] = locator_hash
    if len(_canonical_json(projection).encode("utf-8")) > MAX_LOCATOR_PROJECTION_BYTES:
        raise ValueError("locator projection exceeds its bounded size")
    return projection


def parse_locator(
    raw: Any,
    *,
    document_id: Any = None,
    document_revision_id: Any = None,
    revision_no: int | None = None,
    unit_id: Any = None,
    parent_unit_id: Any = None,
) -> EvidenceLocatorV1 | None:
    """Validate a locator and its row identity; malformed data is a read miss."""
    if not isinstance(raw, Mapping):
        return None
    try:
        locator = EvidenceLocatorV1.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None
    expected = (
        ("document_id", document_id),
        ("document_revision_id", document_revision_id),
        ("unit_id", unit_id),
        ("parent_unit_id", parent_unit_id),
    )
    for field, value in expected:
        if value is not None and getattr(locator, field) != value:
            try:
                if str(getattr(locator, field)) != str(value):
                    return None
            except Exception:  # noqa: BLE001
                return None
    if revision_no is not None and locator.revision_no != revision_no:
        return None
    return locator


def chunk_locator_projection(
    chunk: Any,
    *,
    document_id: Any,
    document_revision_id: Any = None,
    revision_no: int | None = None,
) -> dict[str, Any] | None:
    """Project only a valid M2 locator attached to a chunk."""
    metadata = getattr(chunk, "chunk_metadata", None)
    raw = metadata.get("evidence_locator_v1") if isinstance(metadata, Mapping) else None
    locator = parse_locator(
        raw,
        document_id=document_id,
        document_revision_id=document_revision_id,
        revision_no=revision_no,
        unit_id=getattr(chunk, "id", None),
        parent_unit_id=getattr(chunk, "block_id", None),
    )
    if locator is None:
        return None
    return project_locator(
        locator,
        chunk_id=getattr(chunk, "id", None),
        block_id=getattr(chunk, "block_id", None),
        evidence_id=getattr(chunk, "evidence_id", None),
    )


def safe_locator_projection(
    raw: Any,
    *,
    document_id: Any,
    document_revision_id: Any,
    revision_no: int,
    unit_id: Any,
    parent_unit_id: Any = None,
    evidence_id: Any = None,
) -> dict[str, Any] | None:
    locator = parse_locator(
        raw,
        document_id=document_id,
        document_revision_id=document_revision_id,
        revision_no=revision_no,
        unit_id=unit_id,
        parent_unit_id=parent_unit_id,
    )
    if locator is None:
        return None
    return project_locator(locator, evidence_id=evidence_id, block_id=parent_unit_id)


_PROJECTION_ID_KEYS = {
    "document_id",
    "document_revision_id",
    "document_revision_file_id",
    "chunk_id",
    "block_id",
    "evidence_id",
}
_PROJECTION_KINDS = {
    "section",
    "chunk",
    "structured_unit",
    "table",
    "row",
    "cell",
    "image_region",
}
_PROJECTION_SOURCE_KINDS = {
    "text",
    "pdf",
    "docx",
    "xlsx",
    "xls",
    "csv",
    "json",
    "image",
}


def _canonical_uuid(value: Any) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        normalized = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return str(normalized) if value == str(normalized) else None


def _projection_identity_matches(
    projection: Mapping[str, Any], expected_identity: Mapping[str, Any]
) -> bool:
    for key in ("document_id", "document_revision_id", "chunk_id"):
        expected = expected_identity.get(key)
        if expected is None:
            continue
        normalized_expected = _canonical_uuid(expected)
        if normalized_expected is None or projection.get(key) != normalized_expected:
            return False

    for key in ("document_revision", "document_revision_no", "revision_no"):
        if key not in expected_identity or expected_identity[key] is None:
            continue
        expected = expected_identity[key]
        if isinstance(expected, bool) or type(expected) is not int or expected < 1:
            return False
        if projection.get("revision_no") != expected:
            return False
    return True


def validate_projection(
    raw: Any,
    *,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate a projection received from a vector payload before reading it."""
    if not isinstance(raw, Mapping):
        return None
    try:
        projection = dict(raw)
        supplied_locator_hash = projection.pop("locator_hash")
        supplied_projection_hash = projection.pop("projection_hash")
        if projection.get("locator_version") != LOCATOR_PROJECTION_VERSION:
            return None
        required_keys = {
            "locator_version",
            "document_id",
            "document_revision_id",
            "revision_no",
            "unit_kind",
            "source_kind",
            "source",
        }
        if not required_keys.issubset(projection):
            return None
        allowed_keys = {
            "locator_version", "document_id", "document_revision_id",
            "document_revision_file_id", "revision_no", "chunk_id", "block_id",
            "evidence_id", "unit_kind", "source_kind", "source",
        }
        if set(projection) - allowed_keys:
            return None
        if not isinstance(supplied_locator_hash, str) or not _SHA256.fullmatch(supplied_locator_hash):
            return None
        if not isinstance(supplied_projection_hash, str) or not _SHA256.fullmatch(supplied_projection_hash):
            return None
        for key in _PROJECTION_ID_KEYS:
            if key in projection and _canonical_uuid(projection[key]) != projection[key]:
                return None
        if type(projection["revision_no"]) is not int or projection["revision_no"] < 1:
            return None
        if not isinstance(projection["unit_kind"], str) or projection["unit_kind"] not in _PROJECTION_KINDS:
            return None
        if not isinstance(projection["source_kind"], str) or projection["source_kind"] not in _PROJECTION_SOURCE_KINDS:
            return None
        if not isinstance(projection.get("source"), Mapping):
            return None
        source = SourceLocatorV1.model_validate(projection["source"], strict=True)
        if source.kind != projection["source_kind"]:
            return None
        allowed_sources = {
            "row": {"xlsx", "xls", "csv", "docx"},
            "cell": {"xlsx", "xls", "csv", "docx"},
            "table": {"xlsx", "xls", "csv", "docx"},
            "image_region": {"pdf", "docx", "image"},
        }.get(projection["unit_kind"])
        if allowed_sources is not None and source.kind not in allowed_sources:
            return None
        unsigned_projection = dict(projection)
        if sha256_text(_canonical_json(unsigned_projection)) != supplied_projection_hash:
            return None
        full_projection = {
            **unsigned_projection,
            "locator_hash": supplied_locator_hash,
            "projection_hash": supplied_projection_hash,
        }
        if len(_canonical_json(full_projection).encode("utf-8")) > MAX_LOCATOR_PROJECTION_BYTES:
            return None
        if expected_identity is not None and not _projection_identity_matches(
            unsigned_projection, expected_identity
        ):
            return None
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
    return full_projection


def bound_record_projection(metadata: Any) -> dict[str, Any] | None:
    """Bind a display locator to the record's content revision, not index generation."""
    if not isinstance(metadata, Mapping) or not all(metadata.get(key) for key in
            ("document_id", "document_revision_id", "chunk_id")):
        return None
    return validate_projection(metadata.get("evidence_locator_v1_projection"), expected_identity={
        key: metadata.get(key) for key in ("document_id", "document_revision_id", "chunk_id", "document_revision_no")
    })


def projection_location_label(projection: Mapping[str, Any]) -> str:
    """Format already validated physical page or sheet/row positions."""
    source = projection["source"]
    if projection["source_kind"] == "pdf" and source.get("page"):
        page = source["page"]
        span = str(page["start"]) if page["start"] == page["end"] else f'{page["start"]}–{page["end"]}'
        return f"PDF物理第 {span} 页"
    if source.get("sheet") and source.get("row"):
        row = source["row"]
        span = str(row["start"]) if row["start"] == row["end"] else f'{row["start"]}–{row["end"]}'
        return f'{source["sheet"]["name"]}，第 {span} 行'
    return ""
