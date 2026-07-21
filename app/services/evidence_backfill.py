from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import (
    REVISION_STATUS_DELETED,
    REVISION_STATUS_FAILED,
    REVISION_STATUS_PENDING,
    REVISION_STATUS_PROCESSING,
    REVISION_STATUS_READY,
    REVISION_STATUS_SUPERSEDED,
    DocumentRevision,
)
from app.models.evidence_unit import EvidenceUnit
from app.models.migration_backfill_state import MigrationBackfillState


LEGACY_NAMESPACE = uuid.UUID("6b89d2e4-25d7-4bd6-9f67-5601a03b7c4d")
LEGACY_PARSER_NAME = "legacy"
LEGACY_PARSER_VERSION = "v0.2-m1"
LEGACY_CHUNKING_STRATEGY = "legacy"
LEGACY_CHUNKING_STRATEGY_VERSION = "v0.2-m1"

_ALLOWED_REVISION_STATUSES = {
    REVISION_STATUS_PENDING,
    REVISION_STATUS_PROCESSING,
    REVISION_STATUS_READY,
    REVISION_STATUS_FAILED,
    REVISION_STATUS_SUPERSEDED,
    REVISION_STATUS_DELETED,
}


@dataclass(frozen=True)
class LegacyRevisionBackfillRows:
    revision: DocumentRevision
    document_updates: dict[str, Any]


@dataclass(frozen=True)
class LegacyChunkBackfillRows:
    block: DocumentBlock
    evidence: EvidenceUnit
    chunk_block: ChunkBlock
    chunk_evidence: ChunkEvidence
    chunk_updates: dict[str, Any]


@dataclass(frozen=True)
class BackfillBatchResult:
    processed: int
    failed: int
    exhausted: bool


def _stable_uuid(kind: str, *parts: object) -> uuid.UUID:
    raw = ":".join([kind, *(str(p) for p in parts)])
    return uuid.uuid5(LEGACY_NAMESPACE, raw)


def _quote_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _legacy_revision_status(document: Document) -> str:
    if document.deleted_at is not None:
        return REVISION_STATUS_DELETED
    if document.status == REVISION_STATUS_READY:
        return REVISION_STATUS_READY
    if document.status in _ALLOWED_REVISION_STATUSES:
        return document.status
    return REVISION_STATUS_FAILED


def _published_at(document: Document) -> datetime | None:
    if _legacy_revision_status(document) != REVISION_STATUS_READY:
        return None
    value = document.updated_at or document.created_at
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _snapshot_timestamp(document: Document) -> datetime:
    value = document.updated_at or document.created_at
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def build_legacy_revision_backfill_rows(document: Document) -> LegacyRevisionBackfillRows:
    revision_no = document.current_revision or 1
    revision_id = _stable_uuid("legacy-revision", document.id, revision_no)
    status = _legacy_revision_status(document)

    revision = DocumentRevision(
        id=revision_id,
        library_id=document.library_id,
        document_id=document.id,
        revision_no=revision_no,
        title=document.title,
        document_metadata=document.doc_metadata,
        content_hash=document.content_hash,
        parser_name=LEGACY_PARSER_NAME,
        parser_version=LEGACY_PARSER_VERSION,
        chunking_strategy=LEGACY_CHUNKING_STRATEGY,
        chunking_strategy_version=LEGACY_CHUNKING_STRATEGY_VERSION,
        visibility_scope=document.visibility_scope,
        security_level=document.security_level,
        status=status,
        published_at=_published_at(document),
        created_by=document.created_by,
        created_at=_snapshot_timestamp(document),
        updated_at=_snapshot_timestamp(document),
        finished_at=_published_at(document),
        last_error=document.last_error,
    )

    document_updates: dict[str, Any] = {}
    if document.deleted_at is None:
        document_updates["latest_revision_id"] = revision_id
        if status == REVISION_STATUS_READY:
            document_updates["current_revision_id"] = revision_id

    return LegacyRevisionBackfillRows(revision=revision, document_updates=document_updates)


def build_legacy_chunk_backfill_rows(
    document: Document,
    revision: DocumentRevision,
    chunk: Chunk,
) -> LegacyChunkBackfillRows:
    block_id = _stable_uuid("legacy-block", revision.id, chunk.id)
    evidence_id = _stable_uuid("legacy-evidence", revision.id, chunk.id)
    position = {"type": "legacy_chunk", "chunk_seq": chunk.seq}

    block = DocumentBlock(
        id=block_id,
        library_id=document.library_id,
        document_id=document.id,
        document_revision_id=revision.id,
        seq=chunk.seq,
        block_kind="paragraph",
        text=chunk.text,
        position=position,
        parser_name=LEGACY_PARSER_NAME,
        parser_version=LEGACY_PARSER_VERSION,
    )
    evidence = EvidenceUnit(
        id=evidence_id,
        library_id=document.library_id,
        document_id=document.id,
        document_revision_id=revision.id,
        document_block_id=block_id,
        evidence_kind="chunk",
        position=position,
        text_quote=chunk.text,
        text_quote_hash=_quote_hash(chunk.text),
        visibility_scope=document.visibility_scope,
        security_level=document.security_level,
        status="active",
    )
    chunk_block = ChunkBlock(
        chunk_id=chunk.id,
        document_block_id=block_id,
        document_revision_id=revision.id,
        seq=0,
    )
    chunk_evidence = ChunkEvidence(
        chunk_id=chunk.id,
        evidence_id=evidence_id,
        document_revision_id=revision.id,
        seq=0,
    )
    chunk_updates = {
        "document_revision_id": revision.id,
        "block_id": block_id,
        "evidence_id": evidence_id,
        "chunk_kind": "text",
    }

    return LegacyChunkBackfillRows(
        block=block,
        evidence=evidence,
        chunk_block=chunk_block,
        chunk_evidence=chunk_evidence,
        chunk_updates=chunk_updates,
    )


async def _load_or_create_state(
    db: AsyncSession,
    *,
    task_name: str,
    library_id: uuid.UUID | None,
) -> MigrationBackfillState:
    stmt = select(MigrationBackfillState).where(MigrationBackfillState.task_name == task_name)
    if library_id is None:
        stmt = stmt.where(MigrationBackfillState.library_id.is_(None))
    else:
        stmt = stmt.where(MigrationBackfillState.library_id == library_id)
    state = (await db.execute(stmt)).scalars().first()
    if state is not None:
        if state.status != "done":
            state.status = "running"
        return state

    state = MigrationBackfillState(
        task_name=task_name,
        library_id=library_id,
        status="running",
        processed_count=0,
        failed_count=0,
    )
    db.add(state)
    await db.flush()
    return state


async def _legacy_chunks_for_document(db: AsyncSession, document_id: uuid.UUID) -> list[Chunk]:
    stmt = (
        select(Chunk)
        .where(Chunk.document_id == document_id, Chunk.document_revision_id.is_(None))
        .order_by(Chunk.seq, Chunk.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _backfill_document(db: AsyncSession, document: Document) -> None:
    revision_rows = build_legacy_revision_backfill_rows(document)
    await db.merge(revision_rows.revision)

    for key, value in revision_rows.document_updates.items():
        existing = getattr(document, key)
        if existing is None or existing == value:
            setattr(document, key, value)

    for chunk in await _legacy_chunks_for_document(db, document.id):
        chunk_rows = build_legacy_chunk_backfill_rows(document, revision_rows.revision, chunk)
        await db.merge(chunk_rows.block)
        await db.merge(chunk_rows.evidence)
        await db.merge(chunk_rows.chunk_block)
        await db.merge(chunk_rows.chunk_evidence)
        for key, value in chunk_rows.chunk_updates.items():
            existing = getattr(chunk, key)
            if existing is None or existing == value:
                setattr(chunk, key, value)


async def backfill_v02_m1_batch(
    db: AsyncSession,
    *,
    task_name: str = "v02_m1_evidence_foundation",
    library_id: uuid.UUID | None = None,
    batch_size: int = 100,
) -> BackfillBatchResult:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    state = await _load_or_create_state(db, task_name=task_name, library_id=library_id)
    stmt = select(Document).where(Document.deleted_at.is_(None))
    if library_id is not None:
        stmt = stmt.where(Document.library_id == library_id)
    if state.last_document_id is not None:
        stmt = stmt.where(Document.id > state.last_document_id)
    stmt = stmt.order_by(Document.id).limit(batch_size)
    documents = list((await db.execute(stmt)).scalars().all())

    processed = 0
    failed = 0
    for document in documents:
        try:
            async with db.begin_nested():
                await _backfill_document(db, document)
                state.last_document_id = document.id
                state.processed_count = (state.processed_count or 0) + 1
                state.last_error = None
            processed += 1
        except Exception as exc:
            failed += 1
            state.failed_count = (state.failed_count or 0) + 1
            state.last_error = str(exc)

    exhausted = len(documents) < batch_size
    if exhausted:
        state.status = "done"
        state.finished_at = datetime.now(timezone.utc)
    else:
        state.status = "running"
    await db.commit()

    return BackfillBatchResult(processed=processed, failed=failed, exhausted=exhausted)
