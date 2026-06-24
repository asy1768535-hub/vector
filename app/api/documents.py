"""库文档管理：摄入 / 列表 / 单查 / 删除 / 统计。"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, status, File, UploadFile
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
from app.schemas.admin import EmbeddingJobRead
from app.schemas.documents import (
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentRead,
    ImportFileResponse,
    LibraryStats,
    QueryRequest,
    QueryResultItem,
    QueryResponse,
)
from app.config import settings
from app.services import embedding, ingest as ingest_service, source_enrichment
from app.services import cleanup as cleanup_service
from app.services import rerank as rerank_svc
from app.services import retrieval as retrieval_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/libraries/{slug}", tags=["documents"])

# #13：导入文件后缀白名单（小写）。不在表内 → 415；.xls 在表内但会给出「另存为 xlsx」的专门提示。
_SUPPORTED_IMPORT_SUFFIXES = {
    ".json", ".csv", ".pdf", ".docx", ".xls", ".xlsx", ".txt", ".md", ".markdown",
}

_UPLOAD_READ_CHUNK = 1024 * 1024  # 1MiB 分块


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """#12：分块读取上传文件，累计字节一旦超过 limit 立即中止并 413。

    不先把整文件读进内存——峰值内存被限制在 ~limit（最多 limit + 一个分块），
    避免超大文件（含恶意构造）在 `await file.read()` 那一刻打爆内存。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        part = await file.read(_UPLOAD_READ_CHUNK)
        if not part:
            break
        total += len(part)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"文件过大（已超过上限 {limit} 字节）",
            )
        chunks.append(part)
    return b"".join(chunks)


async def _lock_writable(db: AsyncSession, lib: Library) -> Library:
    """写前置（#6 §6/§7.1）：库 FOR KEY SHARE 锁（与 Worker 并发、与 rebuild 互斥）+ 状态校验。

    external 库 → 409（本系统不可写）；rebuilding/failed → 503（+Retry-After）。
    持锁直到本事务 commit，保证 rebuild 的 FOR UPDATE 会等待在途写、写也看不到半途重建。
    """
    # 取 FOR KEY SHARE 锁（与 rebuild 的 FOR UPDATE 互斥，持锁至本事务 commit）。
    # **必须读锁定后的新鲜行**：等 rebuild 释放后，新鲜行的 index_state 才反映最新状态；
    # 不能用 require_lib 注入前加载的旧 lib（否则等到 rebuild 后仍按旧 ready 放行）。
    locked = (await db.execute(
        select(Library).where(Library.id == lib.id).with_for_update(read=True, key_share=True)
    )).scalars().first()
    if locked is None:   # 锁定时行已不存在（理论上软删保留行，此为防御）→ 404，不回退旧对象
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    if locked.lifecycle_mode == "external":
        raise HTTPException(status.HTTP_409_CONFLICT, "external 库由外部系统管理，本系统禁止写入")
    if locked.index_state != "ready":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "library index rebuilding",
            headers={"Retry-After": "5"},
        )
    return locked


async def _apply_reingest(db: AsyncSession, lib: Library, doc: Document, body: "DocumentIngestRequest", *, force: bool = False):
    """更新已存在文档：重切 + 开新代际（current_revision+=1 + supersede 旧 job）。

    force=True（显式 PUT）总是重做；force=False（external_id upsert）内容没变则 no-op。
    返回 (job, chunk_count)。#6 决策 1：**不再同步删 Qdrant 旧 points**——靠检索按 revision
    过滤即不可见，物理清理走批次 B 的 cleanup outbox。
    """
    job, chunk_count, changed = await ingest_service.reingest_document(
        db=db, library=lib, document=doc,
        new_text=body.text, title=body.title, metadata=body.metadata, splitter=body.splitter,
        force=force,
    )
    await db.commit()
    return job, chunk_count


async def _ingest_or_upsert(db: AsyncSession, lib: Library, user: User, doc_data: dict) -> dict:
    """单条文档摄入：有 external_id 且库内已存在同键未删文档 → reingest 覆盖更新；否则新建。

    返回 import 结果 dict（document_id/title/chunk_count/status/job_id/external_id）。
    #6 决策 1：upsert 命中改内容时**不再**同步删 Qdrant 旧 points——靠 revision 过滤即不可见，
    物理清理走批次 B outbox。
    """
    ext = doc_data.get("external_id")
    if ext:
        # external_id 非唯一索引，历史可能重复 → 取最新一条（FOR UPDATE：_new_generation 前提）
        existing = (await db.execute(
            select(Document).where(
                Document.library_id == lib.id,
                Document.external_id == ext,
                Document.deleted_at.is_(None),
            ).order_by(Document.created_at.desc()).limit(1).with_for_update()
        )).scalars().first()
        if existing is not None:
            job, chunk_count, changed = await ingest_service.reingest_document(
                db=db, library=lib, document=existing,
                new_text=doc_data["text"], title=doc_data["title"],
                metadata=doc_data["metadata"], splitter=doc_data["splitter"],
                force=False, chunks=doc_data.get("chunks"),
            )
            return {
                "document_id": str(existing.id), "title": doc_data["title"],
                "chunk_count": chunk_count, "status": existing.status,
                "job_id": str(job.id) if job is not None else None, "external_id": ext,
            }

    doc, job, chunk_count, _existing = await ingest_service.ingest_text(
        db=db, library=lib, text=doc_data["text"], title=doc_data["title"],
        external_id=ext, metadata=doc_data["metadata"], splitter=doc_data["splitter"],
        created_by=user.id, chunks=doc_data.get("chunks"),
    )
    return {
        "document_id": str(doc.id), "title": doc_data["title"],
        "chunk_count": chunk_count, "status": doc.status,
        "job_id": str(job.id) if job is not None else None, "external_id": ext,
    }


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
    await _lock_writable(db, lib)
    # external_id 自动 upsert：库内已有同 external_id 的未删文档 → 更新它（重 embed），而非新建
    if body.external_id:
        # external_id 普通索引，历史可能重复 → 取最新一条（FOR UPDATE：_new_generation 前提）
        existing = (await db.execute(
            select(Document).where(
                Document.library_id == lib.id,
                Document.external_id == body.external_id,
                Document.deleted_at.is_(None),
            ).order_by(Document.created_at.desc()).limit(1).with_for_update()
        )).scalars().first()
        if existing is not None:
            try:
                job, chunk_count = await _apply_reingest(db, lib, existing, body)
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            return DocumentIngestResponse(
                document_id=existing.id, status=existing.status,
                chunk_count=chunk_count, job_id=job.id if job is not None else None,
            )

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
        job_id=job.id if job is not None else None,
    )


@router.put("/documents/{document_id}", response_model=DocumentIngestResponse)
async def update_document(
    document_id: uuid.UUID,
    body: DocumentIngestRequest,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentIngestResponse:
    """更新文档正文/标题/metadata → 删旧向量 + 重新切分入队，worker 重 embed。"""
    await _lock_writable(db, lib)
    # 锁序 library→document：_new_generation 前提要求 document FOR UPDATE
    doc = (await db.execute(
        select(Document).where(Document.id == document_id).with_for_update()
    )).scalars().first()
    if doc is None or doc.library_id != lib.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    try:
        job, chunk_count = await _apply_reingest(db, lib, doc, body, force=True)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DocumentIngestResponse(
        document_id=doc.id, status=doc.status,
        chunk_count=chunk_count, job_id=job.id if job is not None else None,
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
    locked = await _lock_writable(db, lib)
    doc = (await db.execute(
        select(Document).where(Document.id == document_id).with_for_update()
    )).scalars().first()
    if doc is None or doc.library_id != lib.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    # #7：单事务 tombstone + supersede 在途 job + 入 cleanup outbox（移除不可靠的 BackgroundTask）。
    # 提交后检索立即不可见（按 deleted_at 过滤）；Qdrant 物理清理由 Cleanup Worker 幂等执行。
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Document).where(Document.id == doc.id).values(
            deleted_at=now, status="deleted", updated_at=now
        )
    )
    await db.execute(
        update(EmbeddingJob).where(
            EmbeddingJob.document_id == doc.id,
            EmbeddingJob.status.in_(("pending", "processing")),
        ).values(status="superseded", finished_at=now)
    )
    await cleanup_service.enqueue_delete_document(db, locked, doc.id)
    await db.commit()
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
        done_jobs=counts.get("done", 0),
        failed_jobs=counts.get("failed", 0),
        total_jobs=sum(counts.values()),
    )


@router.get("/jobs/{job_id}", response_model=EmbeddingJobRead)
async def get_job(
    job_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingJob:
    """查单条摄入任务状态（库级隔离）。外部系统可用上传返回的 job_id 轮询。"""
    job = await db.get(EmbeddingJob, job_id)
    if job is None or job.library_id != lib.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.get("/documents/{document_id}/jobs", response_model=list[EmbeddingJobRead])
async def list_document_jobs(
    document_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[EmbeddingJob]:
    """列出某文档的全部摄入任务（最新在前），看重嵌入历史/失败原因。"""
    doc = await db.get(Document, document_id)
    if doc is None or doc.library_id != lib.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    rows = await db.execute(
        select(EmbeddingJob)
        .where(EmbeddingJob.document_id == document_id, EmbeddingJob.library_id == lib.id)
        .order_by(EmbeddingJob.created_at.desc())
    )
    return list(rows.scalars().all())


@router.post("/query", response_model=QueryResponse)
async def query_library(
    body: QueryRequest,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    # 重建中/失败的库不返回半成品（#6 §9）
    if lib.index_state in ("rebuilding", "failed"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "library index rebuilding")
    try:
        vector = await embedding.embed_one(
            body.query, model=lib.embedding_model, base_url=lib.embedding_base_url
        )
    except Exception as exc:
        log.exception("Embedding query failed: %s", str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "embedding service failed")

    # rerank 生效：库级覆盖优先，否则全局，且配好了 reranker 地址
    eff_rerank = (
        (lib.rerank_enabled if lib.rerank_enabled is not None else settings.rerank_enabled)
        and rerank_svc.is_configured()
    )
    recall_limit = max(settings.rerank_candidate_k, body.limit) if eff_rerank else body.limit

    try:
        # 有界 overfetch + 可见性过滤（managed 回查 PG 丢弃陈旧/越库/已删；external 跳过）
        raw = await retrieval_svc._recall_visible(
            db, lib, lib.qdrant_collection, vector, needed=recall_limit,
        )
    except HTTPException:
        raise
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

    contents = [
        (enr.texts[i] if enr.texts[i] is not None else (payloads[i].get("text") or ""))
        for i in range(len(raw))
    ]
    # 重排：召回候选按 query 重排取前 limit；失败/未启用回退向量序（不阻断）
    order, rerank_scores = await rerank_svc.rank_candidates(
        body.query, contents, top_k=body.limit, enabled=bool(eff_rerank), log_label="query"
    )

    results = []
    for i in order:
        item, payload, enriched, src_row = raw[i], payloads[i], enr.texts[i], enr.rows[i]
        text = enriched if enriched is not None else (payload.get("text") or "")
        metadata = {k: v for k, v in payload.items() if k not in internal_keys}
        # 并入源库 extra_columns
        if src_row:
            for col in extra_columns:
                metadata.setdefault(col, src_row.get(col))
        # 双分数留痕便于排查质量：vector_score 恒有；rerank_score 仅重排命中时有
        vector_score = float(item.get("score") or 0.0)
        rr_score = rerank_scores.get(i)
        metadata["vector_score"] = vector_score
        if rr_score is not None:
            metadata["rerank_score"] = rr_score
        sim = rr_score if rr_score is not None else vector_score
        results.append(QueryResultItem(
            text=text or "",
            similarity=sim,
            document_id=str(payload.get("document_id") or ""),
            chunk_id=str(payload.get("chunk_id") or ""),
            title=payload.get("title"),
            metadata=metadata,
        ))
    return QueryResponse(results=results)


@router.post("/import-file", response_model=ImportFileResponse, status_code=status.HTTP_201_CREATED)
async def import_file(
    file: UploadFile = File(...),
    external_id: Optional[str] = Form(default=None),
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
):
    await _lock_writable(db, lib)
    filename = file.filename or "imported_file"
    # #13：后缀大小写不敏感 + 白名单。未知格式直接 415，不再「当纯文本」误吞二进制。
    lower_name = filename.lower()
    suffix = "." + lower_name.rsplit(".", 1)[1] if "." in lower_name else ""
    if suffix not in _SUPPORTED_IMPORT_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"不支持的文件类型 '{suffix or filename}'；支持：{', '.join(sorted(_SUPPORTED_IMPORT_SUFFIXES))}",
        )

    # #12：分块读取并在累计超限时立即 413（不先整文件入内存）。
    content = await _read_capped(file, settings.max_import_file_bytes)

    documents_to_ingest = []

    if suffix == ".json":
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

    elif suffix == ".csv":
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

    elif suffix == ".pdf":
        # PDF：文字层优先；图片/扫描页在库级 ocr_enabled 开启时逐页渲染 + OCR（见 docs/23）。
        # 文字版行为不变；OCR 默认关，关闭时纯扫描件给出"去开启 OCR"的明确 400。
        from app.services import ocr as ocr_svc
        from app.services import pdf_extract

        eff_ocr = lib.ocr_enabled if lib.ocr_enabled is not None else settings.ocr_enabled
        ocr_cb = ocr_svc.ocr_image if (eff_ocr and ocr_svc.is_available()) else None
        try:
            text = pdf_extract.extract_pdf_text(
                content,
                ocr_enabled=bool(eff_ocr),
                ocr=ocr_cb,
                min_text_chars=settings.pdf_ocr_min_text_chars,
                render_dpi=settings.pdf_ocr_render_dpi,
                max_ocr_pages=settings.pdf_ocr_max_pages,
            )
        except pdf_extract.PdfExtractError as exc:
            # 含 PdfOcrUnavailableError（需 OCR 但依赖缺）——消息已是用户可读的提示
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        documents_to_ingest.append({
            "text": text,
            "title": filename,
            "external_id": None,
            "metadata": None,
            "splitter": "text",
        })

    elif suffix == ".docx":
        # Word 文档：抽段落 + 表格（表格内容也入库）；库开了 OCR 则连内嵌图片一起识别。
        # 切块方式按库级 docx_table_aware 开关（null 继承全局 settings.docx_table_aware）：
        #   关（默认）：扁平正文切分——散文为主的库实测更优、成本低；
        #   开：表格感知切块（每表单独成块带表头/章节上下文）——表格召回更稳但 chunk 数/成本上升。
        try:
            from app.services.docx_extract import extract_docx_segments, extract_docx_text
            from app.services.splitter import chunk_segments
            from app.services import ocr as ocr_svc

            eff_ocr = lib.ocr_enabled if lib.ocr_enabled is not None else settings.ocr_enabled
            ocr_cb = ocr_svc.ocr_image if (eff_ocr and ocr_svc.is_available()) else None
            eff_table_aware = (
                lib.docx_table_aware if lib.docx_table_aware is not None else settings.docx_table_aware
            )

            if eff_table_aware:
                segs = extract_docx_segments(content, ocr=ocr_cb)
                # text 仅用于 content_hash / 去重，仍取扁平正文（与开关无关，保证幂等稳定）
                text = "\n".join(s["text"] if s["kind"] == "prose" else "\n".join(s["rows"]) for s in segs)
                if not text.strip():
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "DOCX 无可提取的文本")
                chunks = chunk_segments(segs, chunk_size=lib.chunk_size, chunk_overlap=lib.chunk_overlap)
                documents_to_ingest.append({
                    "text": text, "title": filename, "external_id": None,
                    "metadata": None, "splitter": "docx", "chunks": chunks,
                })
            else:
                text = extract_docx_text(content, ocr=ocr_cb)
                if not text.strip():
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "DOCX 无可提取的文本")
                documents_to_ingest.append({
                    "text": text, "title": filename, "external_id": None,
                    "metadata": None, "splitter": "text",
                })
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid DOCX format: {str(e)}")

    elif suffix == ".xls":
        # 老式 .xls 需要 xlrd（未列入依赖），短期不支持；明确提示另存为 .xlsx。
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "暂不支持 .xls 旧格式，请在 Excel 中『另存为』.xlsx 后再上传。",
        )

    elif suffix == ".xlsx":
        # 电子表格：每个工作表按表格感知切分入库
        try:
            from app.services.xlsx_extract import extract_xlsx_segments
            from app.services.splitter import chunk_segments

            segs = extract_xlsx_segments(content)
            text = "\n".join("\n".join(s["rows"]) for s in segs)
            chunks = chunk_segments(segs, chunk_size=lib.chunk_size, chunk_overlap=lib.chunk_overlap)
            if not chunks:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "表格无可提取内容")
            documents_to_ingest.append({
                "text": text, "title": filename, "external_id": None,
                "metadata": None, "splitter": "docx", "chunks": chunks,
            })
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid spreadsheet: {str(e)}")

    else:
        # 纯文本类（.txt/.md/.markdown）——已被白名单限定，不会再误吞未知二进制
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

    # 表单 external_id 只作用于"单文档"上传（txt/md/pdf/docx/xlsx 或单对象 json）；
    # 多文档（json 数组 / csv 多行）保留每条自带的 external_id，避免互相 upsert 覆盖。
    if external_id and len(documents_to_ingest) == 1 and not documents_to_ingest[0].get("external_id"):
        documents_to_ingest[0]["external_id"] = external_id

    ingested = []
    errors = []
    for idx, doc_data in enumerate(documents_to_ingest):
        try:
            ingested.append(await _ingest_or_upsert(db, lib, user, doc_data))
        except ValueError as exc:
            log.warning("Import doc[%s] failed: %s", idx, str(exc))
            errors.append({
                "index": idx,
                "title": doc_data.get("title"),
                "external_id": doc_data.get("external_id"),
                "error": str(exc),
            })

    # #10：全失败不再谎报 success。全失败 → 400（含明细）；部分失败 → 200 partial。
    if not ingested:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"message": "所有文档摄入均失败", "errors": errors},
        )

    await db.commit()
    return {
        "status": "partial" if errors else "success",
        "imported_count": len(ingested),
        "failed_count": len(errors),
        "documents": ingested,
        "errors": errors,
    }

