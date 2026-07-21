from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.schemas.v02_m4 import ChunkSourceRead, EvidenceRead


def _bounded_window(text: str | None, start: int | None, end: int | None, window: int) -> tuple[str, int | None, int | None]:
    source = text or ""
    if start is None or end is None or start < 0 or end < start or end > len(source):
        return source[: max(0, window * 2)] if window else "", 0 if source else None, min(len(source), window * 2) if source else None
    window_start = max(0, start - window)
    window_end = min(len(source), end + window)
    return source[window_start:window_end], window_start, window_end


async def _document(db: AsyncSession, library: Library, document_id: uuid.UUID) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None or doc.library_id != library.id or doc.deleted_at is not None:
        raise LookupError("document not found")
    return doc


async def _revision(
    db: AsyncSession,
    library: Library,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> DocumentRevision:
    revision = await db.get(DocumentRevision, revision_id)
    if revision is None or revision.library_id != library.id or revision.document_id != document_id:
        raise LookupError("revision not found")
    return revision


def _require_revision_scope(
    document: Document,
    revision: DocumentRevision,
    target_revision_id: uuid.UUID,
    explicit_revision_id: uuid.UUID | None,
) -> None:
    if explicit_revision_id is not None:
        if explicit_revision_id != target_revision_id:
            raise LookupError("revision mismatch")
        return
    if document.current_revision_id != target_revision_id or revision.status != "ready":
        raise LookupError("historical evidence requires explicit revision")


async def list_document_revisions(db: AsyncSession, library: Library, document_id: uuid.UUID) -> list[DocumentRevision]:
    await _document(db, library, document_id)
    result = await db.execute(
        select(DocumentRevision)
        .where(DocumentRevision.library_id == library.id, DocumentRevision.document_id == document_id)
        .order_by(DocumentRevision.revision_no.desc())
    )
    return list(result.scalars().all())


async def get_document_revision(
    db: AsyncSession,
    library: Library,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> DocumentRevision:
    await _document(db, library, document_id)
    return await _revision(db, library, document_id, revision_id)


async def list_revision_blocks(
    db: AsyncSession,
    library: Library,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    block_kind: str | None = None,
    page: int | None = None,
) -> list[DocumentBlock]:
    await get_document_revision(db, library, document_id, revision_id)
    stmt = (
        select(DocumentBlock)
        .where(
            DocumentBlock.library_id == library.id,
            DocumentBlock.document_id == document_id,
            DocumentBlock.document_revision_id == revision_id,
        )
        .order_by(DocumentBlock.seq.asc())
        .limit(limit)
        .offset(offset)
    )
    if block_kind:
        stmt = stmt.where(DocumentBlock.block_kind == block_kind)
    if page is not None:
        stmt = stmt.where(DocumentBlock.page_start <= page, DocumentBlock.page_end >= page)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_revision_chunks(
    db: AsyncSession,
    library: Library,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> list[Chunk]:
    await get_document_revision(db, library, document_id, revision_id)
    result = await db.execute(
        select(Chunk)
        .where(
            Chunk.library_id == library.id,
            Chunk.document_id == document_id,
            Chunk.document_revision_id == revision_id,
        )
        .order_by(Chunk.seq.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_evidence_detail(
    db: AsyncSession,
    library: Library,
    evidence_id: uuid.UUID,
    *,
    revision_id: uuid.UUID | None = None,
    window: int = 300,
) -> EvidenceRead:
    evidence = await db.get(EvidenceUnit, evidence_id)
    if evidence is None or evidence.library_id != library.id or evidence.status != "active":
        raise LookupError("evidence not found")
    document = await _document(db, library, evidence.document_id)
    revision = await _revision(db, library, evidence.document_id, evidence.document_revision_id)
    _require_revision_scope(document, revision, evidence.document_revision_id, revision_id)
    text_window, window_start, window_end = _bounded_window(
        revision.normalized_text,
        evidence.source_start,
        evidence.source_end,
        window,
    )
    return EvidenceRead(
        id=evidence.id,
        library_id=evidence.library_id,
        document_id=evidence.document_id,
        document_revision_id=evidence.document_revision_id,
        document_block_id=evidence.document_block_id,
        evidence_kind=evidence.evidence_kind,
        status=evidence.status,
        text_quote=evidence.text_quote,
        text_window=text_window,
        window_start=window_start,
        window_end=window_end,
        source_start=evidence.source_start,
        source_end=evidence.source_end,
        page_start=evidence.page_start,
        page_end=evidence.page_end,
        title_path=evidence.title_path,
        position=evidence.position,
        evidence_metadata=evidence.evidence_metadata,
    )


async def get_chunk_source(
    db: AsyncSession,
    library: Library,
    chunk_id: uuid.UUID,
    *,
    revision_id: uuid.UUID | None = None,
    window: int = 300,
) -> ChunkSourceRead:
    chunk = await db.get(Chunk, chunk_id)
    if chunk is None or chunk.library_id != library.id:
        raise LookupError("chunk not found")
    document = await _document(db, library, chunk.document_id)
    if chunk.document_revision_id is None:
        raise LookupError("chunk has no document revision")
    revision = await _revision(db, library, chunk.document_id, chunk.document_revision_id)
    _require_revision_scope(document, revision, chunk.document_revision_id, revision_id)
    raw_evidence = await db.get(EvidenceUnit, chunk.evidence_id) if chunk.evidence_id is not None else None
    evidence = (
        raw_evidence
        if raw_evidence is not None
        and raw_evidence.library_id == library.id
        and raw_evidence.document_id == chunk.document_id
        and raw_evidence.document_revision_id == chunk.document_revision_id
        and raw_evidence.status == "active"
        else None
    )
    source_start = evidence.source_start if evidence is not None and evidence.source_start is not None else chunk.source_start
    source_end = evidence.source_end if evidence is not None and evidence.source_end is not None else chunk.source_end
    page_start = evidence.page_start if evidence is not None and evidence.page_start is not None else chunk.page_start
    page_end = evidence.page_end if evidence is not None and evidence.page_end is not None else chunk.page_end
    title_path = evidence.title_path if evidence is not None and evidence.title_path is not None else chunk.title_path
    position = evidence.position if evidence is not None and evidence.position is not None else chunk.position
    text_window, window_start, window_end = _bounded_window(
        revision.normalized_text,
        source_start,
        source_end,
        window,
    )
    return ChunkSourceRead(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_revision_id=chunk.document_revision_id,
        evidence_id=chunk.evidence_id,
        chunk_seq=chunk.seq,
        chunk_text=chunk.text,
        text_window=text_window,
        window_start=window_start,
        window_end=window_end,
        source_start=source_start,
        source_end=source_end,
        page_start=page_start,
        page_end=page_end,
        title_path=title_path,
        position=position,
    )
