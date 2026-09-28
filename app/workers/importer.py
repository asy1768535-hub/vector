from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import shutil
import socket
import tempfile
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_file import DocumentFile
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.file_resource import FileResource
from app.models.library import Library
from app.schemas.storage import SourceLocatorV1, StorageLocatorV1
from app.services import file_resources, import_staging_cleanup, import_uploads
from app.services import folders as folders_service
from app.services import ingest as ingest_service
from app.services.doc_conversion import converted_staging_path, sha256_file
from app.services.evidence_write_path import validate_parser_segments
from app.services.import_parsing import ParsedImport, parse_import_file, rebind_converted_doc
from app.services.import_uploads import (
    folder_path_for_job,
    remove_staging_file,
    source_path_for_job,
    staging_path,
)
from app.services.object_storage import build_object_storage_adapter
from app.services.revision_files import (
    PreparedStoredFile,
    bind_prepared_file_object,
    persist_revision_file_capture,
    prepare_managed_file_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] importer[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
STAGING_CLEANUP_INTERVAL_SECONDS = 60.0
STAGING_READY_RETRY_SECONDS = (1.0, 2.0, 4.0, 8.0)
_last_staging_cleanup_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class _ParserLibrarySnapshot:
    slug: str
    chunk_size: int
    chunk_overlap: int
    ocr_enabled: bool | None
    docx_table_aware: bool | None


@dataclass(frozen=True, slots=True)
class _ImportReadSnapshot:
    library_id: uuid.UUID
    file_resource: FileResource | None
    staging_key: str
    file_name: str
    content_type: str | None
    size_bytes: int
    sha256: str | None
    conversion_sha256: str | None
    converter_version: str | None
    parser_library: _ParserLibrarySnapshot


@dataclass(frozen=True, slots=True)
class _PreparedLegacyFile:
    path: Path


@asynccontextmanager
async def _import_transaction(
    db: AsyncSession,
    staging_key: str,
    should_remove: Callable[[], bool],
):
    async with db.begin():
        yield
    if should_remove() and not await remove_staging_file(staging_key):
        log.warning("failed to remove committed import staging file key=%s", staging_key)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def _wait_for_staging_file(path: Path, expected_size: int) -> int:
    exists = False
    actual_size: int | None = None
    for delay in (0.0, *STAGING_READY_RETRY_SECONDS):
        if delay:
            await asyncio.sleep(delay)
        try:
            exists = path.is_file()
            actual_size = path.stat().st_size if exists else None
        except OSError:
            exists = False
            actual_size = None
        if exists and actual_size == expected_size:
            return actual_size
    raise RuntimeError(
        "staging file is missing or incomplete "
        f"(exists={str(exists).lower()} actual_size="
        f"{actual_size if actual_size is not None else 'missing'} "
        f"expected_size={expected_size})"
    )


def get_worker_target_config() -> tuple[bool, uuid.UUID | None, datetime | None]:
    raw_exclude = os.getenv("WORKER_EXCLUDE_PDF", "").strip().lower()
    raw_target = os.getenv("WORKER_TARGET_LIBRARY_ID", "").strip()
    raw_min_created = os.getenv("WORKER_TASK_MIN_CREATED_AT", "").strip()

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
    if target_library_id is None:
        _, target_library_id, cfg_min_created = get_worker_target_config()
        if min_created_at is None:
            min_created_at = cfg_min_created
    params: dict[str, Any] = {
        "seconds": str(settings.import_worker_stale_seconds),
        "max_attempts": settings.import_worker_max_attempts,
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
            UPDATE document_import_jobs
            SET status = 'failed', worker_id = NULL, claimed_at = NULL,
                finished_at = NOW(),
                last_error = COALESCE(last_error, 'stale import at max attempts')
            WHERE status = 'processing'
              {lib_filter}
              AND current_stage IN ('validating', 'parsing', 'chunking')
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND attempt_count >= :max_attempts
            """
        ),
        params,
    )
    reset = await db.execute(
        text(
            f"""
            UPDATE document_import_jobs
            SET status = 'queued', current_stage = 'queued',
                worker_id = NULL, claimed_at = NULL
            WHERE status = 'processing'
              {lib_filter}
              AND current_stage IN ('validating', 'parsing', 'chunking')
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:seconds || ' seconds')::interval
              AND attempt_count < :max_attempts
            """
        ),
        params,
    )
    await db.commit()
    return (failed.rowcount or 0) + (reset.rowcount or 0)


async def _claim_jobs(
    db: AsyncSession, worker_id: str, limit: int
) -> list[uuid.UUID]:
    exclude_pdf, target_library_id, min_created_at = get_worker_target_config()
    params: dict[str, Any] = {
        "max_attempts": settings.import_worker_max_attempts,
        "limit": limit,
        "worker_id": worker_id,
        "exclude_pdf": exclude_pdf,
    }
    if target_library_id is not None:
        params["target_library_id"] = target_library_id
        target_clause = """
                  AND library_id = :target_library_id
                  AND LOWER(file_name) LIKE '%.pdf'
        """
        if min_created_at is not None:
            params["min_created_at"] = min_created_at
            target_clause += " AND created_at >= :min_created_at"
    else:
        target_clause = """
                  AND (
                    NOT :exclude_pdf
                    OR LOWER(file_name) NOT LIKE '%.pdf'
                  )
        """

    result = await db.execute(
        text(
            f"""
            WITH picked AS (
                SELECT id
                FROM document_import_jobs
                WHERE status = 'queued' AND attempt_count < :max_attempts
                  AND (
                    LOWER(file_name) NOT LIKE '%.doc'
                    OR conversion_sha256 IS NOT NULL
                  )
                  {target_clause}
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE document_import_jobs AS jobs
            SET status = 'processing', current_stage = 'validating',
                worker_id = :worker_id, claimed_at = NOW(),
                attempt_count = jobs.attempt_count + 1, last_error = NULL
            FROM picked
            WHERE jobs.id = picked.id
            RETURNING jobs.id
            """
        ),
        params,
    )
    await db.commit()
    return [row.id for row in result]


async def _set_stage(job_id: uuid.UUID, stage: str) -> None:
    async with async_session_factory() as db:
        job = await db.get(DocumentImportJob, job_id)
        if job is None or job.status != "processing":
            return
        job.current_stage = stage
        await db.commit()


async def _mark_failed(job_id: uuid.UUID, error: Exception) -> None:
    message = str(error).strip() or error.__class__.__name__
    async with async_session_factory() as db:
        job = await db.get(DocumentImportJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.worker_id = None
        job.claimed_at = None
        job.finished_at = datetime.now(timezone.utc)
        job.last_error = message[:4000]
        if Path(job.file_name).suffix.lower() == ".zip":
            job.current_stage = "validating"
            job.graph_extraction_requested = False
            await db.execute(
                update(DocumentImportJob)
                .where(
                    DocumentImportJob.library_id == job.library_id,
                    DocumentImportJob.batch_id == job.batch_id,
                    DocumentImportJob.id != job.id,
                    DocumentImportJob.status == "uploading",
                )
                .values(
                    status="failed",
                    current_stage="validating",
                    finished_at=job.finished_at,
                    last_error="压缩包展开未完成",
                )
            )
        await db.commit()


async def _maybe_cleanup_staging(db: AsyncSession) -> tuple[int, int]:
    global _last_staging_cleanup_monotonic
    current = time.monotonic()
    if (
        _last_staging_cleanup_monotonic is not None
        and current - _last_staging_cleanup_monotonic < STAGING_CLEANUP_INTERVAL_SECONDS
    ):
        return 0, 0
    _last_staging_cleanup_monotonic = current
    try:
        transitioned, staging_keys = await import_staging_cleanup.cleanup_staging(db)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        log.exception("import staging cleanup failed")
        return 0, 0

    try:
        expired_reconciled, expired_removed = (
            await import_staging_cleanup.reconcile_pending_expired_cleanups(
                db,
                config=settings,
            )
        )
    except Exception:  # noqa: BLE001
        await db.rollback()
        log.exception("pending expired upload cleanup reconciliation failed")
        expired_reconciled = 0
        expired_removed = 0
    try:
        reconciled, preflight_removed = (
            await import_staging_cleanup.reconcile_pending_preflight_cleanups(
                db,
                config=settings,
            )
        )
    except Exception:  # noqa: BLE001
        await db.rollback()
        log.exception("pending upload preflight cleanup reconciliation failed")
        reconciled = 0
        preflight_removed = 0
    if (
        transitioned
        or staging_keys
        or expired_reconciled
        or expired_removed
        or reconciled
        or preflight_removed
    ):
        log.info(
            "staging cleanup expired_transitioned=%s expired_removed=%s "
            "expired_attempted=%s expired_reconciled=%s "
            "preflight_reconciled=%s preflight_removed=%s",
            transitioned,
            expired_removed,
            len(staging_keys),
            expired_reconciled,
            reconciled,
            preflight_removed,
        )
    return (
        transitioned + expired_reconciled + reconciled,
        expired_removed + preflight_removed,
    )


def _text_hash(parsed: ParsedImport) -> str:
    return hashlib.sha256(parsed.normalized_text.encode("utf-8")).hexdigest()


async def _target_document(
    db: AsyncSession, job: DocumentImportJob, source_path: str | None
) -> Document | None:
    if job.replace_document_id is not None:
        stmt = select(Document).where(
            Document.id == job.replace_document_id,
            Document.library_id == job.library_id,
            Document.deleted_at.is_(None),
        )
    elif source_path is not None:
        stmt = select(Document).where(
            Document.library_id == job.library_id,
            Document.source_path == source_path,
            Document.deleted_at.is_(None),
        )
    else:
        return None
    return (await db.execute(stmt.with_for_update())).scalars().first()


def _legacy_files_root() -> Path:
    configured = Path(settings.document_files_dir)
    return configured if configured.is_absolute() else BASE_DIR / configured


async def _persist_legacy_file(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    prepared: _PreparedLegacyFile,
) -> None:
    digest = job.sha256 or ""
    suffix = Path(job.file_name).suffix.lower()
    destination = (
        _legacy_files_root()
        / str(job.library_id)
        / str(document.id)
        / str(document.current_revision)
        / f"{digest}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(prepared.path, destination)
    values = {
        "revision": document.current_revision,
        "file_name": job.file_name,
        "content_type": job.content_type,
        "storage_path": str(destination.relative_to(_legacy_files_root())),
        "size_bytes": job.size_bytes,
        "sha256": digest,
    }
    row = await db.get(DocumentFile, document.id)
    if row is None:
        db.add(DocumentFile(document_id=document.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


async def _prepare_revision_file(
    snapshot: _ImportReadSnapshot,
    source: Path,
) -> PreparedStoredFile | _PreparedLegacyFile:
    if settings.revision_file_storage_enabled:
        if snapshot.file_resource is not None:
            resource = snapshot.file_resource
            return PreparedStoredFile(
                library_id=resource.library_id,
                file_name=resource.file_name,
                content_type=resource.content_type,
                size_bytes=resource.size_bytes,
                sha256=resource.sha256,
                locator=StorageLocatorV1(
                    provider=resource.storage_provider,
                    endpoint_ref=resource.endpoint_ref,
                    bucket=resource.bucket,
                    object_key=resource.object_key,
                    object_version=resource.object_version,
                    etag=resource.etag,
                    immutability_mode=resource.immutability_mode,
                ),
                managed_snapshot=True,
                source_locator=SourceLocatorV1(kind="upload"),
                verified_at=resource.storage_verified_at,
            )
        return await prepare_managed_file_path(
            adapter=build_object_storage_adapter(),
            library_id=snapshot.library_id,
            file_name=snapshot.file_name,
            content_type=snapshot.content_type,
            source_path=source,
            expected_sha256=snapshot.sha256,
            source_locator=SourceLocatorV1(kind="upload"),
        )

    root = _legacy_files_root()
    root.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(prefix=".import-", dir=root)
    os.close(file_descriptor)
    prepared = _PreparedLegacyFile(Path(temp_name))
    try:
        await asyncio.to_thread(shutil.copyfile, source, prepared.path)
    except BaseException:
        prepared.path.unlink(missing_ok=True)
        raise
    return prepared


async def _persist_revision_file(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    prepared: PreparedStoredFile | _PreparedLegacyFile,
    revision_id: uuid.UUID | None,
    file_id: uuid.UUID | None = None,
) -> DocumentRevisionFile | None:
    if isinstance(prepared, _PreparedLegacyFile):
        await _persist_legacy_file(db, job=job, document=document, prepared=prepared)
        return None
    if revision_id is None:
        raise RuntimeError("revision file storage requires a DocumentRevision")
    return await persist_revision_file_capture(
        db,
        prepared=bind_prepared_file_object(
            prepared,
            document_id=document.id,
            document_revision_id=revision_id,
            file_id=file_id,
        ),
    )


async def _apply_import_revision_scope(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    document: Document,
    revision_id: uuid.UUID | None,
) -> None:
    if job.security_level is not None:
        document.security_level = job.security_level
    if revision_id is None:
        return
    revision = await db.get(DocumentRevision, revision_id)
    if revision is None:
        return
    if job.security_level is not None:
        revision.security_level = job.security_level
    if job.graph_extraction_requested:
        revision.parser_config = {
            **(revision.parser_config or {}),
            "graph_extraction_requested": True,
        }


async def _process_archive_job(job_id: uuid.UUID) -> None:
    resource_source: Path | None = None
    async with async_session_factory() as read_db:
        job = await read_db.get(DocumentImportJob, job_id)
        if job is None:
            return
        resource = None
        if job.file_resource_id is not None:
            resource = await read_db.get(FileResource, job.file_resource_id)
            if (
                resource is None
                or resource.library_id != job.library_id
                or resource.storage_status != "available"
            ):
                raise RuntimeError("archive file resource is unavailable")
        claim = import_uploads.UploadOperationClaim(
            job_id=job.id,
            owner_token=job.worker_id,
            operation="complete",
            staging_key=job.staging_key,
            file_name=job.file_name,
            size_bytes=job.size_bytes,
            upload_offset=job.upload_offset,
            library_id=job.library_id,
            uploaded_by_user_id=job.requested_by_user_id,
            relative_path=job.relative_path,
            content_type=job.content_type,
        )

    adapter = (
        build_object_storage_adapter(provider=resource.storage_provider)
        if resource is not None
        else build_object_storage_adapter()
    )
    if resource is None:
        source = staging_path(claim.staging_key)
        await _wait_for_staging_file(source, claim.size_bytes)
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".import-archive-", suffix=".zip")
        os.close(descriptor)
        resource_source = Path(temporary_name)
        await file_resources.materialize_file_resource(
            adapter=adapter,
            resource=resource,
            destination_path=resource_source,
        )
        source = resource_source

    try:
        async with async_session_factory() as db:
            await import_uploads._expand_zip_upload(
                db,
                claim=claim,
                path=source,
                adapter=adapter,
                config=settings,
            )
        if not await remove_staging_file(claim.staging_key):
            log.warning("failed to remove expanded archive staging file key=%s", claim.staging_key)
    finally:
        if resource_source is not None:
            resource_source.unlink(missing_ok=True)


async def _process_claimed_job(job_id: uuid.UUID) -> None:
    async with async_session_factory() as read_db:
        job = await read_db.get(DocumentImportJob, job_id)
        if job is None:
            return
        is_archive = Path(job.file_name).suffix.lower() == ".zip"
        if is_archive:
            snapshot = None
        else:
            library = await read_db.get(Library, job.library_id)
            if library is None or library.deleted_at is not None:
                raise RuntimeError("library is unavailable")
            file_resource = None
            file_resource_id = getattr(job, "file_resource_id", None)
            if file_resource_id is not None:
                file_resource = await read_db.get(FileResource, file_resource_id)
                if (
                    file_resource is None
                    or file_resource.library_id != job.library_id
                    or file_resource.storage_status != "available"
                ):
                    raise RuntimeError("file resource is unavailable")
            snapshot = _ImportReadSnapshot(
                library_id=job.library_id,
                file_resource=file_resource,
                staging_key=job.staging_key,
                file_name=job.file_name,
                content_type=job.content_type,
                size_bytes=job.size_bytes,
                sha256=job.sha256,
                conversion_sha256=getattr(job, "conversion_sha256", None),
                converter_version=getattr(job, "converter_version", None),
                parser_library=_ParserLibrarySnapshot(
                    slug=str(getattr(library, "slug", "") or ""),
                    chunk_size=library.chunk_size,
                    chunk_overlap=library.chunk_overlap,
                    ocr_enabled=library.ocr_enabled,
                    docx_table_aware=library.docx_table_aware,
                ),
            )

    if is_archive:
        await _process_archive_job(job_id)
        return
    assert snapshot is not None
    await _set_stage(job_id, "parsing")

    resource_source: Path | None = None
    if snapshot.file_resource is None:
        canonical_source = staging_path(snapshot.staging_key)
        await _wait_for_staging_file(canonical_source, snapshot.size_bytes)
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".import-resource-",
            suffix=Path(snapshot.file_name).suffix.lower(),
        )
        os.close(descriptor)
        resource_source = Path(temporary_name)
        await file_resources.materialize_file_resource(
            adapter=build_object_storage_adapter(
                provider=snapshot.file_resource.storage_provider
            ),
            resource=snapshot.file_resource,
            destination_path=resource_source,
        )
        canonical_source = resource_source
    parser_library = cast(Library, snapshot.parser_library)
    if Path(snapshot.file_name).suffix.lower() == ".doc":
        if not snapshot.conversion_sha256 or not snapshot.converter_version:
            raise RuntimeError("DOC conversion is not ready")
        parser_source = converted_staging_path(snapshot.staging_key)
        if not parser_source.is_file():
            raise RuntimeError("converted DOCX is missing")
        conversion_sha256 = await asyncio.to_thread(sha256_file, parser_source)
        if conversion_sha256 != snapshot.conversion_sha256:
            raise RuntimeError("converted DOCX hash mismatch")
        parsed = await asyncio.to_thread(parse_import_file, parser_source, parser_library)
        parsed = rebind_converted_doc(
            parsed,
            converter_version=snapshot.converter_version,
        )
    else:
        parsed = await asyncio.to_thread(
            parse_import_file,
            canonical_source,
            parser_library,
            file_name=snapshot.file_name,
        )
    validate_parser_segments(parsed.segments)
    prepared_file = await _prepare_revision_file(snapshot, canonical_source)

    remove_staging = False
    try:
        await _set_stage(job_id, "chunking")
        async with async_session_factory() as db:
            async with _import_transaction(db, snapshot.staging_key, lambda: remove_staging):
                job = (
                    await db.execute(
                        select(DocumentImportJob)
                        .where(DocumentImportJob.id == job_id)
                        .with_for_update()
                    )
                ).scalars().first()
                if job is None or job.status != "processing":
                    return
                library = await db.get(Library, job.library_id)
                if library is None or library.deleted_at is not None:
                    raise RuntimeError("library is unavailable")
                source_path = source_path_for_job(job)
                folder_id = await folders_service.ensure_folder_path(
                    db, library, folder_path_for_job(job)
                )
                target = await _target_document(db, job, source_path)
                content_hash = _text_hash(parsed)
                if (
                    target is not None
                    and job.replace_document_id is None
                    and target.content_hash == content_hash
                ):
                    target.folder_id = folder_id
                    job.status = "succeeded"
                    job.current_stage = "completed"
                    job.result_operation = "unchanged"
                    job.document_id = target.id
                    job.document_revision_id = (
                        target.latest_revision_id or target.current_revision_id
                    )
                    await _apply_import_revision_scope(
                        db,
                        job=job,
                        document=target,
                        revision_id=job.document_revision_id,
                    )
                    job.finished_at = datetime.now(timezone.utc)
                    job.worker_id = None
                    job.claimed_at = None
                    remove_staging = True
                    await db.flush()
                    return

                if target is None:
                    candidate_revision_file_id = (
                        uuid.uuid4()
                        if settings.enable_evidence_write_path
                        and settings.revision_file_storage_enabled
                        else None
                    )
                    document, embedding_job, _chunk_count, _existing = (
                        await ingest_service.ingest_text(
                            db=db,
                            library=library,
                            text=parsed.normalized_text,
                            title=job.file_name,
                            external_id=job.external_id,
                            metadata=None,
                            splitter=parsed.splitter_name,
                            created_by=job.requested_by_user_id,
                            security_level=job.security_level,
                            chunks=parsed.chunks,
                            segments=parsed.segments,
                            file_name=job.file_name,
                            raw_file_sha256=job.sha256 if candidate_revision_file_id else None,
                            document_revision_file_id=candidate_revision_file_id,
                            source_path=source_path,
                        )
                    )
                    operation = "unchanged" if _existing else "created"
                    revision_file_id = candidate_revision_file_id if not _existing else None
                else:
                    candidate_revision_file_id = (
                        uuid.uuid4()
                        if settings.enable_evidence_write_path
                        and settings.revision_file_storage_enabled
                        else None
                    )
                    embedding_job, _chunk_count, changed = (
                        await ingest_service.reingest_document(
                            db=db,
                            library=library,
                            document=target,
                            new_text=parsed.normalized_text,
                            title=job.file_name,
                            metadata=None,
                            splitter=parsed.splitter_name,
                            force=job.replace_document_id is not None,
                            chunks=parsed.chunks,
                            segments=parsed.segments,
                            file_name=job.file_name,
                            raw_file_sha256=job.sha256 if candidate_revision_file_id else None,
                            document_revision_file_id=candidate_revision_file_id,
                        )
                    )
                    document = target
                    operation = "updated" if changed else "unchanged"
                    revision_file_id = candidate_revision_file_id if changed else None
                if source_path is not None:
                    document.source_path = source_path
                document.folder_id = folder_id
                revision_id = (
                    getattr(embedding_job, "document_revision_id", None)
                    if embedding_job is not None
                    else document.latest_revision_id or document.current_revision_id
                )
                if operation != "unchanged" or isinstance(prepared_file, _PreparedLegacyFile):
                    await _persist_revision_file(
                        db,
                        job=job,
                        document=document,
                        prepared=prepared_file,
                        revision_id=revision_id,
                        file_id=revision_file_id,
                    )
                await _apply_import_revision_scope(
                    db,
                    job=job,
                    document=document,
                    revision_id=revision_id,
                )
                job.result_operation = operation
                job.document_id = document.id
                job.document_revision_id = revision_id
                job.embedding_job_id = (
                    embedding_job.id if embedding_job is not None else None
                )
                job.status = "processing" if embedding_job is not None else "succeeded"
                job.current_stage = (
                    "embedding" if embedding_job is not None else "completed"
                )
                job.worker_id = None
                job.claimed_at = None
                if embedding_job is None:
                    job.finished_at = datetime.now(timezone.utc)
                remove_staging = True
    finally:
        if isinstance(prepared_file, _PreparedLegacyFile):
            prepared_file.path.unlink(missing_ok=True)
        if resource_source is not None:
            resource_source.unlink(missing_ok=True)


async def run_once() -> int:
    worker_id = _worker_id()
    exclude_pdf, target_library_id, min_created_at = get_worker_target_config()
    async with async_session_factory() as db:
        recovered = await _reset_stale_jobs(
            db, target_library_id=target_library_id, min_created_at=min_created_at
        )
        if recovered:
            log.warning("recovered %s stale import jobs", recovered)
        if target_library_id is None:
            await _maybe_cleanup_staging(db)
        job_ids = await _claim_jobs(
            db, worker_id, max(1, settings.import_worker_batch_size)
        )
    for job_id in job_ids:
        try:
            await _process_claimed_job(job_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("import job %s failed", job_id)
            await _mark_failed(job_id, exc)
    return len(job_ids)


async def run_watch() -> None:
    while True:
        processed = await run_once()
        if not processed:
            await asyncio.sleep(settings.import_worker_poll_seconds)


def main() -> None:
    get_worker_target_config()
    parser = argparse.ArgumentParser(description="Process uploaded document imports")
    parser.add_argument("--watch", action="store_true", help="keep polling for work")
    args = parser.parse_args()
    asyncio.run(run_watch() if args.watch else run_once())

if __name__ == "__main__":
    main()
