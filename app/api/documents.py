"""库文档管理：摄入 / 列表 / 单查 / 删除 / 统计。"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status, File, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.db import get_db
from app.deps import require_lib
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.user import User
from app.schemas.documents import (
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentRead,
    LibraryStats,
    QueryRequest,
    QueryResultItem,
    QueryResponse,
)
from app.services import embedding, ingest as ingest_service, qdrant, source_enrichment

log = logging.getLogger(__name__)
router = APIRouter(prefix="/libraries/{slug}", tags=["documents"])


@router.post(
    "/documents",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest(
    body: DocumentIngestRequest,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentIngestResponse:
    try:
        doc, job, chunk_count, _existing = await ingest_service.ingest_text(
            db=db,
            library=lib,
            text=body.text,
            title=body.title,
            external_id=body.external_id,
            metadata=body.metadata,
            splitter=body.splitter,
            created_by=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return DocumentIngestResponse(
        document_id=doc.id,
        status=doc.status,
        chunk_count=chunk_count,
        job_id=job.id if job is not None else uuid.uuid4(),  # 兜底，理论 ingest 必返 job
    )


@router.get("/documents", response_model=list[DocumentRead])
async def list_documents(
    status_filter: Optional[str] = Query(default=None, alias="status",
                                         pattern="^(pending|processing|ready|failed|deleted)$"),
    external_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    stmt = (
        select(Document)
        .where(Document.library_id == lib.id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    if external_id:
        stmt = stmt.where(Document.external_id == external_id)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None or doc.library_id != lib.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    background: BackgroundTasks,
    lib: Library = Depends(require_lib("delete")),
    db: AsyncSession = Depends(get_db),
) -> None:
    doc = await db.get(Document, document_id)
    if doc is None or doc.library_id != lib.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Document).where(Document.id == doc.id).values(
            deleted_at=now, status="deleted", updated_at=now
        )
    )
    await db.commit()
    collection = lib.qdrant_collection
    doc_id_str = str(doc.id)

    async def _purge():
        try:
            await qdrant.delete_points_by_document_id(collection, doc_id_str)
        except Exception:  # noqa: BLE001
            log.exception("delete_points failed: collection=%s doc=%s", collection, doc_id_str)

    background.add_task(_purge)
    return None


@router.get("/stats", response_model=LibraryStats)
async def library_stats(
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> LibraryStats:
    doc_count = (await db.execute(
        select(func.count(Document.id)).where(
            Document.library_id == lib.id, Document.deleted_at.is_(None)
        )
    )).scalar_one()
    chunk_count = (await db.execute(
        select(func.count(Chunk.id)).where(Chunk.library_id == lib.id)
    )).scalar_one()

    job_counts_row = await db.execute(
        select(EmbeddingJob.status, func.count(EmbeddingJob.id))
        .where(EmbeddingJob.library_id == lib.id)
        .group_by(EmbeddingJob.status)
    )
    counts = {row[0]: int(row[1]) for row in job_counts_row.all()}
    return LibraryStats(
        library_slug=lib.slug,
        document_count=int(doc_count),
        chunk_count=int(chunk_count),
        pending_jobs=counts.get("pending", 0),
        processing_jobs=counts.get("processing", 0),
        failed_jobs=counts.get("failed", 0),
    )


@router.post("/query", response_model=QueryResponse)
async def query_library(
    body: QueryRequest,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    try:
        vector = await embedding.embed_one(
            body.query, model=lib.embedding_model, base_url=lib.embedding_base_url
        )
    except Exception as exc:
        log.exception("Embedding query failed: %s", str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "embedding service failed")

    try:
        raw = await qdrant.search(
            collection=lib.qdrant_collection,
            vector=vector,
            limit=body.limit,
            with_payload=True,
        )
    except Exception as exc:
        log.exception("Qdrant search failed: %s", str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "vector search failed")

    payloads = [(item.get("payload") or {}) for item in raw]

    # 源库补全：payload 只有外键时回查源库取正文（未配置/源库不可达则全 None，走 payload.text）。
    # 运行期错误已在 enrich_payloads 内优雅降级；这里只需硬失败 SourceConfigError。
    try:
        enr = await source_enrichment.enrich_payloads(lib.source_config, payloads)
    except source_enrichment.SourceConfigError as exc:
        log.error("source_config invalid for lib=%s: %s", lib.slug, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "source_config invalid")

    internal_keys = {"text", "title", "library_id", "document_id", "chunk_id", "seq"}
    extra_columns = enr.parsed["extra_columns"] if enr.enabled else []
    results = []
    for item, payload, enriched, src_row in zip(raw, payloads, enr.texts, enr.rows):
        text = enriched if enriched is not None else (payload.get("text") or "")
        metadata = {k: v for k, v in payload.items() if k not in internal_keys}
        # 并入源库 extra_columns
        if src_row:
            for col in extra_columns:
                metadata.setdefault(col, src_row.get(col))
        results.append(QueryResultItem(
            text=text or "",
            similarity=float(item.get("score") or 0.0),
            document_id=str(payload.get("document_id") or ""),
            chunk_id=str(payload.get("chunk_id") or ""),
            title=payload.get("title"),
            metadata=metadata,
        ))
    return QueryResponse(results=results)


@router.post("/import-file", status_code=status.HTTP_201_CREATED)
async def import_file(
    file: UploadFile = File(...),
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
):
    filename = file.filename or "imported_file"
    content = await file.read()

    documents_to_ingest = []

    if filename.endswith(".json"):
        try:
            data = json.loads(content.decode("utf-8"))
            if isinstance(data, list):
                for item in data:
                    text = item.get("text")
                    if not text:
                        continue
                    documents_to_ingest.append({
                        "text": text,
                        "title": item.get("title") or filename,
                        "external_id": item.get("external_id"),
                        "metadata": item.get("metadata"),
                        "splitter": item.get("splitter", "text")
                    })
            elif isinstance(data, dict):
                text = data.get("text")
                if text:
                    documents_to_ingest.append({
                        "text": text,
                        "title": data.get("title") or filename,
                        "external_id": data.get("external_id"),
                        "metadata": data.get("metadata"),
                        "splitter": data.get("splitter", "text")
                    })
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid JSON format: {str(e)}")

    elif filename.endswith(".csv"):
        try:
            text_stream = io.StringIO(content.decode("utf-8"))
            reader = csv.DictReader(text_stream)
            for row in reader:
                # Find first column matching 'text' or 'content'
                text_col = next((col for col in reader.fieldnames or [] if col.lower() in ("text", "content")), None)
                if not text_col and reader.fieldnames:
                    text_col = reader.fieldnames[0]

                if not text_col:
                    continue

                text = row.get(text_col)
                if not text:
                    continue

                title_col = next((col for col in reader.fieldnames or [] if col.lower() in ("title", "name")), None)
                title = row.get(title_col) if title_col else filename

                ext_col = next((col for col in reader.fieldnames or [] if col.lower() in ("external_id", "id")), None)
                ext_id = row.get(ext_col) if ext_col else None

                documents_to_ingest.append({
                    "text": text,
                    "title": title,
                    "external_id": ext_id,
                    "metadata": None,
                    "splitter": "text"
                })
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid CSV format: {str(e)}")

    else:
        # Treat as plain text
        try:
            text = content.decode("utf-8")
            if not text.strip():
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is empty")
            documents_to_ingest.append({
                "text": text,
                "title": filename,
                "external_id": None,
                "metadata": None,
                "splitter": "text"
            })
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid text encoding: {str(e)}")

    if not documents_to_ingest:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No valid content found to ingest")

    ingested = []
    for doc_data in documents_to_ingest:
        try:
            doc, job, chunk_count, _existing = await ingest_service.ingest_text(
                db=db,
                library=lib,
                text=doc_data["text"],
                title=doc_data["title"],
                external_id=doc_data["external_id"],
                metadata=doc_data["metadata"],
                splitter=doc_data["splitter"],
                created_by=user.id,
            )
            ingested.append({
                "document_id": str(doc.id),
                "title": doc_data["title"],
                "chunk_count": chunk_count,
                "status": doc.status
            })
        except ValueError as exc:
            log.warning("Import doc failed: %s", str(exc))

    await db.commit()
    return {"status": "success", "imported_count": len(ingested), "documents": ingested}

