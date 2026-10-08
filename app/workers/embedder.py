"""Embedding worker：从 embedding_jobs 抢锁 → 调 bge-m3 → upsert Qdrant → 标 done。

迁移自 cpwsImportData/importdata/embedding_worker.py:88-117 的 FOR UPDATE SKIP LOCKED 模式。
精简：去掉案件域逻辑（split_blocks、case_metadata），通用化为 (library, document, chunks) 三元组。

用法：
  python -m app.workers.embedder            # 处理完 pending 即退出（cron 友好）
  python -m app.workers.embedder --watch    # 长跑：空闲时 sleep poll_seconds 后再抢
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_factory
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import embedding, qdrant
from app.services.evidence_locator_projection import chunk_locator_projection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

REVISION_POINT_NAMESPACE = uuid.UUID("a65d3d18-55c6-4df6-9f42-2f5dfdcf0a76")


@dataclass(frozen=True, slots=True)
class _LibraryPublicationSnapshot:
    id: uuid.UUID
    qdrant_collection: str
    revision_retention_enabled: bool


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def deterministic_revision_point_id(document_revision_id: uuid.UUID, chunk_id: uuid.UUID) -> str:
    return str(uuid.uuid5(REVISION_POINT_NAMESPACE, f"{document_revision_id}:{chunk_id}"))


def _point_id_for_chunk(job: EmbeddingJob, chunk: Chunk) -> str:
    if job.document_revision_id is not None:
        return deterministic_revision_point_id(job.document_revision_id, chunk.id)
    return str(chunk.id)


def eligibility(job, document, library, operation, revision=None) -> bool:
    """统一资格条件（#6 设计 §5.1）：claim / embedding 前 / 最终写入前三处复用同一条。

    operation 是该 job 的 rebuild_operations 行（普通 job 或非重建时为 None）。
    不满足 → 调用方应把 job 标 superseded（不调 embedding、不计失败）。
    """
    if document is None or library is None:
        return False
    if getattr(library, "deleted_at", None) is not None:   # #7：库已删 → 任何 job 不执行
        return False
    if document.deleted_at is not None:
        return False
    if job.document_revision != document.current_revision:
        return False
    if job.rebuild_operation_id is not None:
        revision_id = getattr(job, "document_revision_id", None)
        if revision_id is not None:
            if (revision is None or getattr(document, "current_revision_id", None) != revision_id
                    or revision.id != revision_id or revision.status != "ready"
                    or revision.document_id != job.document_id or revision.library_id != job.library_id
                    or revision.revision_no != job.document_revision_no):
                return False
        elif (settings.enable_revision_id_worker or getattr(document, "current_revision_id", None) is not None
              or getattr(document, "latest_revision_id", None) is not None):
            return False
        if getattr(library, "lifecycle_mode", None) == "external":
            return False
        if hasattr(library, "id") and library.id != getattr(job, "library_id", None):
            return False
        if operation is not None and getattr(operation, "id", job.rebuild_operation_id) != job.rebuild_operation_id:
            return False
        if (getattr(document, "library_id", None) is not None
                and document.library_id != job.library_id):
            return False
        if (operation is not None and getattr(operation, "library_id", None) is not None
                and (operation.library_id != job.library_id
                     or operation.collection_name != library.qdrant_collection)):
            return False
    elif settings.enable_revision_id_worker:
        if getattr(job, "document_revision_id", None) is None or revision is None:
            return False
        if getattr(revision, "id", None) != job.document_revision_id:
            return False
        if getattr(revision, "status", None) not in ("pending", "processing"):
            return False
        if getattr(document, "latest_revision_id", None) != job.document_revision_id:
            return False
    if library.index_state == "ready":
        # ready 时只放行普通 job；带 rebuild_operation_id 的（含失败 operation 遗留）一律拒绝
        return job.rebuild_operation_id is None
    if library.index_state == "rebuilding":
        return (
            job.rebuild_operation_id is not None
            and job.rebuild_operation_id == library.active_rebuild_operation_id
            and operation is not None
            and operation.status == "running"
        )
    # failed 或未知状态：全拒
    return False


async def _chunks_for_job(db: AsyncSession, job: EmbeddingJob, doc: Document) -> list[Chunk]:
    if (settings.enable_revision_id_worker or job.rebuild_operation_id is not None) and job.document_revision_id is not None:
        rows = await db.execute(
            select(Chunk)
            .where(Chunk.document_revision_id == job.document_revision_id,
                   Chunk.document_id == job.document_id, Chunk.library_id == job.library_id)
            .order_by(Chunk.seq)
        )
    else:
        stmt = select(Chunk).where(Chunk.document_id == doc.id)
        if job.rebuild_operation_id is not None:
            stmt = stmt.where(Chunk.library_id == job.library_id, Chunk.document_revision_id.is_(None))
        rows = await db.execute(
            stmt.order_by(Chunk.seq)
        )
    return list(rows.scalars().all())


def _build_payload(
    lib: Library,
    doc: Document,
    chunk: Chunk,
    *,
    job: EmbeddingJob | None = None,
    revision: DocumentRevision | None = None,
) -> dict:
    """构造单个 Qdrant point 的 payload。

    关键：用户提供的 doc_metadata 先展开，系统保留字段**最后**写入，
    保证 document_id/chunk_id/text/title/library_id 等系统字段恒胜，
    用户无法通过 metadata 覆盖它们（否则会破坏删除/检索完整性、伪造文档归属）。
    """
    is_rebuild = job is not None and job.rebuild_operation_id is not None
    payload = dict((revision.document_metadata if is_rebuild and revision is not None else doc.doc_metadata) or {})
    document_revision_id = (
        getattr(job, "document_revision_id", None)
        or getattr(chunk, "document_revision_id", None)
        or getattr(revision, "id", None)
    )
    document_revision_no = (
        getattr(job, "document_revision_no", None)
        or getattr(revision, "revision_no", None)
        or getattr(job, "document_revision", None)
        or doc.current_revision
    )
    payload.update(
        {
            "library_id": str(lib.id),
            "document_id": str(doc.id),
            "chunk_id": str(chunk.id),
            "seq": chunk.seq,
            "text": chunk.text,
            "title": revision.title if is_rebuild and revision is not None else doc.title,
            "external_id": doc.external_id,
            "document_revision": getattr(job, "document_revision", None) or doc.current_revision,
            "document_revision_no": document_revision_no,
            "document_revision_id": str(document_revision_id) if document_revision_id else None,
            "block_id": str(chunk.block_id) if chunk.block_id else None,
            "evidence_id": str(chunk.evidence_id) if chunk.evidence_id else None,
            "chunk_kind": chunk.chunk_kind,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "title_path": chunk.title_path,
            "source_start": chunk.source_start,
            "source_end": chunk.source_end,
            "position": chunk.position,
            "visibility_scope": doc.visibility_scope,
            "security_level": doc.security_level,
        }
    )
    locator_projection = (
        chunk_locator_projection(
            chunk,
            document_id=doc.id,
            document_revision_id=document_revision_id,
            revision_no=document_revision_no,
        )
        if settings.enable_evidence_locator_projection
        else None
    )
    if locator_projection is not None:
        payload["evidence_locator_v1_projection"] = locator_projection
    return payload


def get_worker_target_config() -> tuple[bool, uuid.UUID | None, datetime | None]:
    raw_scope_mode = os.getenv("WORKER_SCOPE_MODE", "").strip().lower()
    raw_exclude = os.getenv("WORKER_EXCLUDE_PDF", "").strip().lower()
    raw_target = os.getenv("WORKER_TARGET_LIBRARY_ID", "").strip()
    raw_min_created = os.getenv("WORKER_TASK_MIN_CREATED_AT", "").strip()

    is_all_libraries = raw_scope_mode in {"all", "all_libraries", "all-libraries"}
    is_single_library = raw_scope_mode in {"", "single", "single_library", "single-library", "targeted"}

    if not is_all_libraries and not is_single_library:
        log.critical(
            "FATAL: Invalid WORKER_SCOPE_MODE='%s'. Must be 'all_libraries' (production full-library mode) or 'single_library' (targeted mode).",
            raw_scope_mode,
        )
        sys.exit(1)

    if is_all_libraries:
        if raw_target:
            log.critical(
                "FATAL: Configuration conflict! WORKER_TARGET_LIBRARY_ID is set ('%s') but WORKER_SCOPE_MODE='%s'. "
                "Full-library production mode must not be constrained by a single target library. Remove WORKER_TARGET_LIBRARY_ID.",
                raw_target,
                raw_scope_mode,
            )
            sys.exit(1)
        if raw_min_created:
            log.critical(
                "FATAL: Configuration conflict! WORKER_TASK_MIN_CREATED_AT is set ('%s') but WORKER_SCOPE_MODE='%s'. "
                "Full-library production mode must not be constrained by trial min created at timestamp. Remove WORKER_TASK_MIN_CREATED_AT.",
                raw_min_created,
                raw_scope_mode,
            )
            sys.exit(1)
        if raw_exclude in {"1", "true", "yes", "on"}:
            log.critical(
                "FATAL: Configuration conflict! WORKER_EXCLUDE_PDF='%s' conflicts with WORKER_SCOPE_MODE='%s'. "
                "Full-library production mode must consume all documents including PDF.",
                raw_exclude,
                raw_scope_mode,
            )
            sys.exit(1)
        if raw_exclude and raw_exclude not in {"0", "false", "no", "off"}:
            log.critical(
                "FATAL: Ambiguous WORKER_EXCLUDE_PDF='%s'. In all_libraries mode it must be '0' or unset.",
                raw_exclude,
            )
            sys.exit(1)
        return False, None, None

    is_exclude = raw_exclude in {"1", "true", "yes", "on"}
    is_include = raw_exclude in {"0", "false", "no", "off"}

    # 1. 开关漏配或非法值检查：WORKER_EXCLUDE_PDF 必须明确设为 1 或 0（防止两开关同时漏配）
    if not is_exclude and not is_include:
        log.critical(
            "FATAL: Ambiguous or missing WORKER_EXCLUDE_PDF='%s'. Must be explicitly '1' (baseline exclude) or '0' (trial target).",
            raw_exclude,
        )
        sys.exit(1)

    # 2. 目标库 UUID 解析
    target_library_id: uuid.UUID | None = None
    if raw_target:
        try:
            target_library_id = uuid.UUID(raw_target)
        except (ValueError, TypeError) as exc:
            log.critical(
                "FATAL: WORKER_TARGET_LIBRARY_ID='%s' is not a valid UUID: %s",
                raw_target,
                exc,
            )
            sys.exit(1)

    # 3. 配置冲突检查：已指定目标库但 WORKER_EXCLUDE_PDF=1
    if is_exclude and target_library_id is not None:
        log.critical(
            "FATAL: Configuration conflict! WORKER_TARGET_LIBRARY_ID is set ('%s') but WORKER_EXCLUDE_PDF=1. "
            "Trial worker cannot exclude PDF, and baseline worker cannot target a specific library.",
            raw_target,
        )
        sys.exit(1)

    # 4. 试用模式检查：WORKER_EXCLUDE_PDF=0 时必须配置合法目标库 UUID
    if is_include and target_library_id is None:
        log.critical(
            "FATAL: WORKER_TARGET_LIBRARY_ID must be specified when WORKER_EXCLUDE_PDF=0 in trial mode."
        )
        sys.exit(1)

    # 5. 可选历史积压隔离过滤：仅申领指定时间戳之后的新任务
    min_created_at: datetime | None = None
    if raw_min_created:
        try:
            min_created_at = datetime.fromisoformat(raw_min_created.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            log.critical(
                "FATAL: WORKER_TASK_MIN_CREATED_AT='%s' is not a valid ISO timestamp: %s",
                raw_min_created,
                exc,
            )
            sys.exit(1)

    return is_exclude, target_library_id, min_created_at

async def _reset_stale_jobs(
    db: AsyncSession,
    target_library_id: uuid.UUID | None = None,
    min_created_at: datetime | None = None,
) -> int:
    """超时仍在 processing 的任务回收；达最大次数的直接 failed，其余重置为 pending。"""
    if target_library_id is None:
        _, target_library_id, cfg_min_created = get_worker_target_config()
        if min_created_at is None:
            min_created_at = cfg_min_created
    params: dict[str, Any] = {
        "secs": str(settings.embed_worker_stale_seconds),
        "max": settings.embed_worker_max_attempts,
    }
    lib_filter = ""
    if target_library_id is not None:
        params["target_library_id"] = target_library_id
        lib_filter = "AND library_id = :target_library_id"
    if min_created_at is not None:
        params["min_created_at"] = min_created_at
        lib_filter += " AND created_at >= :min_created_at"

    failed = await db.execute(
        text(
            f"""
            UPDATE embedding_jobs
            SET status = 'failed', worker_id = NULL, claimed_at = NULL,
                finished_at = NOW(), last_error = COALESCE(last_error, 'stale processing at max attempts')
            WHERE status = 'processing'
              {lib_filter}
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:secs || ' seconds')::interval
              AND attempt_count >= :max
            """
        ),
        params,
    )
    reset = await db.execute(
        text(
            f"""
            UPDATE embedding_jobs
            SET status = 'pending', worker_id = NULL, claimed_at = NULL
            WHERE status = 'processing'
              {lib_filter}
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:secs || ' seconds')::interval
              AND attempt_count < :max
            """
        ),
        params,
    )
    if (failed.rowcount or 0) > 0:
        await db.execute(
            text(
                """
                UPDATE document_import_jobs dij
                SET status = 'failed', worker_id = NULL, claimed_at = NULL,
                    finished_at = NOW(),
                    last_error = COALESCE(dij.last_error, 'stale embedding at max attempts')
                FROM embedding_jobs ej
                WHERE dij.embedding_job_id = ej.id
                  AND dij.status = 'processing'
                  AND ej.status = 'failed'
                  AND ej.last_error = 'stale processing at max attempts'
                  AND ej.finished_at >= NOW() - interval '10 seconds'
                """
            )
        )
    await db.commit()
    return (failed.rowcount or 0) + (reset.rowcount or 0)


async def _claim_jobs(db: AsyncSession, worker_id: str, limit: int) -> list[EmbeddingJob]:
    """FOR UPDATE SKIP LOCKED 抢锁 → 标记 processing。"""
    exclude_pdf, target_library_id, min_created_at = get_worker_target_config()
    params: dict[str, Any] = {
        "limit": limit,
        "worker_id": worker_id,
        "max_attempts": settings.embed_worker_max_attempts,
        "exclude_pdf": exclude_pdf,
    }
    if target_library_id is not None:
        params["target_library_id"] = target_library_id
        target_clause = """
              AND j.library_id = :target_library_id
              AND (
                LOWER(COALESCE(d.source_path, '')) LIKE '%.pdf'
                OR LOWER(COALESCE(d.title, '')) LIKE '%.pdf'
              )
        """
        if min_created_at is not None:
            params["min_created_at"] = min_created_at
            target_clause += " AND j.created_at >= :min_created_at"
    else:
        target_clause = """
              AND (
                NOT :exclude_pdf
                OR (
                    LOWER(COALESCE(d.source_path, '')) NOT LIKE '%.pdf'
                    AND LOWER(COALESCE(d.title, '')) NOT LIKE '%.pdf'
                )
              )
        """
    raw_sql = text(
        f"""
        WITH picked AS (
            SELECT j.id
            FROM embedding_jobs j
            JOIN documents d ON d.id = j.document_id
            JOIN sys_libraries l ON l.id = j.library_id
            WHERE j.status = 'pending'
              AND j.attempt_count < :max_attempts
              AND (j.rebuild_operation_id IS NULL OR (
                l.index_state = 'rebuilding'
                AND l.active_rebuild_operation_id = j.rebuild_operation_id
                AND EXISTS (
                  SELECT 1 FROM rebuild_operations ro
                  WHERE ro.id = j.rebuild_operation_id AND ro.status = 'running'
                    AND ro.library_id = j.library_id
                    AND ro.collection_name = l.qdrant_collection
                )
              ))
              {target_clause}
            ORDER BY j.created_at
            FOR UPDATE OF j SKIP LOCKED
            LIMIT :limit
        )
        UPDATE embedding_jobs j
        SET status = 'processing',
            worker_id = :worker_id,
            attempt_count = j.attempt_count + 1,
            claimed_at = NOW()
        FROM picked
        WHERE j.id = picked.id
        RETURNING j.id
        """
    )
    result = await db.execute(raw_sql, params)
    ids = [row[0] for row in result.all()]
    if not ids:
        await db.commit()
        return []
    rows = await db.execute(select(EmbeddingJob).where(EmbeddingJob.id.in_(ids)))
    jobs = list(rows.scalars().all())
    await db.commit()
    return jobs


async def _mark_superseded(db: AsyncSession, job: EmbeddingJob) -> None:
    """资格条件不满足（旧 revision / 库重建中 / 已删等）→ 正常终止，不计失败、不重试。"""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
            status="superseded", finished_at=now
        )
    )
    await db.execute(
        update(DocumentImportJob)
        .where(
            DocumentImportJob.embedding_job_id == job.id,
            DocumentImportJob.status == "processing",
        )
        .values(
            status="superseded",
            current_stage="embedding",
            worker_id=None,
            claimed_at=None,
            finished_at=now,
            last_error="superseded",
        )
    )
    await db.commit()
    await _finalize_rebuild_job(db, job.rebuild_operation_id)


async def _publish_revision_after_qdrant(
    db: AsyncSession,
    *,
    library: Library,
    job: EmbeddingJob,
    now: datetime | None = None,
) -> bool:
    """Publish a revision only after Qdrant upsert has succeeded."""
    if job.rebuild_operation_id is not None:
        raise ValueError("rebuild must not publish or clean up content revisions")
    if isinstance(db, AsyncSession) and job in db:
        db.expunge(job)
    now = now or datetime.now(timezone.utc)
    library_id = library.id
    library_snapshot = _LibraryPublicationSnapshot(
        id=library_id,
        qdrant_collection=library.qdrant_collection,
        revision_retention_enabled=bool(
            getattr(library, "revision_retention_enabled", False)
        ),
    )
    job_id = job.id
    document_id = job.document_id
    revision_id = job.document_revision_id
    await db.rollback()
    from app.services import cleanup as cleanup_service

    current_library = (await db.execute(
        select(Library).where(Library.id == library_id)
        .with_for_update(read=True, key_share=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (current_library is None or current_library.deleted_at is not None
            or current_library.index_state != "ready"):
        await _mark_superseded(db, job)
        return False

    doc = (
        await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    revision = (
        await db.execute(
            select(DocumentRevision)
            .where(DocumentRevision.id == revision_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if (doc is not None and doc.current_revision != job.document_revision
            or revision is not None and revision.status not in ("pending", "processing")):
        # An old indexing task must not publish, supersede or delete a content
        # revision already retained by a newer generation/rebuild.
        await _mark_superseded(db, job)
        return False

    if (
        doc is None
        or doc.deleted_at is not None
        or revision is None
        or doc.latest_revision_id != revision_id
    ):
        await db.execute(
            update(EmbeddingJob)
            .where(EmbeddingJob.id == job_id)
            .values(status="superseded", finished_at=now)
        )
        if revision_id is not None:
            await db.execute(
                update(DocumentRevision)
                .where(DocumentRevision.id == revision_id)
                .values(status="superseded", finished_at=now)
            )
            await cleanup_service.enqueue_delete_unpublished_revision_points(
                db, library_snapshot, document_id, revision_id
            )
        await db.commit()
        return False

    graph_extraction_requested = bool(
        (revision.parser_config or {}).get("graph_extraction_requested")
    )

    old_current_revision_id = doc.current_revision_id
    await db.execute(
        update(DocumentRevision)
        .where(DocumentRevision.id == revision_id)
        .values(
            status="ready",
            published_at=now,
            finished_at=now,
            last_error=None,
        )
    )
    await db.execute(
        update(EmbeddingJob)
        .where(EmbeddingJob.id == job_id)
        .values(status="done", finished_at=now, last_error=None)
    )
    # Source import is complete once the revision is published. Graph work is
    # tracked independently and must not keep the upload task in processing.
    await db.execute(
        update(DocumentImportJob)
        .where(
            DocumentImportJob.embedding_job_id == job_id,
            DocumentImportJob.status == "processing",
        )
        .values(
            status="succeeded",
            current_stage="completed",
            worker_id=None,
            claimed_at=None,
            finished_at=now,
            last_error=None,
        )
    )
    await db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.latest_revision_id == revision_id,
            Document.deleted_at.is_(None),
        )
        .values(
            current_revision_id=revision_id,
            status="ready",
            last_error=None,
            updated_at=now,
        )
    )
    if old_current_revision_id is not None and old_current_revision_id != revision_id:
        from app.services.knowledge_artifact_publication import (
            supersede_revision_artifacts,
        )

        await supersede_revision_artifacts(
            db,
            library_id=library_id,
            document_id=document_id,
            document_revision_id=old_current_revision_id,
            now=now,
        )
        from app.services.classification_jobs import (
            supersede_revision_classification_jobs,
        )

        await supersede_revision_classification_jobs(
            db,
            library_id=library_id,
            document_id=document_id,
            document_revision_id=old_current_revision_id,
            now=now,
        )
        await db.execute(
            update(DocumentRevision)
            .where(DocumentRevision.id == old_current_revision_id)
            .values(status="superseded", finished_at=now)
        )
        await cleanup_service.enqueue_delete_document_revision(
            db, library_snapshot, document_id, old_current_revision_id
        )
    retention_scope = (
        (library_id, document_id, old_current_revision_id, revision_id)
        if (
            settings.revision_retention_enabled
            and library_snapshot.revision_retention_enabled
            and old_current_revision_id is not None
            and old_current_revision_id != revision_id
        )
        else None
    )
    await db.commit()
    if retention_scope is not None:
        try:
            from app.services.revision_retention import schedule_revision_retention

            await schedule_revision_retention(
                db,
                library_id=retention_scope[0],
                document_id=retention_scope[1],
                document_revision_id=retention_scope[2],
                replacement_revision_id=retention_scope[3],
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception(
                "revision retention scheduling failed after publication: "
                "doc=%s revision=%s",
                retention_scope[1],
                retention_scope[2],
            )
    try:
        from app.services.graph_extraction_triggers import (
            enqueue_ready_revision_graph_extraction,
        )

        await enqueue_ready_revision_graph_extraction(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
            force=graph_extraction_requested,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "graph extraction auto trigger failed after publication: doc=%s revision=%s",
            document_id,
            revision_id,
        )
    try:
        from app.services.knowledge_artifact_jobs import (
            enqueue_ready_revision_artifacts,
        )

        await enqueue_ready_revision_artifacts(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "knowledge artifact auto trigger failed after publication: "
            "doc=%s revision=%s",
            document_id,
            revision_id,
        )
    try:
        from app.services.classification_jobs import (
            enqueue_ready_revision_classification,
        )

        await enqueue_ready_revision_classification(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "classification auto trigger failed after publication: "
            "doc=%s revision=%s",
            document_id,
            revision_id,
        )
    return True


async def _process_job(db: AsyncSession, job: EmbeddingJob) -> None:
    """处理单个 job：资格检查 → 拉 chunks → embed → 按锁序复核 → upsert → 标 done（#6 §5）。"""
    if isinstance(db, AsyncSession) and job in db:
        db.expunge(job)
    lib = await db.get(Library, job.library_id)
    doc = await db.get(Document, job.document_id)
    if lib is None or doc is None:
        await _mark_failed(db, job, "library or document missing")
        return
    op = await db.get(RebuildOperation, job.rebuild_operation_id) if job.rebuild_operation_id else None
    revision = (
        await db.get(DocumentRevision, job.document_revision_id)
        if (settings.enable_revision_id_worker or job.rebuild_operation_id is not None)
        and job.document_revision_id is not None
        else None
    )

    # 资格检查 #1（embedding 前，§5.3）：不满足直接 superseded，省下 embedding 成本
    if not eligibility(job, doc, lib, op, revision):
        await _mark_superseded(db, job)
        log.info("superseded (pre-embed): job=%s doc=%s rev=%s", job.id, doc.id, job.document_revision)
        return

    chunks = await _chunks_for_job(db, job, doc)
    if not chunks:
        await _mark_failed(db, job, "no chunks to embed")
        return

    if isinstance(db, AsyncSession):
        for chunk in chunks:
            if chunk in db:
                db.expunge(chunk)

    texts = [c.text for c in chunks]
    try:
        # 分批调 embedding（不持行锁；READ COMMITTED 下最终再按锁序复核最新状态）
        batch = lib.embed_batch_size or settings.embed_batch_size
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch):
            piece = await embedding.embed_texts(
                texts[i : i + batch],
                model=lib.embedding_model,
                base_url=lib.embedding_base_url,
            )
            vectors.extend(piece)
        if len(vectors) != len(chunks):
            raise RuntimeError(f"vector count mismatch: {len(vectors)} vs {len(chunks)}")
        if any(len(v) != lib.embedding_dim for v in vectors):
            raise RuntimeError(f"dim mismatch: expected {lib.embedding_dim}")

        # 资格检查 #2（写 Qdrant 前，§5.4）：按锁序 library FOR KEY SHARE → document FOR UPDATE 复核
        if (settings.enable_revision_id_worker or job.rebuild_operation_id is not None) and job.document_revision_id is not None:
            await db.rollback()
            lib_l = (await db.execute(
                select(Library).where(Library.id == job.library_id)
                .with_for_update(read=True, key_share=True)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            doc_l = (await db.execute(
                select(Document).where(Document.id == job.document_id).with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            op_l = (await db.execute(
                select(RebuildOperation).where(RebuildOperation.id == job.rebuild_operation_id)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none() if job.rebuild_operation_id else None
            revision_l = (await db.execute(
                select(DocumentRevision).where(DocumentRevision.id == job.document_revision_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if lib_l is None or doc_l is None or not eligibility(job, doc_l, lib_l, op_l, revision_l):
                await _mark_superseded(db, job)
                log.info(
                    "superseded (pre-write): job=%s doc=%s rev=%s",
                    job.id,
                    job.document_id,
                    job.document_revision,
                )
                return
            points = [
                {
                    "id": _point_id_for_chunk(job, chunk),
                    "vector": vec,
                    "payload": _build_payload(lib_l, doc_l, chunk, job=job, revision=revision_l),
                }
                for chunk, vec in zip(chunks, vectors)
            ]
            await qdrant.upsert_points(
                lib_l.qdrant_collection, points, timeout=settings.qdrant_upsert_timeout_seconds
            )
            if job.rebuild_operation_id is not None:
                now = datetime.now(timezone.utc)
                await db.execute(
                    update(EmbeddingJob).where(EmbeddingJob.id == job.id)
                    .values(status="done", finished_at=now, last_error=None)
                )
                await db.execute(
                    update(Document).where(
                        Document.id == job.document_id,
                        Document.current_revision == job.document_revision,
                        Document.current_revision_id == job.document_revision_id,
                    ).values(status="ready", last_error=None, updated_at=now)
                )
                await db.commit()
                await _finalize_rebuild_job(db, job.rebuild_operation_id)
                return
            published_slug = lib_l.slug
            published_document_id = doc_l.id
            published_revision_id = job.document_revision_id
            published = await _publish_revision_after_qdrant(db, library=lib_l, job=job)
            if published:
                log.info(
                    "done: lib=%s doc=%s revision_id=%s chunks=%s",
                    published_slug,
                    published_document_id,
                    published_revision_id,
                    len(chunks),
                )
            return

        lib_l = (await db.execute(
            select(Library).where(Library.id == job.library_id).with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        doc_l = (await db.execute(
            select(Document).where(Document.id == job.document_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        op_l = (await db.execute(
            select(RebuildOperation).where(RebuildOperation.id == job.rebuild_operation_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none() if job.rebuild_operation_id else None
        if lib_l is None or doc_l is None or not eligibility(job, doc_l, lib_l, op_l):
            await db.rollback()  # 释放行锁
            await _mark_superseded(db, job)
            log.info("superseded (pre-write): job=%s doc=%s rev=%s", job.id, job.document_id, job.document_revision)
            return

        # 准备 Qdrant points（payload 带 document_revision，由锁定的 doc 提供）
        points = [
            {"id": str(chunk.id), "vector": vec, "payload": _build_payload(lib_l, doc_l, chunk)}
            for chunk, vec in zip(chunks, vectors)
        ]
        await qdrant.upsert_points(
            lib_l.qdrant_collection, points, timeout=settings.qdrant_upsert_timeout_seconds
        )

        now = datetime.now(timezone.utc)
        await db.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
                status="done", finished_at=now, last_error=None
            )
        )
        # 状态写守护：仍是当前 revision 才置 ready（eligibility 已在锁内确认）
        await db.execute(
            update(Document).where(Document.id == doc_l.id).values(
                status="ready", last_error=None, updated_at=now
            )
        )
        await db.commit()  # 释放锁
        log.info("done: lib=%s doc=%s rev=%s chunks=%s", lib_l.slug, doc_l.id, job.document_revision, len(chunks))
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed: %s", job.id, exc)
        await _mark_failed(db, job, str(exc)[:1000])

    # 即时 finalize：rebuild job 转终态（done 或 failed）后立刻尝试收口（新 session，锁序 library→operation）。
    # 失败/崩溃由主循环周期 reconcile 兜底。
    await _finalize_rebuild_job(db, job.rebuild_operation_id)


async def _finalize_rebuild_job(db: AsyncSession, operation_id) -> None:
    if operation_id is not None:
        try:
            from app.services import rebuild as rebuild_svc
            async with AsyncSession(bind=db.bind, expire_on_commit=False) as fsession:
                await rebuild_svc.try_finalize(fsession, operation_id)
        except Exception:
            log.exception("immediate finalize failed for op=%s (reconcile 兜底)", operation_id)


async def _mark_failed(db: AsyncSession, job: EmbeddingJob, reason: str) -> None:
    if isinstance(db, AsyncSession) and job in db:
        db.expunge(job)
    now = datetime.now(timezone.utc)
    # revision 守卫（#6）：embedding 期间文档可能被另一事务更新/删除。**必须读新鲜行**——
    # 先 rollback 清掉本 session 的 identity-map 缓存与可能的未决事务，再按锁序
    # library FOR KEY SHARE → document FOR UPDATE 重新读取，避免 db.get 返回缓存旧 revision。
    await db.rollback()
    await db.execute(
        select(Library.id).where(Library.id == job.library_id).with_for_update(read=True, key_share=True)
    )
    doc = (await db.execute(
        select(Document).where(Document.id == job.document_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    stale = doc is None or doc.deleted_at is not None or job.document_revision != doc.current_revision
    if not stale and job.rebuild_operation_id is not None:
        library = (await db.execute(
            select(Library).where(Library.id == job.library_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        operation = (await db.execute(
            select(RebuildOperation).where(RebuildOperation.id == job.rebuild_operation_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        revision = (await db.execute(
            select(DocumentRevision).where(DocumentRevision.id == job.document_revision_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none() if job.document_revision_id is not None else None
        stale = not eligibility(job, doc, library, operation, revision)
    if stale:
        await db.execute(
            update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
                status="superseded", finished_at=now
            )
        )
        await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.embedding_job_id == job.id,
                DocumentImportJob.status == "processing",
            )
            .values(
                status="superseded",
                current_stage="embedding",
                worker_id=None,
                claimed_at=None,
                finished_at=now,
                last_error="superseded by newer revision or deleted document",
            )
        )
        await db.commit()
        await _finalize_rebuild_job(db, job.rebuild_operation_id)
        return

    # 仍是当前 revision：超 max_attempts → final failed；否则回 pending 重试
    next_status = (
        "failed"
        if (job.attempt_count or 0) >= settings.embed_worker_max_attempts
        else "pending"
    )
    await db.execute(
        update(EmbeddingJob).where(EmbeddingJob.id == job.id).values(
            status=next_status,
            last_error=reason,
            finished_at=now if next_status == "failed" else None,
            worker_id=None if next_status == "pending" else job.worker_id,
            claimed_at=None if next_status == "pending" else job.claimed_at,
        )
    )
    if next_status == "failed":
        # 只在仍是当前 revision 时标 Document failed（守卫已确认）
        await db.execute(
            update(Document).where(Document.id == job.document_id).values(
                status="failed", last_error=reason, updated_at=now
            )
        )
        # 同步更新对应父导入任务为 failed（精确关联当前 embedding_job_id 且仍为 processing）
        await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.embedding_job_id == job.id,
                DocumentImportJob.status == "processing",
            )
            .values(
                status="failed",
                current_stage="embedding",
                worker_id=None,
                claimed_at=None,
                finished_at=now,
                last_error=reason[:4000] if reason else "embedding failed at max attempts",
            )
        )
    await db.commit()
    await _finalize_rebuild_job(db, job.rebuild_operation_id)


async def run(watch: bool) -> None:
    exclude_pdf, target_library_id, min_created_at = get_worker_target_config()
    worker_id = _worker_id()
    log.info(
        "starting worker id=%s watch=%s batch_docs=%s target_library_id=%s min_created_at=%s",
        worker_id,
        watch,
        settings.embed_worker_batch_docs,
        target_library_id,
        min_created_at,
    )
    # 启动自检：embedding / Qdrant 用不了时立刻报（非 fatal）。
    from app.services import heartbeat, selfcheck
    degraded = not await selfcheck.run_startup_check("worker")
    if degraded:
        log.error("[worker] 自检失败 → 进入 degraded：暂停消费，避免把 pending 任务刷成 failed。")
    # 运行状态心跳：独立后台任务，与主循环解耦——长任务 / degraded sleep 期间照常打卡（docs/26）。
    hb_stop = asyncio.Event()
    hb_instance = heartbeat.make_instance_id()
    hb_task = asyncio.create_task(heartbeat.heartbeat_loop(
        "embedding_worker", hb_instance,
        hostname=heartbeat.HOSTNAME, pid=heartbeat.PID, started_at=heartbeat.STARTED_AT,
        stop_event=hb_stop, metadata_provider=lambda: {"watch": watch, "degraded": degraded},
    ))

    async def _stop_heartbeat() -> None:
        hb_stop.set()
        await heartbeat.beat("embedding_worker", hb_instance, hostname=heartbeat.HOSTNAME,
                             pid=heartbeat.PID, started_at=heartbeat.STARTED_AT, status="stopping")
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    last_reconcile = 0.0
    while True:
        # 周期 reconcile：即使持续有任务也按间隔收口 running operation（不只在空闲时）。
        # 靶向试用 worker 跳过全局 reconcile，由常驻 worker 处理
        if (
            target_library_id is None
            and not degraded
            and (time.monotonic() - last_reconcile) >= settings.worker_reconcile_seconds
        ):
            last_reconcile = time.monotonic()
            try:
                from app.services import rebuild as rebuild_svc
                async with async_session_factory() as rsession:
                    advanced = await rebuild_svc.reconcile_running(rsession)
                    if advanced:
                        log.info("periodic reconcile finalized %s rebuild operation(s)", advanced)
            except Exception:  # noqa: BLE001
                log.exception("periodic rebuild reconcile failed")
        # degraded：不 claim 任务，定时重测自检（embedding + Qdrant 都要好），恢复后再消费
        if degraded:
            if not watch:
                # 单次模式下依赖不可用，直接退出（cron 会下次再来）
                log.error("[worker] 依赖不可用且非 watch 模式，退出。")
                await _stop_heartbeat()
                return
            ok, msg = await selfcheck.check_consumable()
            if ok:
                log.warning("[worker] 依赖已恢复（embedding + Qdrant），退出 degraded，恢复消费。")
                degraded = False
            else:
                log.error("[worker] degraded：暂停消费，%ss 后重测（%s）",
                          settings.worker_degraded_retry_seconds, msg)
                await asyncio.sleep(settings.worker_degraded_retry_seconds)
                continue

        async with async_session_factory() as session:
            reset = await _reset_stale_jobs(
                session, target_library_id=target_library_id, min_created_at=min_created_at
            )
            if reset:
                log.warning("reset %s stale processing jobs", reset)
            jobs = await _claim_jobs(session, worker_id, settings.embed_worker_batch_docs)

        if not jobs:
            if not watch:
                # 单次模式退出前补一次 reconcile，避免遗留 running operation 卡住
                # 靶向试用 worker 同样跳过全局 reconcile
                if target_library_id is None:
                    try:
                        from app.services import rebuild as rebuild_svc
                        async with async_session_factory() as rsession:
                            await rebuild_svc.reconcile_running(rsession)
                    except Exception:  # noqa: BLE001
                        log.exception("final reconcile failed")
                log.info("no pending jobs; exiting")
                await _stop_heartbeat()
                return
            await asyncio.sleep(settings.embed_worker_poll_seconds)
            continue

        log.info("claimed %s jobs", len(jobs))
        # 每个 job 独立 session，错误不互相影响
        for job in jobs:
            async with async_session_factory() as session:
                await _process_job(session, job)


def main() -> None:
    get_worker_target_config()
    parser = argparse.ArgumentParser(description="Embedding worker (DB-queue based).")
    parser.add_argument("--watch", action="store_true", help="Long-running mode; poll when idle.")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
