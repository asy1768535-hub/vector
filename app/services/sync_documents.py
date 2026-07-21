from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.sync_source import SyncSource
from app.schemas.v02_m4 import SyncDocumentDeleteRequest, SyncDocumentResult, SyncDocumentUpsertRequest
from app.services import cleanup as cleanup_service
from app.services import folders as folders_service
from app.services import ingest as ingest_service
from app.services import splitter as splitter_service
from app.services import sync_sources as sync_sources_service
from app.services.evidence_write_path import create_evidence_generation
from app.services.metadata_guard import validate_external_metadata


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sync_document_identity_statement(library_id: uuid.UUID, sync_source_id: uuid.UUID, external_id: str):
    return select(Document).where(
        Document.library_id == library_id,
        Document.sync_source_id == sync_source_id,
        Document.external_id == external_id,
        Document.deleted_at.is_(None),
    )


async def find_sync_document(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    external_id: str,
    *,
    for_update: bool = False,
) -> Document | None:
    stmt = sync_document_identity_statement(library.id, source.id, external_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.order_by(Document.created_at.desc()).limit(1))).scalars().first()


def is_revision_affecting_change(
    document: Document,
    *,
    text: str,
    title: str | None,
    metadata: dict | None,
    visibility_scope: str | None,
    security_level: str | None,
) -> bool:
    return not (
        content_hash(text) == document.content_hash
        and title == document.title
        and (metadata or None) == (document.doc_metadata or None)
        and visibility_scope == document.visibility_scope
        and security_level == document.security_level
    )


def _result(
    payload,
    *,
    operation: str,
    document: Document | None = None,
    job: EmbeddingJob | None = None,
    chunk_count: int = 0,
) -> SyncDocumentResult:
    return SyncDocumentResult(
        external_id=payload.external_id,
        operation=operation,
        document_id=document.id if document is not None else None,
        document_revision_id=(
            job.document_revision_id
            if job is not None and job.document_revision_id is not None
            else document.latest_revision_id if document is not None else None
        ),
        job_id=job.id if job is not None else None,
        document_status=document.status if document is not None else None,
        revision_status=job.status if job is not None and job.document_revision_id is not None else None,
        job_status=job.status if job is not None else None,
        chunk_count=chunk_count,
    )


async def _create_sync_document(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    payload: SyncDocumentUpsertRequest,
    *,
    folder_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
) -> SyncDocumentResult:
    chunks_text = splitter_service.split_text(
        payload.text,
        chunk_size=library.chunk_size,
        chunk_overlap=library.chunk_overlap,
        splitter=payload.splitter,
    )
    if not chunks_text:
        raise ValueError("text produced zero chunks after splitting")

    doc = Document(
        library_id=library.id,
        folder_id=folder_id,
        sync_source_id=source.id,
        external_id=payload.external_id,
        title=payload.title,
        doc_metadata=payload.metadata,
        content_hash=content_hash(payload.text),
        current_revision=1,
        visibility_scope=payload.visibility_scope,
        security_level=payload.security_level,
        status="pending",
        created_by=created_by,
    )
    try:
        async with db.begin_nested():
            db.add(doc)
            await db.flush()
    except IntegrityError:
        try:
            db.expunge(doc)
        except Exception:
            pass
        winner = await find_sync_document(db, library, source, payload.external_id)
        if winner is None:
            raise
        return _result(payload, operation="unchanged", document=winner)

    prepared_chunks = ingest_service._prepare_evidence_chunks(
        chunks_text,
        title=payload.title,
        external_id=payload.external_id,
        revision=1,
    )
    generation = await create_evidence_generation(
        db,
        library=library,
        document=doc,
        normalized_text=payload.text,
        title=payload.title,
        document_metadata=payload.metadata,
        splitter=payload.splitter,
        created_by=created_by,
        prepared_chunks=prepared_chunks,
    )
    return _result(payload, operation="created", document=doc, job=generation.job, chunk_count=len(generation.chunks))


async def upsert_sync_document(
    db: AsyncSession,
    library: Library,
    source_key: str,
    payload: SyncDocumentUpsertRequest,
    *,
    created_by: uuid.UUID | None,
) -> SyncDocumentResult:
    validate_external_metadata(payload.metadata)
    source = await sync_sources_service.require_active_sync_source(db, library, source_key)
    folder_id = await folders_service.ensure_folder_path(db, library, payload.folder_path) if "folder_path" in payload.model_fields_set else None
    document = await find_sync_document(db, library, source, payload.external_id, for_update=True)
    if document is None:
        return await _create_sync_document(db, library, source, payload, folder_id=folder_id, created_by=created_by)

    if "folder_path" in payload.model_fields_set:
        folders_service.apply_document_folder(document, folder_id)

    if not is_revision_affecting_change(
        document,
        text=payload.text,
        title=payload.title,
        metadata=payload.metadata,
        visibility_scope=payload.visibility_scope,
        security_level=payload.security_level,
    ):
        await db.flush()
        return _result(payload, operation="unchanged", document=document)

    document.visibility_scope = payload.visibility_scope
    document.security_level = payload.security_level
    job, chunk_count, changed = await ingest_service.reingest_document(
        db=db,
        library=library,
        document=document,
        new_text=payload.text,
        title=payload.title,
        metadata=payload.metadata,
        splitter=payload.splitter,
        force=True,
    )
    return _result(payload, operation="updated" if changed else "unchanged", document=document, job=job, chunk_count=chunk_count)


async def delete_sync_document(
    db: AsyncSession,
    library: Library,
    source_key: str,
    payload: SyncDocumentDeleteRequest,
) -> SyncDocumentResult:
    source = await sync_sources_service.require_active_sync_source(db, library, source_key)
    document = await find_sync_document(db, library, source, payload.external_id, for_update=True)
    if document is None:
        return _result(payload, operation="not_found")

    now = datetime.now(timezone.utc)
    document.deleted_at = now
    document.status = "deleted"
    await db.execute(
        update(EmbeddingJob)
        .where(
            EmbeddingJob.document_id == document.id,
            EmbeddingJob.status.in_(("pending", "processing")),
        )
        .values(status="superseded", finished_at=now)
    )
    await cleanup_service.enqueue_delete_document(db, library, document.id)
    await db.flush()
    return _result(payload, operation="deleted", document=document)
