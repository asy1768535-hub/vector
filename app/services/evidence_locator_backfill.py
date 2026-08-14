"""Bounded, additive EvidenceLocatorV1 backfill for immutable revisions.

This module deliberately does not reuse the legacy v0.2 migration backfill.  It
only enriches existing ready revision rows and never creates, deletes, or
rewrites historical entities, scalar locations, or revision identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.evidence_unit import EvidenceUnit
from app.schemas.evidence_locator import (
    EvidenceLocatorV1,
    normalize_evidence_locator_payload,
    sha256_text,
)


BACKFILL_TASK = "phase1-m4-evidence-locator-v1"
BACKFILL_MARKER_KEY = "evidence_locator_v1_backfill"
BACKFILL_MARKER_TOOL = BACKFILL_TASK
MAX_BATCH_SIZE = 100
MAX_REVISIONS = 1000
UNIT_PAGE_SIZE = 500
MAX_UNITS_PER_REVISION = 10000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LocatorBackfillError(ValueError):
    """Stable, non-sensitive row failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RevisionCursor:
    created_at: datetime
    revision_id: uuid.UUID


@dataclass(frozen=True)
class BackfillResult:
    scanned: int
    skipped: int
    updated: int
    failed: int
    reason_counts: dict[str, int]
    next_cursor: str | None
    exhausted: bool
    run_id: str


@dataclass(frozen=True)
class RollbackResult:
    scanned: int
    candidates: int
    removed: int
    refused: int
    reason_counts: dict[str, int]
    next_cursor: str | None
    exhausted: bool


@dataclass(frozen=True)
class _RowUpdate:
    obj: Any
    field: str
    metadata: dict[str, Any]
    original: Any


@dataclass(frozen=True)
class _RevisionRows:
    blocks: tuple[DocumentBlock, ...]
    chunks: tuple[Chunk, ...]
    evidence: tuple[EvidenceUnit, ...]
    chunk_blocks: tuple[ChunkBlock, ...]
    chunk_evidence: tuple[ChunkEvidence, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _locator_hash(locator: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(locator).encode("utf-8")).hexdigest()


def encode_cursor(cursor: RevisionCursor | None) -> str | None:
    if cursor is None:
        return None
    payload = {
        "created_at": cursor.created_at.astimezone(timezone.utc).isoformat(),
        "revision_id": str(cursor.revision_id),
    }
    raw = _canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> RevisionCursor | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        revision_id = uuid.UUID(payload["revision_id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if str(revision_id) != payload["revision_id"]:
        raise ValueError("cursor is invalid")
    return RevisionCursor(created_at.astimezone(timezone.utc), revision_id)


def _validate_limits(batch_size: int, max_revisions: int | None) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("batch_size must be an integer")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size exceeds the bounded limit")
    if max_revisions is not None:
        if isinstance(max_revisions, bool) or not isinstance(max_revisions, int):
            raise ValueError("max_revisions must be an integer")
        if not 1 <= max_revisions <= MAX_REVISIONS:
            raise ValueError("max_revisions exceeds the bounded limit")


def _validate_run_id(run_id: str | None) -> str:
    if run_id is None:
        return str(uuid.uuid4())
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
        raise ValueError("run_id is invalid")
    if any(ord(char) < 32 for char in run_id):
        raise ValueError("run_id is invalid")
    return run_id


def _revision_created_at(revision: DocumentRevision) -> datetime:
    value = revision.created_at
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _file_suffix(file_name: str | None) -> str:
    return Path(file_name or "").suffix.lower()


def _source_kind(file_name: str | None, position: Mapping[str, Any] | None) -> str:
    suffix = _file_suffix(file_name)
    by_suffix = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".xlsx": "xlsx",
        ".xls": "xls",
        ".csv": "csv",
        ".json": "json",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".tif": "image",
        ".tiff": "image",
    }
    if suffix in by_suffix:
        return by_suffix[suffix]
    kind = position.get("type") if isinstance(position, Mapping) else None
    if kind == "page":
        return "pdf"
    if kind in {"sheet", "sheet_row"}:
        return "xlsx"
    if kind == "csv_row":
        return "csv"
    if kind == "json":
        return "json"
    return "text"


def _valid_int(value: Any, *, minimum: int) -> int | None:
    if isinstance(value, bool) or type(value) is not int or value < minimum:
        return None
    return value


def _valid_text_range(
    normalized_text: str | None,
    text: str | None,
    start: Any,
    end: Any,
) -> dict[str, Any] | None:
    if not isinstance(normalized_text, str) or not isinstance(text, str):
        return None
    start_value = _valid_int(start, minimum=0)
    end_value = _valid_int(end, minimum=0)
    if (
        start_value is None
        or end_value is None
        or end_value < start_value
        or end_value > len(normalized_text)
        or normalized_text[start_value:end_value] != text
    ):
        return None
    return {
        "start": start_value,
        "end": end_value,
        "ranges": [{
            "start": start_value,
            "end": end_value,
            "sha256": sha256_text(text),
        }],
    }


def _source_payload(
    *,
    source_kind: str,
    file_name: str | None,
    position: Mapping[str, Any] | None,
    page_start: Any,
    page_end: Any,
    title_path: Any,
    normalized_text: str | None,
    text: str | None,
    source_start: Any,
    source_end: Any,
) -> dict[str, Any]:
    source: dict[str, Any] = {"kind": source_kind}
    if isinstance(file_name, str) and file_name:
        source["file_name"] = file_name
    location = position if isinstance(position, Mapping) else {}
    location_type = location.get("type")
    page_start_value = _valid_int(page_start, minimum=1)
    page_end_value = _valid_int(page_end, minimum=1)
    if page_start_value is None and location_type == "page":
        page = _valid_int(location.get("page"), minimum=1)
        page_start_value = page_end_value = page
    if (
        page_start_value is not None
        and page_end_value is not None
        and page_end_value >= page_start_value
    ):
        source["page"] = {"start": page_start_value, "end": page_end_value}

    if isinstance(title_path, list) and all(isinstance(item, str) for item in title_path):
        source["heading_path"] = title_path

    if location_type in {"sheet", "sheet_row"} and source_kind in {"xlsx", "xls"}:
        sheet = location.get("sheet")
        if isinstance(sheet, str) and sheet:
            source["sheet"] = {"name": sheet}
    if location_type == "csv_row" and source_kind == "csv":
        row = _valid_int(location.get("row"), minimum=1)
        if row is not None:
            source["row"] = {"start": row, "end": row}
    if location_type == "table" and source_kind in {"docx", "xlsx", "xls", "csv"}:
        table_index = _valid_int(location.get("table_index"), minimum=1)
        if table_index is not None:
            source["table"] = {"index": table_index - 1}
    if source_kind == "json":
        pointer = location.get("json_pointer")
        if isinstance(pointer, str) and (pointer == "" or pointer.startswith("/")):
            source["json_pointer"] = pointer

    text_span = _valid_text_range(normalized_text, text, source_start, source_end)
    if text_span is not None:
        source["text"] = text_span
    return source


def _parser_payload(revision: DocumentRevision) -> tuple[dict[str, Any], bool]:
    name = revision.parser_name if isinstance(revision.parser_name, str) else ""
    version = revision.parser_version if isinstance(revision.parser_version, str) else ""
    available = bool(name.strip() and version.strip())
    payload: dict[str, Any] = {
        "name": name.strip() if available else "legacy-backfill",
        "version": version.strip() if available else "unknown",
    }
    config = revision.parser_config
    if isinstance(config, Mapping):
        try:
            payload["config_hash"] = sha256_text(_canonical_json(config))
        except (TypeError, ValueError):
            available = False
    return payload, available


def _revision_file_state(
    revision: DocumentRevision,
    revision_file: DocumentRevisionFile | None,
) -> tuple[bool, str | None, str | None, str | None]:
    if revision_file is None:
        return False, None, None, None
    if (
        revision_file.document_id != revision.document_id
        or revision_file.document_revision_id != revision.id
        or revision_file.library_id != revision.library_id
        or not isinstance(revision_file.sha256, str)
        or _SHA256.fullmatch(revision_file.sha256) is None
    ):
        raise LocatorBackfillError("revision_file_scope_mismatch")
    return True, revision_file.id, revision_file.sha256, revision_file.file_name


def _unit_kind(value: str | None, *, fallback: str) -> str:
    allowed = {"section", "chunk", "structured_unit", "table", "row", "cell", "image_region"}
    return value if value in allowed else fallback


def _build_locator(
    *,
    revision: DocumentRevision,
    revision_file: DocumentRevisionFile | None,
    unit_id: uuid.UUID,
    parent_unit_id: uuid.UUID | None,
    ordinal: int,
    unit_kind: str,
    source_kind: str,
    file_name: str | None,
    position: Mapping[str, Any] | None,
    page_start: Any,
    page_end: Any,
    title_path: Any,
    source_start: Any,
    source_end: Any,
    text: str | None,
    evidence_quote: str | None,
) -> dict[str, Any]:
    parser, parser_available = _parser_payload(revision)
    file_available, file_id, raw_hash, file_name = _revision_file_state(revision, revision_file)
    source = _source_payload(
        source_kind=source_kind,
        file_name=file_name,
        position=position,
        page_start=page_start,
        page_end=page_end,
        title_path=title_path,
        normalized_text=revision.normalized_text,
        text=text,
        source_start=source_start,
        source_end=source_end,
    )
    exact_range = "text" in source
    unit_hash = sha256_text(text) if isinstance(text, str) else None
    quote_hash = sha256_text(evidence_quote) if isinstance(evidence_quote, str) else None
    normalized_hash_valid = bool(
        isinstance(revision.normalized_text, str)
        and isinstance(revision.content_hash, str)
        and _SHA256.fullmatch(revision.content_hash)
        and sha256_text(revision.normalized_text) == revision.content_hash
    )
    normalized_hash = revision.content_hash if normalized_hash_valid else None
    verified = bool(
        file_available
        and parser_available
        and exact_range
        and unit_hash
        and quote_hash
        and normalized_hash_valid
    )
    payload: dict[str, Any] = {
        "document_id": revision.document_id,
        "document_revision_id": revision.id,
        "revision_no": revision.revision_no,
        "document_revision_file_id": file_id if file_available else None,
        "raw_file_sha256": raw_hash if file_available else None,
        "normalized_content_hash": normalized_hash,
        "unit_id": unit_id,
        "parent_unit_id": parent_unit_id,
        "unit_kind": unit_kind,
        "ordinal": ordinal,
        "parser": parser,
        "source": source,
        "quality": {"extraction_mode": "not_applicable"},
        "unit_text_sha256": unit_hash,
        "quote_sha256": quote_hash,
        "provenance_status": "verified" if verified else "legacy_unverified",
    }
    try:
        return normalize_evidence_locator_payload(EvidenceLocatorV1.model_validate(payload))
    except (TypeError, ValueError) as exc:
        raise LocatorBackfillError("locator_contract_invalid") from exc


def _metadata_update(
    existing: Any,
    *,
    locator: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    if existing is None:
        metadata: dict[str, Any] = {}
    elif isinstance(existing, Mapping):
        metadata = dict(existing)
    else:
        raise LocatorBackfillError("metadata_not_object")
    existing_marker = metadata.get(BACKFILL_MARKER_KEY)
    if existing_marker is not None and (
        not isinstance(existing_marker, Mapping)
        or existing_marker.get("tool") != BACKFILL_MARKER_TOOL
    ):
        raise LocatorBackfillError("metadata_reserved_key_conflict")
    metadata["evidence_locator_v1"] = locator
    metadata[BACKFILL_MARKER_KEY] = {
        "tool": BACKFILL_MARKER_TOOL,
        "run_id": run_id,
        "locator_hash": _locator_hash(locator),
    }
    return metadata


def _expected_identity(
    *, revision: DocumentRevision, unit_id: uuid.UUID, parent_unit_id: uuid.UUID | None
) -> dict[str, Any]:
    return {
        "document_id": revision.document_id,
        "document_revision_id": revision.id,
        "revision_no": revision.revision_no,
        "unit_id": unit_id,
        "parent_unit_id": parent_unit_id,
    }


def _existing_locator_state(
    metadata: Any,
    *,
    revision: DocumentRevision,
    revision_file: DocumentRevisionFile | None,
    unit_id: uuid.UUID,
    parent_unit_id: uuid.UUID | None,
    text: str | None,
    evidence_quote: str | None,
) -> str:
    if not isinstance(metadata, Mapping) or "evidence_locator_v1" not in metadata:
        return "missing"
    raw = metadata.get("evidence_locator_v1")
    try:
        locator = EvidenceLocatorV1.model_validate(raw)
    except (TypeError, ValueError):
        raise LocatorBackfillError("existing_locator_invalid") from None
    expected = _expected_identity(
        revision=revision, unit_id=unit_id, parent_unit_id=parent_unit_id
    )
    for key, value in expected.items():
        if getattr(locator, key) != value:
            raise LocatorBackfillError("existing_locator_identity_conflict")
    expected_texts = tuple(
        value for value in (text, evidence_quote) if isinstance(value, str)
    )
    if expected_texts:
        normalized_text = revision.normalized_text
        text_span = locator.source.text
        if not isinstance(normalized_text, str) or text_span is None:
            raise LocatorBackfillError("existing_locator_source_range_conflict")
        if (
            text_span.start < 0
            or text_span.end < text_span.start
            or text_span.end > len(normalized_text)
        ):
            raise LocatorBackfillError("existing_locator_source_range_conflict")
        ranges = text_span.ranges or [text_span]
        reconstructed_parts: list[str] = []
        for item in ranges:
            if item.start < 0 or item.end < item.start or item.end > len(normalized_text):
                raise LocatorBackfillError("existing_locator_source_range_conflict")
            item_text = normalized_text[item.start:item.end]
            item_hash = getattr(item, "sha256", None)
            if item_hash is not None and item_hash != sha256_text(item_text):
                raise LocatorBackfillError("existing_locator_source_hash_conflict")
            reconstructed_parts.append(item_text)
        reconstructed = "".join(reconstructed_parts)
        if any(reconstructed != expected for expected in expected_texts):
            raise LocatorBackfillError("existing_locator_source_range_conflict")
    normalized_hash = revision.content_hash
    if locator.normalized_content_hash != normalized_hash:
        raise LocatorBackfillError("existing_locator_content_hash_conflict")
    if locator.document_revision_file_id is not None:
        if revision_file is None or (
            locator.document_revision_file_id != revision_file.id
            or locator.raw_file_sha256 != revision_file.sha256
        ):
            raise LocatorBackfillError("existing_locator_file_hash_conflict")
    if isinstance(text, str) and locator.unit_text_sha256 != sha256_text(text):
        raise LocatorBackfillError("existing_locator_unit_hash_conflict")
    if evidence_quote is None:
        if locator.provenance_status == "verified":
            raise LocatorBackfillError("existing_locator_quote_unverifiable")
    elif locator.quote_sha256 != sha256_text(evidence_quote):
        raise LocatorBackfillError("existing_locator_quote_hash_conflict")
    return "valid"


def _parent_block(
    block_id: uuid.UUID | None,
    *,
    blocks: Mapping[uuid.UUID, DocumentBlock],
    revision: DocumentRevision,
    child_seq: int | None,
) -> uuid.UUID | None:
    if block_id is None:
        return None
    parent = blocks.get(block_id)
    if parent is None:
        raise LocatorBackfillError("parent_missing")
    if parent.document_revision_id != revision.id or parent.document_id != revision.document_id:
        raise LocatorBackfillError("parent_revision_mismatch")
    if child_seq is not None and parent.seq >= child_seq:
        raise LocatorBackfillError("parent_order_invalid")
    return parent.id


def _chunk_parent_ids(
    chunks: tuple[Chunk, ...],
    links: tuple[ChunkBlock, ...],
) -> dict[uuid.UUID, uuid.UUID | None]:
    linked: dict[uuid.UUID, uuid.UUID] = {}
    for link in links:
        if link.chunk_id in linked and linked[link.chunk_id] != link.document_block_id:
            raise LocatorBackfillError("chunk_parent_conflict")
        linked[link.chunk_id] = link.document_block_id
    output: dict[uuid.UUID, uuid.UUID | None] = {}
    for chunk in chunks:
        linked_id = linked.get(chunk.id)
        if chunk.block_id is not None and linked_id is not None and chunk.block_id != linked_id:
            raise LocatorBackfillError("chunk_parent_conflict")
        output[chunk.id] = chunk.block_id or linked_id
    return output


async def _page_rows(
    db: AsyncSession,
    model: Any,
    revision_id: uuid.UUID,
    *,
    order_by_seq: bool,
) -> tuple[Any, ...]:
    rows: list[Any] = []
    offset = 0
    while True:
        if model is ChunkBlock:
            order = (model.seq, model.chunk_id, model.document_block_id)
        elif model is ChunkEvidence:
            order = (model.seq, model.chunk_id, model.evidence_id)
        else:
            order = (model.seq, model.id) if order_by_seq else (model.id,)
        result = await db.execute(
            select(model)
            .where(model.document_revision_id == revision_id)
            .order_by(*order)
            .offset(offset)
            .limit(UNIT_PAGE_SIZE)
        )
        page = list(result.scalars().all())
        if len(rows) + len(page) > MAX_UNITS_PER_REVISION:
            raise LocatorBackfillError("revision_unit_limit")
        rows.extend(page)
        if len(page) < UNIT_PAGE_SIZE:
            return tuple(rows)
        offset += UNIT_PAGE_SIZE


async def _revision_rows(db: AsyncSession, revision_id: uuid.UUID) -> _RevisionRows:
    return _RevisionRows(
        blocks=await _page_rows(db, DocumentBlock, revision_id, order_by_seq=True),
        chunks=await _page_rows(db, Chunk, revision_id, order_by_seq=True),
        evidence=await _page_rows(db, EvidenceUnit, revision_id, order_by_seq=False),
        chunk_blocks=await _page_rows(db, ChunkBlock, revision_id, order_by_seq=True),
        chunk_evidence=await _page_rows(db, ChunkEvidence, revision_id, order_by_seq=True),
    )


def _file_name(revision_file: DocumentRevisionFile | None) -> str | None:
    return revision_file.file_name if revision_file is not None else None


def _validate_row_scope(obj: Any, revision: DocumentRevision) -> None:
    if (
        getattr(obj, "document_revision_id", revision.id) != revision.id
        or getattr(obj, "document_id", revision.document_id) != revision.document_id
        or getattr(obj, "library_id", revision.library_id) != revision.library_id
    ):
        raise LocatorBackfillError("row_scope_mismatch")


def _row_plan(
    *,
    obj: Any,
    field: str,
    revision: DocumentRevision,
    revision_file: DocumentRevisionFile | None,
    blocks: Mapping[uuid.UUID, DocumentBlock],
    parent_unit_id: uuid.UUID | None,
    ordinal: int,
    unit_kind: str,
    text: str | None,
    evidence_quote: str | None,
    position: Mapping[str, Any] | None,
    page_start: Any,
    page_end: Any,
    title_path: Any,
    source_start: Any,
    source_end: Any,
    run_id: str,
) -> tuple[str, _RowUpdate | None]:
    if parent_unit_id is not None:
        _parent_block(
            parent_unit_id,
            blocks=blocks,
            revision=revision,
            child_seq=None,
        )
    state = _existing_locator_state(
        getattr(obj, field),
        revision=revision, revision_file=revision_file,
        unit_id=obj.id,
        parent_unit_id=parent_unit_id,
        text=text, evidence_quote=evidence_quote,
    )
    if state == "valid":
        return "skipped", None
    source_kind = _source_kind(_file_name(revision_file), position)
    locator = _build_locator(
        revision=revision,
        revision_file=revision_file,
        unit_id=obj.id,
        parent_unit_id=parent_unit_id,
        ordinal=ordinal,
        unit_kind=_unit_kind(unit_kind, fallback="structured_unit"),
        source_kind=source_kind,
        file_name=_file_name(revision_file),
        position=position,
        page_start=page_start,
        page_end=page_end,
        title_path=title_path,
        source_start=source_start,
        source_end=source_end,
        text=text,
        evidence_quote=evidence_quote,
    )
    original = getattr(obj, field)
    return "updated", _RowUpdate(
        obj, field, _metadata_update(original, locator=locator, run_id=run_id), original
    )


async def _process_revision(
    db: AsyncSession,
    *,
    revision: DocumentRevision,
    run_id: str,
    apply: bool,
) -> tuple[int, int, int, int, dict[str, int]]:
    rows = _RevisionRows((), (), (), (), ())
    scanned = 0
    skipped = updated = failed = 0
    reasons: dict[str, int] = {}
    updates: list[_RowUpdate] = []
    try:
        rows = await _revision_rows(db, revision.id)
        scanned = len(rows.blocks) + len(rows.chunks) + len(rows.evidence)
        revision_file_result = await db.execute(
            select(DocumentRevisionFile)
            .where(DocumentRevisionFile.document_revision_id == revision.id)
        )
        revision_file = revision_file_result.scalars().first()
        _revision_file_state(revision, revision_file)
        blocks = {row.id: row for row in rows.blocks}
        for row in (*rows.blocks, *rows.chunks, *rows.evidence):
            _validate_row_scope(row, revision)
        chunk_parents = _chunk_parent_ids(rows.chunks, rows.chunk_blocks)
        for ordinal, block in enumerate(rows.blocks):
            parent_id = _parent_block(
                block.parent_block_id,
                blocks=blocks,
                revision=revision,
                child_seq=block.seq,
            )
            state, update = _row_plan(
                obj=block, field="content", revision=revision, revision_file=revision_file,
                blocks=blocks, parent_unit_id=parent_id, ordinal=ordinal,
                unit_kind=block.block_kind, text=block.text, evidence_quote=block.text,
                position=block.position, page_start=block.page_start, page_end=block.page_end,
                title_path=block.title_path, source_start=block.source_start,
                source_end=block.source_end, run_id=run_id,
            )
            if state == "skipped":
                skipped += 1
            else:
                updates.append(update)
        block_offset = len(rows.blocks)
        for index, chunk in enumerate(rows.chunks):
            parent_id = chunk_parents.get(chunk.id)
            if parent_id is None:
                raise LocatorBackfillError("parent_missing")
            state, update = _row_plan(
                obj=chunk, field="chunk_metadata", revision=revision, revision_file=revision_file,
                blocks=blocks, parent_unit_id=parent_id, ordinal=block_offset + index,
                unit_kind="chunk", text=chunk.text, evidence_quote=chunk.text,
                position=chunk.position, page_start=chunk.page_start, page_end=chunk.page_end,
                title_path=chunk.title_path, source_start=chunk.source_start,
                source_end=chunk.source_end, run_id=run_id,
            )
            if state == "skipped":
                skipped += 1
            else:
                updates.append(update)
        evidence_offset = block_offset + len(rows.chunks)
        for index, evidence in enumerate(rows.evidence):
            parent_id = evidence.document_block_id
            if parent_id is None:
                raise LocatorBackfillError("parent_missing")
            state, update = _row_plan(
                obj=evidence, field="evidence_metadata", revision=revision,
                revision_file=revision_file, blocks=blocks, parent_unit_id=parent_id,
                ordinal=evidence_offset + index, unit_kind="chunk", text=evidence.text_quote,
                evidence_quote=evidence.text_quote, position=evidence.position,
                page_start=evidence.page_start, page_end=evidence.page_end,
                title_path=evidence.title_path, source_start=evidence.source_start,
                source_end=evidence.source_end, run_id=run_id,
            )
            if state == "skipped":
                skipped += 1
            else:
                updates.append(update)
        if apply and updates:
            for update in updates:
                setattr(update.obj, update.field, update.metadata)
            await db.flush()
        updated = len(updates)
    except LocatorBackfillError as exc:
        if apply:
            for update in updates:
                setattr(update.obj, update.field, update.original)
        failed = 1
        reasons[exc.code] = 1
        updated = 0
    except Exception:
        if apply:
            for update in updates:
                setattr(update.obj, update.field, update.original)
        failed = 1
        reasons["revision_processing_failed"] = 1
        updated = 0
    return scanned, skipped, updated, failed, reasons


async def _revision_file_for(db: AsyncSession, revision_id: uuid.UUID) -> DocumentRevisionFile | None:
    result = await db.execute(
        select(DocumentRevisionFile).where(DocumentRevisionFile.document_revision_id == revision_id)
    )
    return result.scalars().first()


def _rollback_metadata(
    metadata: Any,
    *,
    run_id: str,
    unit_id: uuid.UUID,
    revision: DocumentRevision,
    apply: bool,
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(metadata, Mapping):
        return "metadata_not_object", None
    marker = metadata.get(BACKFILL_MARKER_KEY)
    raw = metadata.get("evidence_locator_v1")
    if not isinstance(marker, Mapping) or marker.get("tool") != BACKFILL_MARKER_TOOL:
        return "marker_missing", None
    if marker.get("run_id") != run_id:
        return "marker_run_mismatch", None
    try:
        locator = EvidenceLocatorV1.model_validate(raw)
    except (TypeError, ValueError):
        return "locator_invalid", None
    if (
        locator.unit_id != unit_id
        or locator.document_id != revision.document_id
        or locator.document_revision_id != revision.id
        or locator.revision_no != revision.revision_no
        or marker.get("locator_hash") != _locator_hash(normalize_evidence_locator_payload(locator))
    ):
        return "identity_or_hash_mismatch", None
    output = dict(metadata)
    output.pop(BACKFILL_MARKER_KEY, None)
    output.pop("evidence_locator_v1", None)
    return "remove", output if apply else metadata


async def _rollback_revision(
    db: AsyncSession,
    *,
    revision: DocumentRevision,
    run_id: str,
    apply: bool,
) -> tuple[int, int, int, dict[str, int]]:
    updates: list[tuple[Any, str, Any]] = []
    try:
        rows = await _revision_rows(db, revision.id)
        scanned = candidates = removed = 0
        reasons: dict[str, int] = {}
        for obj, field in [
            *((row, "content") for row in rows.blocks),
            *((row, "chunk_metadata") for row in rows.chunks),
            *((row, "evidence_metadata") for row in rows.evidence),
        ]:
            scanned += 1
            code, metadata = _rollback_metadata(
                getattr(obj, field),
                run_id=run_id,
                unit_id=obj.id,
                revision=revision,
                apply=apply,
            )
            if code == "remove":
                candidates += 1
                if apply:
                    updates.append((obj, field, getattr(obj, field)))
                    setattr(obj, field, metadata)
                    removed += 1
            elif code != "marker_missing":
                reasons[code] = reasons.get(code, 0) + 1
        if apply and removed:
            await db.flush()
        return scanned, candidates, removed, reasons
    except Exception:
        if apply:
            for obj, field, original in updates:
                setattr(obj, field, original)
        raise


async def _ready_revisions(
    db: AsyncSession,
    *,
    library_id: uuid.UUID | None,
    revision_id: uuid.UUID | None,
    cursor: RevisionCursor | None,
    limit: int,
    statuses: Collection[str] = ("ready",),
) -> tuple[tuple[DocumentRevision, ...], bool]:
    if not statuses:
        raise ValueError("statuses must not be empty")
    stmt = select(DocumentRevision).where(DocumentRevision.status.in_(tuple(statuses)))
    if library_id is not None:
        stmt = stmt.where(DocumentRevision.library_id == library_id)
    if revision_id is not None:
        stmt = stmt.where(DocumentRevision.id == revision_id)
    if cursor is not None:
        stmt = stmt.where(
            or_(
                DocumentRevision.created_at > cursor.created_at,
                and_(
                    DocumentRevision.created_at == cursor.created_at,
                    DocumentRevision.id > cursor.revision_id,
                ),
            )
        )
    stmt = stmt.order_by(DocumentRevision.created_at, DocumentRevision.id).limit(limit)
    result = await db.execute(stmt)
    rows = tuple(result.scalars().all())
    return rows, len(rows) < limit


async def backfill_evidence_locators(
    db: AsyncSession,
    *,
    library_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    batch_size: int = 50,
    max_revisions: int | None = None,
    cursor: str | None = None,
    apply: bool = False,
    run_id: str | None = None,
) -> BackfillResult:
    """Backfill one bounded revision page; writes are opt-in and savepointed."""
    _validate_limits(batch_size, max_revisions)
    decoded = decode_cursor(cursor)
    limit = min(batch_size, max_revisions or batch_size)
    revisions, exhausted = await _ready_revisions(
        db,
        library_id=library_id,
        revision_id=revision_id,
        cursor=decoded,
        limit=limit,
        statuses=("ready",),
    )
    effective_run_id = _validate_run_id(run_id)
    scanned = skipped = updated = failed = 0
    reasons: dict[str, int] = {}
    for revision in revisions:
        savepoint = db.begin_nested()
        async with savepoint:
            values = await _process_revision(
                db, revision=revision, run_id=effective_run_id, apply=apply
            )
            if values[3]:
                rollback = getattr(savepoint, "rollback", None)
                if rollback is not None and getattr(savepoint, "is_active", True):
                    await rollback()
            scanned += values[0]
            skipped += values[1]
            updated += values[2]
            failed += values[3]
            for code, count in values[4].items():
                reasons[code] = reasons.get(code, 0) + count
        if apply:
            await db.commit()
    next_cursor = encode_cursor(
        RevisionCursor(_revision_created_at(revisions[-1]), revisions[-1].id)
        if revisions else decoded
    )
    return BackfillResult(
        scanned=scanned, skipped=skipped, updated=updated, failed=failed,
        reason_counts=reasons, next_cursor=next_cursor, exhausted=exhausted,
        run_id=effective_run_id,
    )


async def rollback_evidence_locators(
    db: AsyncSession,
    *,
    run_id: str,
    library_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    batch_size: int = 50,
    max_revisions: int | None = None,
    cursor: str | None = None,
    apply: bool = False,
) -> RollbackResult:
    """Plan or apply removal only for matching tool markers and locator hashes."""
    _validate_limits(batch_size, max_revisions)
    _validate_run_id(run_id)
    decoded = decode_cursor(cursor)
    limit = min(batch_size, max_revisions or batch_size)
    revisions, exhausted = await _ready_revisions(
        db,
        library_id=library_id,
        revision_id=revision_id,
        cursor=decoded,
        limit=limit,
        statuses=("ready", "superseded"),
    )
    scanned = candidates = removed = refused = 0
    reasons: dict[str, int] = {}
    for revision in revisions:
        try:
            async with db.begin_nested():
                values = await _rollback_revision(
                    db, revision=revision, run_id=run_id, apply=apply
                )
                scanned += values[0]
                candidates += values[1]
                removed += values[2]
                refused += sum(values[3].values())
                for code, count in values[3].items():
                    reasons[code] = reasons.get(code, 0) + count
        except LocatorBackfillError as exc:
            refused += 1
            reasons[exc.code] = reasons.get(exc.code, 0) + 1
        except Exception:
            refused += 1
            reasons["rollback_revision_failed"] = reasons.get("rollback_revision_failed", 0) + 1
        if apply:
            await db.commit()
    next_cursor = encode_cursor(
        RevisionCursor(_revision_created_at(revisions[-1]), revisions[-1].id)
        if revisions else decoded
    )
    return RollbackResult(
        scanned=scanned, candidates=candidates, removed=removed, refused=refused,
        reason_counts=reasons, next_cursor=next_cursor, exhausted=exhausted,
    )
