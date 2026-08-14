from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.schemas.evidence_locator import (
    EvidenceLocatorV1,
    ProvenanceStatus,
    normalize_evidence_locator_payload,
    sha256_text,
)
from app.services.parser_units import validate_parser_unit_hierarchy


PARSER_NAME = "legacy"
PARSER_VERSION = "v0.2-m2"
CHUNKING_STRATEGY_VERSION = "v0.2-m2"
MAX_STRUCTURED_CONTENT_BYTES = 8192
MAX_STRUCTURED_FIELD_BYTES = 2048


@dataclass(frozen=True)
class PreparedChunk:
    text: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceGenerationResult:
    revision: DocumentRevision
    job: EmbeddingJob
    chunks: list[Chunk]
    blocks: list[DocumentBlock]
    evidence_units: list[EvidenceUnit]


def validate_parser_segments(segments: list[dict] | None) -> None:
    """Validate parser output before any document or evidence mutation."""
    if not segments:
        return
    units: list[Mapping[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("parser segment must be an object")
        parser_unit = segment.get("parser_unit")
        if not isinstance(parser_unit, Mapping):
            raise ValueError("parser segment is missing parser_unit")
        units.append(parser_unit)
        structured_units = segment.get("structured_units") or []
        if not isinstance(structured_units, list):
            raise ValueError("parser structured_units must be a list")
        for unit in structured_units:
            if not isinstance(unit, Mapping):
                raise ValueError("parser structured_units must contain only objects")
            units.append(unit)
    validate_parser_unit_hierarchy(units)


def _unit_text(unit: Mapping[str, Any], fallback: str = "") -> str:
    value = unit.get("text")
    if isinstance(value, str):
        return value
    value = unit.get("value")
    if value is None:
        return fallback
    return str(value)


def _parser_payload(unit: Mapping[str, Any]) -> dict[str, Any]:
    parser = unit.get("parser")
    if not isinstance(parser, Mapping):
        raise ValueError("parser unit is missing parser provenance")
    payload = {
        "name": parser.get("name"),
        "version": parser.get("version"),
    }
    if parser.get("config_hash") is not None:
        payload["config_hash"] = parser["config_hash"]
    return payload


def _source_payload(
    unit: Mapping[str, Any], *, file_name: str | None = None
) -> dict[str, Any]:
    source = unit.get("source") or {}
    if not isinstance(source, Mapping):
        raise ValueError("parser unit source must be an object")
    allowed = {
        "page", "text", "heading_path", "table", "sheet", "row", "column",
        "cell", "json_pointer", "bbox",
    }
    payload = {key: source[key] for key in allowed if key in source}
    if file_name:
        payload["file_name"] = file_name
    return payload


def _quality_payload(unit: Mapping[str, Any]) -> dict[str, Any]:
    quality = unit.get("quality") or {}
    if not isinstance(quality, Mapping):
        raise ValueError("parser unit quality must be an object")
    return {
        key: quality[key]
        for key in ("extraction_mode", "ocr_engine", "ocr_engine_version", "ocr_confidence")
        if key in quality
    }


def _locator(
    *,
    document: Document,
    revision: DocumentRevision,
    unit_id: uuid.UUID,
    parent_unit_id: uuid.UUID | None,
    unit_kind: str,
    ordinal: int,
    parser_unit: Mapping[str, Any],
    unit_text: str,
    file_name: str | None,
    raw_file_sha256: str | None,
    document_revision_file_id: uuid.UUID | None,
    source: Mapping[str, Any] | None = None,
    provenance_status: ProvenanceStatus | None = None,
) -> dict[str, Any]:
    if (raw_file_sha256 is None) != (document_revision_file_id is None):
        raise ValueError("raw file hash and revision file ID must be provided together")
    payload = {
        "document_id": document.id,
        "document_revision_id": revision.id,
        "revision_no": revision.revision_no,
        "document_revision_file_id": document_revision_file_id,
        "raw_file_sha256": raw_file_sha256,
        "normalized_content_hash": revision.content_hash,
        "unit_id": unit_id,
        "parent_unit_id": parent_unit_id,
        "unit_kind": unit_kind,
        "ordinal": ordinal,
        "parser": _parser_payload(parser_unit),
        "source": {
            "kind": parser_unit.get("source_kind"),
            **(_source_payload(parser_unit, file_name=file_name) if source is None else dict(source)),
        },
        "quality": _quality_payload(parser_unit),
        "unit_text_sha256": sha256_text(unit_text),
        "quote_sha256": sha256_text(unit_text),
        "provenance_status": provenance_status or (
            "verified" if document_revision_file_id is not None else "legacy_unverified"
        ),
    }
    normalized = normalize_evidence_locator_payload(EvidenceLocatorV1.model_validate(payload))
    if normalized["source"].get("heading_path") == []:
        normalized["source"].pop("heading_path")
    return normalized


def _merge_locator_metadata(
    metadata: dict[str, Any] | None, locator: dict[str, Any]
) -> dict[str, Any]:
    output = dict(metadata or {})
    if "evidence_locator_v1" in output:
        raise ValueError("evidence_locator_v1 is reserved for the system locator")
    output["evidence_locator_v1"] = locator
    return output


def _bounded_json_value(value: Any, *, limit: int = MAX_STRUCTURED_FIELD_BYTES) -> Any:
    """Keep parser-owned structural data JSON-safe and bounded."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        json_value = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        json_value = str(value)
        encoded = json.dumps(json_value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= limit:
        return json_value
    return {
        "truncated": True,
        "sha256": sha256_text(encoded),
        "type": type(value).__name__,
    }


def _structural_payload(
    unit: Mapping[str, Any], context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "unit_key": str(unit["unit_key"]),
        "unit_kind": str(unit["unit_kind"]),
        "source_kind": str(unit["source_kind"]),
    }
    for key in ("value", "formula", "json_value", "json_value_kind", "text"):
        if key in unit and unit[key] is not None:
            payload[key] = _bounded_json_value(unit[key])
    for key in ("kind", "caption", "header", "heading"):
        if context and key in context and context[key] is not None:
            payload[f"segment_{key}"] = _bounded_json_value(context[key])
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) <= MAX_STRUCTURED_CONTENT_BYTES:
        return payload
    # Individual fields are bounded, but a segment header can still make the
    # combined envelope large. Preserve identity and a digest rather than data.
    return {
        "unit_key": payload["unit_key"],
        "unit_kind": payload["unit_kind"],
        "source_kind": payload["source_kind"],
        "payload_truncated": True,
        "payload_sha256": sha256_text(encoded),
    }


def _short_span_hash(value: str) -> str:
    return sha256_text(value)[:16]


def _validated_chunk_ranges(
    *, normalized_text: str, chunk_text: str, metadata: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    start = metadata.get("source_start")
    end = metadata.get("source_end")
    if (start is None) != (end is None):
        raise ValueError("chunk source range must provide both start and end")
    if start is not None and (
        isinstance(start, bool) or isinstance(end, bool)
        or not isinstance(start, int) or not isinstance(end, int)
        or start < 0 or end < start or end > len(normalized_text)
    ):
        raise ValueError("chunk source range is outside normalized revision text")

    raw_ranges = metadata.get("source_ranges")
    ranges = raw_ranges if isinstance(raw_ranges, list) and raw_ranges else None
    if ranges is None:
        if start is None:
            return None
        source_slice = normalized_text[start:end]
        if source_slice != chunk_text:
            raise ValueError("chunk text does not match its normalized source range")
        ranges = [{"start": start, "end": end}]
    else:
        previous_end = 0
        normalized_ranges: list[dict[str, Any]] = []
        for item in ranges:
            if not isinstance(item, Mapping):
                raise ValueError("chunk source ranges must contain objects")
            item_start, item_end = item.get("start"), item.get("end")
            if (
                isinstance(item_start, bool) or isinstance(item_end, bool)
                or not isinstance(item_start, int) or not isinstance(item_end, int)
                or item_start < 0 or item_end < item_start or item_end > len(normalized_text)
                or item_start < previous_end
            ):
                raise ValueError("chunk source ranges are invalid or overlapping")
            source_slice = normalized_text[item_start:item_end]
            supplied_hash = item.get("sha256") or item.get("hash")
            if supplied_hash is not None and str(supplied_hash) not in {
                sha256_text(source_slice), _short_span_hash(source_slice),
            }:
                raise ValueError("chunk source range hash does not match normalized text")
            normalized_ranges.append({
                "start": item_start,
                "end": item_end,
                "sha256": sha256_text(source_slice),
            })
            previous_end = item_end
        reconstructed_text = "".join(
            normalized_text[item["start"]:item["end"]]
            for item in normalized_ranges
        )
        if reconstructed_text != chunk_text:
            raise ValueError("chunk text does not match its normalized source ranges")
        ranges = normalized_ranges

    if start is None:
        start = min(item["start"] for item in ranges)
        end = max(item["end"] for item in ranges)
    supplied_span_hash = metadata.get("source_span_hash")
    if supplied_span_hash is not None and str(supplied_span_hash) not in {
        sha256_text(normalized_text[start:end]), _short_span_hash(normalized_text[start:end]),
    }:
        raise ValueError("chunk source span hash does not match normalized text")
    return ranges


def _source_span_for_chunk(
    *, normalized_text: str, chunk_text: str, metadata: Mapping[str, Any], root_source: Mapping[str, Any]
) -> dict[str, Any]:
    source = dict(root_source)
    ranges = _validated_chunk_ranges(
        normalized_text=normalized_text, chunk_text=chunk_text, metadata=metadata
    )
    if ranges is None:
        return source
    start = min(item["start"] for item in ranges)
    end = max(item["end"] for item in ranges)
    source["text"] = {
        "start": start,
        "end": end,
        "ranges": ranges,
    }
    return source


def validate_prepared_chunk_ranges(
    *, normalized_text: str, prepared_chunks: list[PreparedChunk]
) -> None:
    """Validate all chunk quotes/ranges before the owning Document is persisted."""
    for prepared in prepared_chunks:
        _validated_chunk_ranges(
            normalized_text=normalized_text,
            chunk_text=prepared.text,
            metadata=prepared.metadata or {},
        )


def _root_matches_location(root: Mapping[str, Any], location: Mapping[str, Any]) -> bool:
    source = root.get("source") or {}
    if not isinstance(source, Mapping):
        return False
    kind = location.get("type")
    if kind == "page":
        page = source.get("page") or {}
        return root.get("source_kind") == "pdf" and page.get("start") == page.get("end") == location.get("page")
    if kind in {"sheet", "sheet_row"}:
        sheet = source.get("sheet") or {}
        return sheet.get("name") == location.get("sheet")
    if kind == "table":
        table = source.get("table") or {}
        table_index = location.get("table_index")
        return isinstance(table_index, int) and table.get("index") == table_index - 1
    if kind == "csv_row":
        row = source.get("row") or {}
        row_number = location.get("row")
        return isinstance(row_number, int) and row.get("start") == row.get("end") == row_number
    if kind in {"paragraph", "line"}:
        heading = location.get("heading")
        headings = source.get("heading_path") or []
        return bool(heading) and isinstance(headings, list) and headings == [part.strip() for part in str(heading).split("/") if part.strip()]
    return False


def _chunk_root_key(
    *, roots: list[Mapping[str, Any]], root_spans: Mapping[str, tuple[int, int]],
    prepared: PreparedChunk,
) -> str | None:
    metadata = prepared.metadata or {}
    location = metadata.get("location")
    if isinstance(location, Mapping):
        identity_matches = [
            str(root["unit_key"])
            for root in roots
            if _root_matches_location(root, location)
        ]
        if len(identity_matches) == 1:
            return identity_matches[0]
        if len(identity_matches) > 1:
            return None
    start, end = metadata.get("source_start"), metadata.get("source_end")
    if not isinstance(start, int) or not isinstance(end, int):
        return str(roots[0]["unit_key"]) if len(roots) == 1 else None
    candidates = [
        key
        for key, span in root_spans.items()
        if span[0] <= start and end <= span[1]
    ]
    return candidates[0] if len(candidates) == 1 else None


def _segment_units(
    segments: list[dict],
) -> tuple[
    list[tuple[Mapping[str, Any], str]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    candidates: list[tuple[str, Mapping[str, Any], str]] = []
    by_key: dict[str, Mapping[str, Any]] = {}
    context_by_key: dict[str, Mapping[str, Any]] = {}
    text_by_key: dict[str, str] = {}
    for segment in segments:
        root = segment["parser_unit"]
        root_key = str(root["unit_key"])
        context = {
            key: segment[key]
            for key in ("kind", "caption", "header", "heading")
            if key in segment
        }
        by_key[root_key] = root
        context_by_key[root_key] = context
        text_by_key[root_key] = str(segment.get("text") or "")
        candidates.append((root_key, root, text_by_key[root_key]))
        for unit in segment.get("structured_units") or []:
            key = str(unit["unit_key"])
            by_key[key] = unit
            context_by_key[key] = context
            text_by_key[key] = _unit_text(unit)
            candidates.append((key, unit, text_by_key[key]))
    ordered_keys: list[str] = []
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        parent = by_key[key].get("parent_key")
        if parent is not None:
            parent_key = str(parent)
            if parent_key not in by_key:
                raise ValueError(f"parser parent key is not available: {parent_key}")
            visit(parent_key)
        visited.add(key)
        ordered_keys.append(key)

    for key, _unit, _text in candidates:
        visit(key)
    ordered = [(by_key[key], text_by_key[key]) for key in ordered_keys]
    return ordered, by_key, context_by_key


async def _create_segmented_generation(
    db: AsyncSession,
    *,
    library: Library,
    document: Document,
    normalized_text: str,
    title: str | None,
    document_metadata: dict[str, Any] | None,
    splitter: str,
    created_by: uuid.UUID | None,
    prepared_chunks: list[PreparedChunk],
    segments: list[dict],
    file_name: str | None,
    raw_file_sha256: str | None,
    document_revision_file_id: uuid.UUID | None,
    job: EmbeddingJob | None,
    rebuild_operation_id: uuid.UUID | None,
) -> EvidenceGenerationResult:
    validate_parser_segments(segments)
    validate_prepared_chunk_ranges(
        normalized_text=normalized_text, prepared_chunks=prepared_chunks
    )
    document_id = _ensure_id(document)
    ordered_units, units_by_key, contexts_by_key = _segment_units(segments)
    if not ordered_units:
        raise ValueError("parser segments produced no units")
    roots = [segment["parser_unit"] for segment in segments]
    parser_payloads = [_parser_payload(root) for root in roots]
    revision_id = uuid.uuid4()
    revision_no = int(document.current_revision or 1)
    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=revision_no,
        title=title,
        document_metadata=document_metadata,
        content_hash=document.content_hash,
        normalized_text=normalized_text,
        parser_name=parser_payloads[0]["name"] if len({p["name"] for p in parser_payloads}) == 1 else "multi-parser",
        parser_version=parser_payloads[0]["version"],
        parser_config={"source_parsers": parser_payloads},
        chunking_strategy=splitter,
        chunking_strategy_version=CHUNKING_STRATEGY_VERSION,
        chunking_config={"chunk_size": library.chunk_size, "chunk_overlap": library.chunk_overlap},
        visibility_scope=document.visibility_scope,
        security_level=document.security_level,
        status="pending",
        created_by=created_by,
    )
    document.latest_revision_id = revision.id

    if job is None:
        job = EmbeddingJob(
            library_id=library.id,
            document_id=document_id,
            status="pending",
            document_revision=revision_no,
            document_revision_id=revision.id,
            document_revision_no=revision_no,
            rebuild_operation_id=rebuild_operation_id,
        )
    else:
        job.document_revision_id = revision.id
        job.document_revision_no = revision_no

    root_spans: dict[str, tuple[int, int]] = {}
    for root, _text in ((segment["parser_unit"], segment.get("text") or "") for segment in segments):
        text_span = (root.get("source") or {}).get("text")
        if isinstance(text_span, Mapping):
            start, end = text_span.get("start"), text_span.get("end")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(normalized_text):
                root_spans[str(root["unit_key"])] = (start, end)

    chunk_root_keys = [
        _chunk_root_key(
            roots=roots,
            root_spans=root_spans,
            prepared=prepared,
        )
        for prepared in prepared_chunks
    ]
    need_revision_root = any(key is None for key in chunk_root_keys)
    blocks: list[DocumentBlock] = []
    evidence_units: list[EvidenceUnit] = []
    chunks: list[Chunk] = []
    chunk_blocks: list[ChunkBlock] = []
    chunk_evidence: list[ChunkEvidence] = []
    block_ids: dict[str, uuid.UUID] = {}
    block_locators: dict[str, dict[str, Any]] = {}
    next_ordinal = 0

    if need_revision_root:
        revision_root = {
            "source_kind": roots[0].get("source_kind"),
            "unit_kind": "section",
            "parser": roots[0].get("parser"),
            "source": {"text": {"start": 0, "end": len(normalized_text), "ranges": [{
                "start": 0, "end": len(normalized_text), "sha256": sha256_text(normalized_text)
            }]}},
        }
        root_id = uuid.uuid4()
        root_locator = _locator(
            document=document, revision=revision, unit_id=root_id, parent_unit_id=None,
            unit_kind="section", ordinal=next_ordinal, parser_unit=revision_root,
            unit_text=normalized_text, file_name=file_name,
            raw_file_sha256=raw_file_sha256,
            document_revision_file_id=document_revision_file_id,
        )
        root_block = DocumentBlock(
            id=root_id, library_id=library.id, document_id=document.id,
            document_revision_id=revision.id, seq=next_ordinal, block_kind="section",
            source_start=0, source_end=len(normalized_text), text=normalized_text,
            content={
                "evidence_locator_v1": root_locator,
                "parser_unit": {
                    "unit_kind": "section",
                    "source_kind": roots[0].get("source_kind"),
                    "segment_kind": "revision_root",
                },
            },
            position={"type": "revision_root"}, parser_name=revision_root["parser"]["name"],
            parser_version=revision_root["parser"]["version"],
        )
        blocks.append(root_block)
        block_ids["revision:root"] = root_id
        block_locators["revision:root"] = root_locator
        next_ordinal += 1

    for unit, fallback_text in ordered_units:
        key = str(unit["unit_key"])
        unit_id = uuid.uuid4()
        parent_key = unit.get("parent_key")
        parent_id = block_ids.get(str(parent_key)) if parent_key is not None else None
        if parent_key is not None and parent_id is None:
            raise ValueError(f"parser parent key is not available: {parent_key}")
        unit_text = _unit_text(unit, fallback_text)
        locator = _locator(
            document=document, revision=revision, unit_id=unit_id,
            parent_unit_id=parent_id, unit_kind=str(unit["unit_kind"]),
            ordinal=next_ordinal, parser_unit=unit, unit_text=unit_text,
            file_name=file_name, raw_file_sha256=raw_file_sha256,
            document_revision_file_id=document_revision_file_id,
        )
        source = locator["source"]
        block = DocumentBlock(
            id=unit_id, library_id=library.id, document_id=document.id,
            document_revision_id=revision.id, parent_block_id=parent_id,
            seq=next_ordinal, block_kind=str(unit["unit_kind"]),
            title_path=source.get("heading_path"), page_start=(source.get("page") or {}).get("start"),
            page_end=(source.get("page") or {}).get("end"),
            source_start=(source.get("text") or {}).get("start"),
            source_end=(source.get("text") or {}).get("end"), text=unit_text,
            content={
                "evidence_locator_v1": locator,
                "parser_unit": _structural_payload(unit, contexts_by_key.get(key)),
            }, position=dict(source),
            parser_name=unit["parser"]["name"], parser_version=unit["parser"]["version"],
        )
        blocks.append(block)
        block_ids[key] = unit_id
        block_locators[key] = locator
        next_ordinal += 1

    for seq, (prepared, root_key) in enumerate(zip(prepared_chunks, chunk_root_keys, strict=True)):
        metadata = prepared.metadata or {}
        parent_id = block_ids.get(root_key or "revision:root")
        if parent_id is None:
            raise ValueError("chunk has no containing parser section")
        root_unit = units_by_key.get(root_key)
        if root_unit is None:
            root_source = {"kind": roots[0].get("source_kind")}
            if file_name:
                root_source["file_name"] = file_name
            parser_unit = {
                "source_kind": roots[0].get("source_kind"),
                "unit_kind": "section",
                "parser": roots[0].get("parser"),
                "source": root_source,
            }
        else:
            root_source = _source_payload(root_unit, file_name=file_name)
            parser_unit = dict(root_unit)
        chunk_source = _source_span_for_chunk(
            normalized_text=normalized_text,
            chunk_text=prepared.text,
            metadata=metadata,
            root_source=root_source,
        )
        chunk_unit = parser_unit
        chunk_unit["source"] = chunk_source
        chunk_unit["source_kind"] = parser_unit.get("source_kind")
        chunk_id = uuid.uuid4()
        chunk_text = prepared.text
        chunk_locator = _locator(
            document=document, revision=revision, unit_id=chunk_id,
            parent_unit_id=parent_id, unit_kind="chunk", ordinal=next_ordinal,
            parser_unit=chunk_unit, unit_text=chunk_text, file_name=file_name,
            raw_file_sha256=raw_file_sha256,
            document_revision_file_id=document_revision_file_id, source=chunk_source,
        )
        chunk_metadata = _merge_locator_metadata(metadata, chunk_locator)
        source = chunk_locator["source"]
        legacy_position = _position(metadata) or dict(source)
        title_path = metadata.get("title_path") if isinstance(metadata.get("title_path"), list) else source.get("heading_path")
        page_start = _int_or_none(metadata.get("page_start"))
        page_end = _int_or_none(metadata.get("page_end"))
        if page_start is None:
            page_start = _int_or_none((source.get("page") or {}).get("start"))
        if page_end is None:
            page_end = _int_or_none((source.get("page") or {}).get("end"))
        source_start = _int_or_none((source.get("text") or {}).get("start"))
        source_end = _int_or_none((source.get("text") or {}).get("end"))
        evidence_id = uuid.uuid4()
        evidence_payload = dict(chunk_locator)
        evidence_payload["unit_id"] = evidence_id
        evidence_payload["quote_sha256"] = sha256_text(chunk_text)
        evidence_payload = normalize_evidence_locator_payload(EvidenceLocatorV1.model_validate(evidence_payload))
        evidence = EvidenceUnit(
            id=evidence_id, library_id=library.id, document_id=document.id,
            document_revision_id=revision.id, document_block_id=parent_id,
            evidence_kind="chunk", source_start=source_start, source_end=source_end,
            page_start=page_start, page_end=page_end,
            title_path=title_path, position=legacy_position, text_quote=chunk_text,
            text_quote_hash=sha256_text(chunk_text), evidence_metadata={"evidence_locator_v1": evidence_payload},
            visibility_scope=document.visibility_scope, security_level=document.security_level, status="active",
        )
        chunk = Chunk(
            id=chunk_id, document_id=document.id, library_id=library.id,
            document_revision_id=revision.id, block_id=parent_id, evidence_id=evidence_id,
            seq=seq, chunk_kind="text", text=chunk_text, token_count=len(chunk_text),
            page_start=page_start, page_end=page_end,
            title_path=title_path, source_start=source_start,
            source_end=source_end, position=legacy_position, chunk_metadata=chunk_metadata,
        )
        chunks.append(chunk)
        evidence_units.append(evidence)
        chunk_blocks.append(ChunkBlock(
            chunk_id=chunk_id, document_block_id=parent_id, document_revision_id=revision.id,
            seq=seq, source_start=chunk.source_start, source_end=chunk.source_end,
        ))
        chunk_evidence.append(ChunkEvidence(
            chunk_id=chunk_id, evidence_id=evidence_id, document_revision_id=revision.id, seq=seq,
        ))
        next_ordinal += 1

    try:
        db.add(revision)
        db.add(job)
        db.add_all([*blocks, *evidence_units, *chunks, *chunk_blocks, *chunk_evidence])
        await db.flush()
    except Exception as exc:
        revision.status = "failed"
        revision.last_error = str(exc)
        if document.current_revision_id is None:
            document.status = "failed"
        raise ValueError(str(exc)) from exc
    return EvidenceGenerationResult(revision=revision, job=job, chunks=chunks, blocks=blocks, evidence_units=evidence_units)


def _ensure_id(obj: Any) -> uuid.UUID:
    current = getattr(obj, "id", None)
    if current is None:
        current = uuid.uuid4()
        setattr(obj, "id", current)
    return current


def _quote_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _position(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    location = metadata.get("location")
    if isinstance(location, dict):
        return dict(location)
    return None


async def create_evidence_generation(
    db: AsyncSession,
    *,
    library: Library,
    document: Document,
    normalized_text: str,
    title: str | None,
    document_metadata: dict[str, Any] | None,
    splitter: str,
    created_by: uuid.UUID | None,
    prepared_chunks: list[PreparedChunk],
    job: EmbeddingJob | None = None,
    rebuild_operation_id: uuid.UUID | None = None,
    segments: list[dict] | None = None,
    file_name: str | None = None,
    raw_file_sha256: str | None = None,
    document_revision_file_id: uuid.UUID | None = None,
) -> EvidenceGenerationResult:
    if segments:
        return await _create_segmented_generation(
            db,
            library=library,
            document=document,
            normalized_text=normalized_text,
            title=title,
            document_metadata=document_metadata,
            splitter=splitter,
            created_by=created_by,
            prepared_chunks=prepared_chunks,
            segments=segments,
            file_name=file_name,
            raw_file_sha256=raw_file_sha256,
            document_revision_file_id=document_revision_file_id,
            job=job,
            rebuild_operation_id=rebuild_operation_id,
        )
    document_id = _ensure_id(document)
    revision_id = uuid.uuid4()
    revision_no = int(document.current_revision or 1)

    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=revision_no,
        title=title,
        document_metadata=document_metadata,
        content_hash=document.content_hash,
        normalized_text=normalized_text,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        parser_config=None,
        chunking_strategy=splitter,
        chunking_strategy_version=CHUNKING_STRATEGY_VERSION,
        chunking_config={
            "chunk_size": library.chunk_size,
            "chunk_overlap": library.chunk_overlap,
        },
        visibility_scope=document.visibility_scope,
        security_level=document.security_level,
        status="pending",
        created_by=created_by,
    )
    document.latest_revision_id = revision.id

    job_created = job is None
    if job is None:
        job = EmbeddingJob(
            library_id=library.id,
            document_id=document_id,
            status="pending",
            document_revision=revision_no,
            document_revision_id=revision.id,
            document_revision_no=revision_no,
            rebuild_operation_id=rebuild_operation_id,
        )
    else:
        job.document_revision_id = revision.id
        job.document_revision_no = revision_no

    blocks: list[DocumentBlock] = []
    evidence_units: list[EvidenceUnit] = []
    chunks: list[Chunk] = []
    chunk_blocks: list[ChunkBlock] = []
    chunk_evidence: list[ChunkEvidence] = []

    validate_prepared_chunk_ranges(
        normalized_text=normalized_text, prepared_chunks=prepared_chunks
    )

    for seq, prepared in enumerate(prepared_chunks):
        metadata = prepared.metadata or {}
        source_start = _int_or_none(metadata.get("source_start"))
        source_end = _int_or_none(metadata.get("source_end"))
        page_start = _int_or_none(metadata.get("page_start"))
        page_end = _int_or_none(metadata.get("page_end"))
        title_path = metadata.get("title_path")
        if not isinstance(title_path, list):
            title_path = None
        position = _position(metadata)
        source = _source_span_for_chunk(
            normalized_text=normalized_text,
            chunk_text=prepared.text,
            metadata=metadata,
            root_source={"kind": "text", **({"file_name": file_name} if file_name else {})},
        )
        parser_unit = {
            "source_kind": "text",
            "unit_kind": "section",
            "parser": {"name": PARSER_NAME, "version": PARSER_VERSION},
        }
        block_id = uuid.uuid4()
        evidence_id = uuid.uuid4()
        chunk_id = uuid.uuid4()

        block = DocumentBlock(
            id=block_id,
            library_id=library.id,
            document_id=document_id,
            document_revision_id=revision.id,
            seq=seq,
            block_kind="paragraph",
            title_path=title_path,
            page_start=page_start,
            page_end=page_end,
            source_start=source_start,
            source_end=source_end,
            text=prepared.text,
            content={
                "evidence_locator_v1": _locator(
                    document=document,
                    revision=revision,
                    unit_id=block_id,
                    parent_unit_id=None,
                    unit_kind="section",
                    ordinal=seq,
                    parser_unit=parser_unit,
                    unit_text=prepared.text,
                    file_name=file_name,
                    raw_file_sha256=raw_file_sha256,
                    document_revision_file_id=document_revision_file_id,
                    source=source,
                    provenance_status="verified",
                ),
            },
            position=position,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
        )
        evidence = EvidenceUnit(
            id=evidence_id,
            library_id=library.id,
            document_id=document_id,
            document_revision_id=revision.id,
            document_block_id=block.id,
            evidence_kind="chunk",
            source_start=source_start,
            source_end=source_end,
            page_start=page_start,
            page_end=page_end,
            title_path=title_path,
            position=position,
            text_quote=prepared.text,
            text_quote_hash=_quote_hash(prepared.text),
            evidence_metadata={
                "evidence_locator_v1": _locator(
                    document=document,
                    revision=revision,
                    unit_id=evidence_id,
                    parent_unit_id=block.id,
                    unit_kind="chunk",
                    ordinal=seq,
                    parser_unit=parser_unit,
                    unit_text=prepared.text,
                    file_name=file_name,
                    raw_file_sha256=raw_file_sha256,
                    document_revision_file_id=document_revision_file_id,
                    source=source,
                    provenance_status="verified",
                ),
            },
            visibility_scope=document.visibility_scope,
            security_level=document.security_level,
            status="active",
        )
        chunk = Chunk(
            id=chunk_id,
            document_id=document_id,
            library_id=library.id,
            document_revision_id=revision.id,
            block_id=block.id,
            evidence_id=evidence.id,
            seq=seq,
            chunk_kind="text",
            text=prepared.text,
            token_count=len(prepared.text),
            page_start=page_start,
            page_end=page_end,
            title_path=title_path,
            source_start=source_start,
            source_end=source_end,
            position=position,
            chunk_metadata=_merge_locator_metadata(
                prepared.metadata, _locator(
                    document=document,
                    revision=revision,
                    unit_id=chunk_id,
                    parent_unit_id=block.id,
                    unit_kind="chunk",
                    ordinal=seq,
                    parser_unit=parser_unit,
                    unit_text=prepared.text,
                    file_name=file_name,
                    raw_file_sha256=raw_file_sha256,
                    document_revision_file_id=document_revision_file_id,
                    source=source,
                    provenance_status="verified",
                )
            ),
        )
        blocks.append(block)
        evidence_units.append(evidence)
        chunks.append(chunk)
        chunk_blocks.append(
            ChunkBlock(
                chunk_id=chunk.id,
                document_block_id=block.id,
                document_revision_id=revision.id,
                seq=seq,
                source_start=source_start,
                source_end=source_end,
            )
        )
        chunk_evidence.append(
            ChunkEvidence(
                chunk_id=chunk.id,
                evidence_id=evidence.id,
                document_revision_id=revision.id,
                seq=seq,
            )
        )

    try:
        db.add(revision)
        if job_created:
            db.add(job)
        db.add_all([*blocks, *evidence_units, *chunks, *chunk_blocks, *chunk_evidence])
        await db.flush()
    except Exception as exc:
        revision.status = "failed"
        revision.last_error = str(exc)
        if document.current_revision_id is None:
            document.status = "failed"
        raise ValueError(str(exc)) from exc
    return EvidenceGenerationResult(
        revision=revision,
        job=job,
        chunks=chunks,
        blocks=blocks,
        evidence_units=evidence_units,
    )
