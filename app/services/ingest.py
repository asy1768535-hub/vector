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

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.services import splitter as splitter_service

log = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _find_active(
    db: AsyncSession, library_id: uuid.UUID, external_id: str | None, content_hash: str
) -> Document | None:
    """按身份规则查活动（未删）文档：external_id 优先，否则 content_hash。

    用 limit(1) 而非 scalar_one_or_none：即便唯一索引尚未建/历史有重复也不会 500（#14）。
    """
    stmt = select(Document).where(
        Document.library_id == library_id,
        Document.deleted_at.is_(None),
    )
    if external_id is not None:
        stmt = stmt.where(Document.external_id == external_id)
    else:
        stmt = stmt.where(Document.external_id.is_(None), Document.content_hash == content_hash)
    stmt = stmt.order_by(Document.created_at.desc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


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
    chunks: list[str] | None = None,
) -> tuple[Document, EmbeddingJob, int, bool]:
    """返回 (document, job, chunk_count, was_existing)。

    chunks 不为 None 时直接用它作为分片（跳过 split_text）——用于表格感知切分等
    上游已组好块的场景；content_hash / 去重仍按扁平 text 计算，语义不变。
    """
    chash = _content_hash(text)

    # 身份解析（external_id 优先，否则 content_hash）→ 命中已有活动文档直接返回（不重复 embed）
    existing = await _find_active(db, library.id, external_id, chash)
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
        status="pending",
        created_by=created_by,
    )
    db.add(doc)
    try:
        await db.flush()  # 拿到 doc.id；此处可能撞活动行唯一索引（并发同身份插入）
    except IntegrityError:
        # 并发：另一个请求刚用相同身份建好了 → 回滚本次插入，重查胜出记录返回（#4）
        await db.rollback()
        winner = await _find_active(db, library.id, external_id, chash)
        if winner is None:
            raise  # 不是身份冲突（其它约束）→ 抛出
        log.info("ingest race resolved: lib=%s ext=%s hash=%s winner=%s",
                 library.slug, external_id, chash[:8], winner.id)
        job, chunk_count = await _latest_job_and_count(db, winner.id)
        return winner, job, chunk_count, True

    chunk_objs: list[Chunk] = []
    for seq, txt in enumerate(chunks_text):
        chunk_objs.append(
            Chunk(
                document_id=doc.id,
                library_id=library.id,
                seq=seq,
                text=txt,
                token_count=len(txt),  # 用字符数代替；真要 token 再接 tiktoken
                chunk_metadata={"title": title, "external_id": external_id} if (title or external_id) else None,
            )
        )
    db.add_all(chunk_objs)

    job = EmbeddingJob(library_id=library.id, document_id=doc.id, status="pending")
    db.add(job)

    await db.flush()
    log.info(
        "ingest queued: lib=%s doc_id=%s chunks=%s job_id=%s",
        library.slug, doc.id, len(chunk_objs), job.id,
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
    chunks: list[str] | None = None,
) -> tuple[EmbeddingJob | None, int, bool]:
    """更新已存在文档：删旧 chunk → 用新文本重切 → 更新 doc → 新建 pending job。

    chunks 不为 None 时直接用它作分片（跳过 split_text）；no-op 判定仍按扁平 new_text。

    返回 (job, chunk_count, changed)。changed=False 表示判定为 no-op、未做改动。
    force=True 时跳过 no-op 判定（总是重切+重 embed）—— 用于显式 PUT 更新，
    因为 splitter 未持久化、无法检测"仅切分方式变化"，显式更新一律重做最稳。
    no-op 判定（仅 force=False 时）只看正文/title/metadata，给 external_id upsert 做幂等。
    Qdrant 旧 points 的清理由调用方负责（需要 await HTTP）。
    """
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

    # 删旧 chunk（旧 Qdrant points 由调用方按 document_id 清）
    await db.execute(sa_delete(Chunk).where(Chunk.document_id == document.id))

    document.content_hash = new_hash
    document.title = title
    document.doc_metadata = metadata
    document.status = "pending"
    document.last_error = None

    for seq, txt in enumerate(chunks_text):
        db.add(Chunk(
            document_id=document.id,
            library_id=library.id,
            seq=seq,
            text=txt,
            token_count=len(txt),
            chunk_metadata={"title": title, "external_id": document.external_id}
            if (title or document.external_id) else None,
        ))

    job = EmbeddingJob(library_id=library.id, document_id=document.id, status="pending")
    db.add(job)
    await db.flush()
    log.info("reingest queued: lib=%s doc_id=%s chunks=%s job_id=%s",
             library.slug, document.id, len(chunks_text), job.id)
    return job, len(chunks_text), True
