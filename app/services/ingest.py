"""摄入服务：text → chunks → 写库 → 入队。

文档身份（#4 规则）：
  - 带 external_id：身份 = (library_id, external_id)，**只**按它解析，不做内容去重；
    不同 external_id 即使内容相同也是不同文档。
  - 不带 external_id：身份 = (library_id, content_hash)，按内容去重。
由 documents 表两个「活动行」部分唯一索引在 DB 层兜底（见迁移 0008）；并发插入撞索引
时捕获 IntegrityError、回滚、重查胜出记录返回。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import delete as sa_delete, func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.services.evidence_write_path import PreparedChunk, create_evidence_generation
from app.services.metadata_guard import validate_external_metadata
from app.services import splitter as splitter_service

log = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_write_enabled() -> bool:
    return bool(getattr(settings, "enable_evidence_write_path", False))


def _chunk_text_and_metadata(
    chunk: str | dict, *, title: str | None, external_id: str | None, revision: int
) -> tuple[str, dict | None]:
    base = {"title": title, "external_id": external_id} if (title or external_id) else {}
    if isinstance(chunk, str):
        return chunk, (base or None)
    text = str(chunk.get("text") or "")
    metadata = dict(base)
    metadata.update({
        "source_start": int(chunk["source_start"]),
        "source_end": int(chunk["source_end"]),
        "location": chunk.get("location") or {},
        "source_revision": revision,
    })
    # span hash 用于后续验证原文完整性（不硬依赖 chunk.text == text[start:end]）
    if chunk.get("source_span_hash"):
        metadata["source_span_hash"] = str(chunk["source_span_hash"])
    if chunk.get("source_ranges"):
        metadata["source_ranges"] = list(chunk["source_ranges"])
    return text, metadata


def _prepare_evidence_chunks(
    chunks_text: list[str | dict], *, title: str | None, external_id: str | None, revision: int
) -> list[PreparedChunk]:
    prepared: list[PreparedChunk] = []
    for chunk in chunks_text:
        txt, chunk_metadata = _chunk_text_and_metadata(
            chunk, title=title, external_id=external_id, revision=revision
        )
        prepared.append(PreparedChunk(text=txt, metadata=chunk_metadata))
    return prepared


async def _find_active(
    db: AsyncSession,
    library_id: uuid.UUID,
    external_id: str | None,
    content_hash: str,
    source_path: str | None = None,
) -> Document | None:
    """按身份规则查活动（未删）文档：external_id 优先，否则 content_hash。

    用 limit(1) 而非 scalar_one_or_none：即便唯一索引尚未建/历史有重复也不会 500（#14）。
    """
    stmt = select(Document).where(
        Document.library_id == library_id,
        Document.deleted_at.is_(None),
    )
    if source_path is not None:
        stmt = stmt.where(Document.source_path == source_path)
    elif external_id is not None:
        stmt = stmt.where(Document.external_id == external_id)
    else:
        stmt = stmt.where(
            Document.source_path.is_(None),
            Document.external_id.is_(None),
            Document.content_hash == content_hash,
        )
    stmt = stmt.order_by(Document.created_at.desc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _supersede_active_jobs(db: AsyncSession, document_id: uuid.UUID) -> None:
    """把某文档所有 pending/processing job 标 superseded（#6 §5.1）——它们对应旧 revision。"""
    await db.execute(
        sa_update(EmbeddingJob)
        .where(
            EmbeddingJob.document_id == document_id,
            EmbeddingJob.status.in_(("pending", "processing")),
        )
        .values(status="superseded", finished_at=func.now())
    )


async def _new_generation(
    db: AsyncSession, library: Library, document: Document, *, rebuild_operation_id: uuid.UUID | None = None
) -> EmbeddingJob:
    """为已存在文档开新代际（#6 §4 写路径）：current_revision+=1 → supersede 旧 job → 建带新 revision 的 job。

    调用方须已按锁序持有 library 锁 + document 行锁（见设计 §5.2）。
    """
    document.current_revision = (document.current_revision or 0) + 1
    await _supersede_active_jobs(db, document.id)
    job = EmbeddingJob(
        library_id=library.id,
        document_id=document.id,
        status="pending",
        document_revision=document.current_revision,
        rebuild_operation_id=rebuild_operation_id,
    )
    db.add(job)
    await db.flush()
    return job


async def _latest_job_and_count(db: AsyncSession, document_id: uuid.UUID) -> tuple[EmbeddingJob | None, int]:
    job = (await db.execute(
        select(EmbeddingJob).where(EmbeddingJob.document_id == document_id)
        .order_by(EmbeddingJob.created_at.desc()).limit(1)
    )).scalars().first()
    cnt = (await db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )).scalar_one()
    return job, int(cnt)


async def ingest_text(
    *,
    db: AsyncSession,
    library: Library,
    text: str,
    title: str | None,
    external_id: str | None,
    metadata: dict[str, Any] | None,
    splitter: str,
    created_by: uuid.UUID | None,
    visibility_scope: str | None = None,
    security_level: str | None = None,
    chunks: list[str | dict] | None = None,
    source_path: str | None = None,
) -> tuple[Document, EmbeddingJob, int, bool]:
    """返回 (document, job, chunk_count, was_existing)。

    chunks 不为 None 时直接用它作为分片（跳过 split_text）——用于表格感知切分等
    上游已组好块的场景；content_hash / 去重仍按扁平 text 计算，语义不变。
    """
    if _evidence_write_enabled():
        validate_external_metadata(metadata)
    chash = _content_hash(text)

    # 身份解析（external_id 优先，否则 content_hash）→ 命中已有活动文档直接返回（不重复 embed）
    existing = await _find_active(
        db, library.id, external_id, chash, source_path=source_path
    )
    if existing is not None:
        log.info("ingest dedup hit: lib=%s ext=%s hash=%s doc_id=%s",
                 library.slug, external_id, chash[:8], existing.id)
        job, chunk_count = await _latest_job_and_count(db, existing.id)
        return existing, job, chunk_count, True

    # 切分（上游已组好块则直接用）
    chunks_text = chunks if chunks is not None else splitter_service.split_text(
        text,
        chunk_size=library.chunk_size,
        chunk_overlap=library.chunk_overlap,
        splitter=splitter,
    )
    if not chunks_text:
        raise ValueError("text produced zero chunks after splitting")

    doc = Document(
        library_id=library.id,
        external_id=external_id,
        title=title,
        doc_metadata=metadata,
        content_hash=chash,
        source_path=source_path,
        current_revision=1,          # #6：新文档索引版本从 1 起（DB 默认已移除，须显式赋值）
        visibility_scope=visibility_scope,
        security_level=security_level,
        status="pending",
        created_by=created_by,
    )
    try:
        # 用 SAVEPOINT 只包住本次 INSERT：撞唯一索引时只回滚这一条，
        # 绝不动同一 session/事务里已成功的其它文档（批量导入安全的关键）。
        async with db.begin_nested():
            db.add(doc)
            await db.flush()  # 拿到 doc.id；可能撞活动行唯一索引（并发同身份插入）
    except IntegrityError:
        # 并发：另一请求已用相同身份建好 → savepoint 已回滚本条，重查胜出记录返回（#4）。
        # 不调用 session 级 rollback（那会把同批前面成功的文档也回滚）。
        if doc in db:
            db.expunge(doc)  # 确保被回滚的 doc 不会在端点 commit 时被再次 INSERT
        winner = await _find_active(
            db, library.id, external_id, chash, source_path=source_path
        )
        if winner is None:
            raise  # 不是身份冲突（其它约束）→ 抛出
        log.info("ingest race resolved: lib=%s ext=%s hash=%s winner=%s",
                 library.slug, external_id, chash[:8], winner.id)
        job, chunk_count = await _latest_job_and_count(db, winner.id)
        return winner, job, chunk_count, True

    if _evidence_write_enabled():
        prepared_chunks = _prepare_evidence_chunks(
            chunks_text, title=title, external_id=external_id, revision=doc.current_revision
        )
        result = await create_evidence_generation(
            db,
            library=library,
            document=doc,
            normalized_text=text,
            title=title,
            document_metadata=metadata,
            splitter=splitter,
            created_by=created_by,
            prepared_chunks=prepared_chunks,
        )
        log.info(
            "evidence ingest queued: lib=%s doc_id=%s rev=%s revision_id=%s chunks=%s job_id=%s",
            library.slug,
            doc.id,
            doc.current_revision,
            result.revision.id,
            len(result.chunks),
            result.job.id,
        )
        return doc, result.job, len(result.chunks), False

    chunk_objs: list[Chunk] = []
    for seq, chunk in enumerate(chunks_text):
        txt, chunk_metadata = _chunk_text_and_metadata(
            chunk, title=title, external_id=external_id, revision=doc.current_revision
        )
        chunk_objs.append(
            Chunk(
                document_id=doc.id,
                library_id=library.id,
                seq=seq,
                text=txt,
                token_count=len(txt),  # 用字符数代替；真要 token 再接 tiktoken
                chunk_metadata=chunk_metadata,
            )
        )
    db.add_all(chunk_objs)

    job = EmbeddingJob(
        library_id=library.id, document_id=doc.id, status="pending",
        document_revision=doc.current_revision,   # =1
    )
    db.add(job)

    await db.flush()
    log.info(
        "ingest queued: lib=%s doc_id=%s rev=%s chunks=%s job_id=%s",
        library.slug, doc.id, doc.current_revision, len(chunk_objs), job.id,
    )
    return doc, job, len(chunk_objs), False


async def reingest_document(
    *,
    db: AsyncSession,
    library: Library,
    document: Document,
    new_text: str,
    title: str | None,
    metadata: dict[str, Any] | None,
    splitter: str,
    force: bool = False,
    chunks: list[str | dict] | None = None,
) -> tuple[EmbeddingJob | None, int, bool]:
    """更新已存在文档：删旧 chunk → 用新文本重切 → 更新 doc → 新建 pending job。

    chunks 不为 None 时直接用它作分片（跳过 split_text）；no-op 判定仍按扁平 new_text。

    返回 (job, chunk_count, changed)。changed=False 表示判定为 no-op、未做改动。
    force=True 时跳过 no-op 判定（总是重切+重 embed）—— 用于显式 PUT 更新，
    因为 splitter 未持久化、无法检测"仅切分方式变化"，显式更新一律重做最稳。
    no-op 判定（仅 force=False 时）只看正文/title/metadata，给 external_id upsert 做幂等。

    #6：changed 时走 _new_generation（current_revision+=1 + supersede 旧 job）。旧 Qdrant points
    **不再**由调用方同步删除（决策 1）——靠检索按 current_revision 过滤即不可见，物理清理走批次 B outbox。
    """
    if _evidence_write_enabled():
        validate_external_metadata(metadata)
    new_hash = _content_hash(new_text)
    unchanged = (
        not force
        and new_hash == document.content_hash
        and title == document.title
        and (metadata or None) == (document.doc_metadata or None)
    )
    if unchanged:
        cnt = await db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id)
        )
        return None, int(cnt.scalar_one()), False

    chunks_text = chunks if chunks is not None else splitter_service.split_text(
        new_text,
        chunk_size=library.chunk_size,
        chunk_overlap=library.chunk_overlap,
        splitter=splitter,
    )
    if not chunks_text:
        raise ValueError("text produced zero chunks after splitting")

    # 删旧 chunk（旧 Qdrant points 不在此同步清，靠 revision 过滤 + 批次 B outbox）
    await db.execute(sa_delete(Chunk).where(Chunk.document_id == document.id))

    document.content_hash = new_hash
    document.title = title
    document.doc_metadata = metadata
    document.status = "pending"
    document.last_error = None

    # 开新代际：current_revision+=1 + supersede 旧 job + 建带新 revision 的 job
    job = await _new_generation(db, library, document)

    if _evidence_write_enabled():
        prepared_chunks = _prepare_evidence_chunks(
            chunks_text, title=title, external_id=document.external_id, revision=document.current_revision
        )
        result = await create_evidence_generation(
            db,
            library=library,
            document=document,
            normalized_text=new_text,
            title=title,
            document_metadata=metadata,
            splitter=splitter,
            created_by=document.created_by,
            prepared_chunks=prepared_chunks,
            job=job,
        )
        from app.services import cleanup as cleanup_service
        if not settings.enable_revision_id_worker:
            await cleanup_service.enqueue_delete_before_revision(
                db, library, document.id, document.current_revision)
        log.info("evidence reingest queued: lib=%s doc_id=%s rev=%s revision_id=%s chunks=%s job_id=%s",
                 library.slug, document.id, document.current_revision, result.revision.id, len(result.chunks), job.id)
        return job, len(result.chunks), True

    for seq, chunk in enumerate(chunks_text):
        txt, chunk_metadata = _chunk_text_and_metadata(
            chunk, title=title, external_id=document.external_id, revision=document.current_revision
        )
        db.add(Chunk(
            document_id=document.id,
            library_id=library.id,
            seq=seq,
            text=txt,
            token_count=len(txt),
            chunk_metadata=chunk_metadata,
        ))

    # #7：入 cleanup outbox 删旧 revision points（target=新 current_revision，删 < target 与缺 revision）
    from app.services import cleanup as cleanup_service
    await cleanup_service.enqueue_delete_before_revision(
        db, library, document.id, document.current_revision)
    log.info("reingest queued: lib=%s doc_id=%s rev=%s chunks=%s job_id=%s",
             library.slug, document.id, document.current_revision, len(chunks_text), job.id)
    return job, len(chunks_text), True
