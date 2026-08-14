from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.schemas.v02_m4 import ChunkSourceRead, EvidenceRead
from app.schemas.evidence_locator import sha256_text
from app.services.evidence_locator_projection import parse_locator, project_locator


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


async def _verified_locator(
    db: AsyncSession,
    raw: object,
    *,
    document: Document,
    revision: DocumentRevision,
    unit_id: uuid.UUID,
    parent_unit_id: uuid.UUID | None,
    quote: str | None,
    unit_text: str | None = None,
):
    if not settings.enable_evidence_locator_read:
        return None
    locator = parse_locator(
        raw,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_no=revision.revision_no,
        unit_id=unit_id,
        parent_unit_id=parent_unit_id,
    )
    if locator is None:
        return None
    if locator.normalized_content_hash != revision.content_hash:
        return None
    file_id = locator.document_revision_file_id
    if file_id is not None:
        revision_file = await db.get(DocumentRevisionFile, file_id)
        if (
            revision_file is None
            or revision_file.document_id != document.id
            or revision_file.document_revision_id != revision.id
            or revision_file.library_id != document.library_id
            or locator.raw_file_sha256 != revision_file.sha256
        ):
            return None
    if quote is None:
        return None
    quote_hash = sha256_text(quote)
    unit_hash = sha256_text(unit_text if unit_text is not None else quote)
    if locator.quote_sha256 != quote_hash or locator.unit_text_sha256 != unit_hash:
        return None
    return locator


def _locator_text_span(locator, normalized_text: str, quote: str | None):
    text_span = locator.source.text
    if text_span is None:
        return None
    start, end = text_span.start, text_span.end
    if start < 0 or end < start or end > len(normalized_text):
        return None
    if text_span.ranges:
        reconstructed = "".join(
            normalized_text[item.start:item.end] for item in text_span.ranges
        )
    else:
        reconstructed = normalized_text[start:end]
    if quote is not None and reconstructed != quote:
        return None
    return start, end


def _locator_fields(locator, normalized_text: str, quote: str | None):
    span = _locator_text_span(locator, normalized_text, quote)
    if span is None:
        return None
    source = locator.source
    page = source.page
    position = source.model_dump(mode="json", exclude_none=True)
    return {
        "source_start": span[0],
        "source_end": span[1],
        "page_start": page.start if page is not None else None,
        "page_end": page.end if page is not None else None,
        "title_path": source.heading_path or None,
        "position": position,
    }


def _safe_evidence_metadata(
    metadata: object,
    *,
    locator,
    evidence: EvidenceUnit,
) -> dict | None:
    if not isinstance(metadata, Mapping):
        return None
    output = dict(metadata)
    output.pop("evidence_locator_v1", None)
    if locator is not None:
        try:
            output["evidence_locator_v1_projection"] = project_locator(
                locator,
                evidence_id=evidence.id,
                block_id=evidence.document_block_id,
            )
        except (TypeError, ValueError):
            pass
    return output or None


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
    locator = await _verified_locator(
        db,
        (evidence.evidence_metadata or {}).get("evidence_locator_v1")
        if isinstance(evidence.evidence_metadata, Mapping)
        else None,
        document=document,
        revision=revision,
        unit_id=evidence.id,
        parent_unit_id=evidence.document_block_id,
        quote=evidence.text_quote,
    )
    locator_fields = (
        _locator_fields(locator, revision.normalized_text or "", evidence.text_quote)
        if locator is not None
        else None
    )
    source_start = locator_fields["source_start"] if locator_fields else evidence.source_start
    source_end = locator_fields["source_end"] if locator_fields else evidence.source_end
    page_start = locator_fields["page_start"] if locator_fields else evidence.page_start
    page_end = locator_fields["page_end"] if locator_fields else evidence.page_end
    title_path = locator_fields["title_path"] if locator_fields else evidence.title_path
    position = locator_fields["position"] if locator_fields else evidence.position
    text_window, window_start, window_end = _bounded_window(
        revision.normalized_text,
        source_start,
        source_end,
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
        source_start=source_start,
        source_end=source_end,
        page_start=page_start,
        page_end=page_end,
        title_path=title_path,
        position=position,
        evidence_metadata=_safe_evidence_metadata(
            evidence.evidence_metadata,
            locator=locator,
            evidence=evidence,
        ),
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
    locator = None
    if isinstance(chunk.chunk_metadata, Mapping):
        locator = await _verified_locator(
            db,
            chunk.chunk_metadata.get("evidence_locator_v1"),
            document=document,
            revision=revision,
            unit_id=chunk.id,
            parent_unit_id=chunk.block_id,
            quote=chunk.text,
        )
    if locator is None and evidence is not None and isinstance(evidence.evidence_metadata, Mapping):
        locator = await _verified_locator(
            db,
            evidence.evidence_metadata.get("evidence_locator_v1"),
            document=document,
            revision=revision,
            unit_id=evidence.id,
            parent_unit_id=evidence.document_block_id,
            quote=evidence.text_quote,
        )
    locator_fields = (
        _locator_fields(locator, revision.normalized_text or "", chunk.text)
        if locator is not None
        else None
    )
    source_start = (
        locator_fields["source_start"] if locator_fields
        else evidence.source_start if evidence is not None and evidence.source_start is not None
        else chunk.source_start
    )
    source_end = (
        locator_fields["source_end"] if locator_fields
        else evidence.source_end if evidence is not None and evidence.source_end is not None
        else chunk.source_end
    )
    page_start = (
        locator_fields["page_start"] if locator_fields
        else evidence.page_start if evidence is not None and evidence.page_start is not None
        else chunk.page_start
    )
    page_end = (
        locator_fields["page_end"] if locator_fields
        else evidence.page_end if evidence is not None and evidence.page_end is not None
        else chunk.page_end
    )
    title_path = (
        locator_fields["title_path"] if locator_fields
        else evidence.title_path if evidence is not None and evidence.title_path is not None
        else chunk.title_path
    )
    position = (
        locator_fields["position"] if locator_fields
        else evidence.position if evidence is not None and evidence.position is not None
        else chunk.position
    )
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
