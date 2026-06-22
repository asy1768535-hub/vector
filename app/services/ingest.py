"""摄入服务：text → chunks → 写库 → 入队。

幂等：相同 (library_id, content_hash) 已存在的文档直接返回；不会重复 embed。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.services import splitter as splitter_service

log = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    # 幂等检查
    existing_row = await db.execute(
        select(Document).where(
            Document.library_id == library.id,
            Document.content_hash == chash,
            Document.deleted_at.is_(None),
        )
    )
    existing = existing_row.scalar_one_or_none()
    if existing is not None:
        log.info("ingest dedup hit: lib=%s hash=%s doc_id=%s", library.slug, chash[:8], existing.id)
        # 找该文档最新的 job 一并返回（若 ready 就找最近一条；都没有则不返回 job）
        job_row = await db.execute(
            select(EmbeddingJob).where(EmbeddingJob.document_id == existing.id)
            .order_by(EmbeddingJob.created_at.desc()).limit(1)
        )
        job = job_row.scalar_one_or_none()
        chunk_count_row = await db.execute(
            select(Chunk.id).where(Chunk.document_id == existing.id)
        )
        chunk_count = len(chunk_count_row.scalars().all())
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
    await db.flush()  # 拿到 doc.id

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
