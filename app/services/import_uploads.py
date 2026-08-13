from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, Settings, settings
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.library import Library
from app.models.user import User
from app.schemas.documents import ImportSessionCreate


ALLOWED_IMPORT_EXTENSIONS = (
    ".csv",
    ".docx",
    ".json",
    ".markdown",
    ".md",
    ".pdf",
    ".txt",
    ".xlsx",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ImportUploadError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def import_configuration(config: Settings = settings) -> dict:
    return {
        "max_file_bytes": config.import_staging_max_file_bytes,
        "chunk_bytes": config.import_upload_chunk_bytes,
        "max_files_per_selection": config.import_selection_max_files,
        "upload_concurrency": config.import_upload_file_concurrency,
        "allowed_extensions": list(ALLOWED_IMPORT_EXTENSIONS),
    }


def normalize_relative_path(relative_path: str | None, file_name: str) -> str | None:
    if not relative_path:
        return None
    raw = relative_path.replace("\\", "/").strip()
    if (
        not raw
        or raw.startswith("/")
        or raw.startswith("//")
        or _WINDOWS_DRIVE.match(raw)
        or "\x00" in raw
    ):
        raise ImportUploadError("invalid_relative_path", "relative path is invalid")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ImportUploadError("invalid_relative_path", "relative path is invalid")
    if any(len(part) > 255 for part in parts):
        raise ImportUploadError("invalid_relative_path", "relative path segment is too long")
    if parts[-1] != file_name:
        raise ImportUploadError(
            "relative_path_mismatch",
            "relative path must end with the uploaded file name",
        )
    return "/".join(parts)


def source_path_for_job(job: DocumentImportJob) -> str | None:
    return f"/{job.relative_path}" if job.relative_path else None


def folder_path_for_job(job: DocumentImportJob) -> str | None:
    if not job.relative_path:
        return None
    parent = PurePosixPath(job.relative_path).parent
    return None if str(parent) == "." else f"/{parent.as_posix()}"


def staging_root(config: Settings = settings) -> Path:
    configured = Path(config.import_staging_dir)
    root = configured if configured.is_absolute() else BASE_DIR / configured
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def staging_path(staging_key: str, config: Settings = settings) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}\.upload", staging_key):
        raise ImportUploadError("invalid_staging_key", "staging key is invalid")
    root = staging_root(config)
    path = (root / staging_key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ImportUploadError("invalid_staging_key", "staging key is invalid") from exc
    return path


def _validate_payload(payload: ImportSessionCreate, config: Settings) -> str | None:
    suffix = Path(payload.file_name).suffix.lower()
    if suffix not in ALLOWED_IMPORT_EXTENSIONS:
        raise ImportUploadError(
            "unsupported_file_type",
            f"unsupported file type: {suffix or '(none)'}",
            status_code=415,
        )
    if payload.size_bytes > config.import_staging_max_file_bytes:
        raise ImportUploadError(
            "file_too_large",
            f"file exceeds {config.import_staging_max_file_bytes} bytes",
            status_code=413,
        )
    return normalize_relative_path(payload.relative_path, payload.file_name)


async def create_session(
    db: AsyncSession,
    *,
    library: Library,
    user: User,
    payload: ImportSessionCreate,
    config: Settings = settings,
) -> DocumentImportJob:
    relative_path = _validate_payload(payload, config)
    existing = (
        await db.execute(
            select(DocumentImportJob).where(
                DocumentImportJob.library_id == library.id,
                DocumentImportJob.batch_id == payload.batch_id,
                DocumentImportJob.relative_path == relative_path,
                DocumentImportJob.file_name == payload.file_name,
                DocumentImportJob.size_bytes == payload.size_bytes,
                DocumentImportJob.last_modified_millis
                == payload.last_modified_millis,
                DocumentImportJob.status == "uploading",
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    batch_count = (
        await db.execute(
            select(func.count(DocumentImportJob.id)).where(
                DocumentImportJob.library_id == library.id,
                DocumentImportJob.batch_id == payload.batch_id,
                DocumentImportJob.status != "cancelled",
            )
        )
    ).scalar_one()
    if batch_count >= config.import_selection_max_files:
        raise ImportUploadError(
            "selection_file_limit",
            f"selection exceeds {config.import_selection_max_files} files",
            status_code=409,
        )

    job_id = uuid.uuid4()
    job = DocumentImportJob(
        id=job_id,
        library_id=library.id,
        requested_by_user_id=user.id,
        batch_id=payload.batch_id,
        file_name=payload.file_name,
        relative_path=relative_path,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        last_modified_millis=payload.last_modified_millis,
        staging_key=f"{job_id.hex}.upload",
        external_id=payload.external_id,
        replace_document_id=payload.replace_document_id,
        security_level=payload.security_level,
        graph_extraction_requested=payload.graph_extraction_requested,
    )
    db.add(job)
    await db.flush()
    path = staging_path(job.staging_key, config)
    await asyncio.to_thread(path.touch, exist_ok=False)
    return job


async def get_owned_job(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    job_id: uuid.UUID,
    user: User,
    for_update: bool = False,
) -> DocumentImportJob:
    stmt = select(DocumentImportJob).where(
        DocumentImportJob.id == job_id,
        DocumentImportJob.library_id == library_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None or (
        not user.is_superuser and job.requested_by_user_id != user.id
    ):
        raise ImportUploadError("job_not_found", "import job not found", status_code=404)
    return job


async def append_content(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    expected_offset: int,
    body: AsyncIterator[bytes],
    config: Settings = settings,
) -> int:
    if job.status != "uploading":
        raise ImportUploadError(
            "upload_not_active", "upload is not active", status_code=409
        )
    if expected_offset != job.upload_offset:
        raise ImportUploadError(
            "upload_offset_mismatch",
            f"expected upload offset {job.upload_offset}",
            status_code=409,
        )

    path = staging_path(job.staging_key, config)
    current_size = path.stat().st_size if path.exists() else 0
    if current_size < job.upload_offset:
        raise ImportUploadError(
            "staging_file_incomplete", "staging file is incomplete", status_code=409
        )
    if current_size != job.upload_offset:
        await asyncio.to_thread(_truncate_file, path, job.upload_offset)

    written = 0
    with path.open("ab", buffering=0) as handle:
        async for chunk in body:
            if not chunk:
                continue
            written += len(chunk)
            if written > config.import_upload_chunk_bytes:
                await asyncio.to_thread(_truncate_file, path, job.upload_offset)
                raise ImportUploadError(
                    "upload_chunk_too_large",
                    f"upload chunk exceeds {config.import_upload_chunk_bytes} bytes",
                    status_code=413,
                )
            if job.upload_offset + written > job.size_bytes:
                await asyncio.to_thread(_truncate_file, path, job.upload_offset)
                raise ImportUploadError(
                    "upload_exceeds_declared_size",
                    "upload exceeds declared file size",
                    status_code=413,
                )
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    if written == 0:
        raise ImportUploadError("empty_upload_chunk", "upload chunk is empty")
    job.upload_offset += written
    await db.flush()
    return job.upload_offset


def _truncate_file(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(size)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def complete_upload(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    config: Settings = settings,
) -> DocumentImportJob:
    if job.status == "queued":
        return job
    if job.status != "uploading" or job.upload_offset != job.size_bytes:
        raise ImportUploadError(
            "upload_incomplete", "upload is incomplete", status_code=409
        )
    path = staging_path(job.staging_key, config)
    if not path.is_file() or path.stat().st_size != job.size_bytes:
        raise ImportUploadError(
            "staging_size_mismatch", "staging file size is invalid", status_code=409
        )
    job.sha256 = await asyncio.to_thread(_sha256_file, path)
    job.status = "queued"
    job.current_stage = "queued"
    job.upload_completed_at = datetime.now(timezone.utc)
    await db.flush()
    return job


async def cancel_upload(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    config: Settings = settings,
) -> None:
    if job.status not in {"uploading", "failed"}:
        raise ImportUploadError(
            "job_not_cancellable", "import job cannot be cancelled", status_code=409
        )
    job.status = "cancelled"
    job.finished_at = datetime.now(timezone.utc)
    path = staging_path(job.staging_key, config)
    await asyncio.to_thread(path.unlink, missing_ok=True)
    await db.flush()


async def retry_job(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    config: Settings = settings,
) -> DocumentImportJob:
    if job.status != "failed":
        raise ImportUploadError(
            "job_not_retryable", "import job is not retryable", status_code=409
        )
    if job.attempt_count >= config.import_worker_max_attempts:
        raise ImportUploadError(
            "attempt_budget_exhausted",
            "import attempt budget exhausted",
            status_code=409,
        )
    job.status = "queued"
    job.current_stage = "queued"
    job.worker_id = None
    job.claimed_at = None
    job.finished_at = None
    job.last_error = None
    await db.flush()
    return job


def session_projection(job: DocumentImportJob, config: Settings = settings) -> dict:
    return {
        "id": job.id,
        "library_id": job.library_id,
        "batch_id": job.batch_id,
        "file_name": job.file_name,
        "relative_path": job.relative_path,
        "size_bytes": job.size_bytes,
        "upload_offset": job.upload_offset,
        "status": job.status,
        "current_stage": job.current_stage,
        "retry_target_type": (
            "import"
            if job.status == "failed"
            and job.current_stage != "graph"
            and job.attempt_count < config.import_worker_max_attempts
            else None
        ),
        "retry_target_id": (
            job.id
            if job.status == "failed"
            and job.current_stage != "graph"
            and job.attempt_count < config.import_worker_max_attempts
            else None
        ),
        "attempt_count": job.attempt_count,
        "last_error": job.last_error,
        "result_operation": job.result_operation,
        "document_id": job.document_id,
        "document_revision_id": job.document_revision_id,
        "embedding_job_id": job.embedding_job_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "upload_completed_at": job.upload_completed_at,
        "claimed_at": job.claimed_at,
        "finished_at": job.finished_at,
        "chunk_bytes": config.import_upload_chunk_bytes,
    }


async def _latest_graph_job(
    db: AsyncSession,
    *,
    revision_id: uuid.UUID,
) -> GraphExtractionJob | None:
    return (
        await db.execute(
            select(GraphExtractionJob)
            .where(GraphExtractionJob.document_revision_id == revision_id)
            .order_by(GraphExtractionJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def job_projection(db: AsyncSession, job: DocumentImportJob) -> dict:
    projection = session_projection(job)
    projection.pop("chunk_bytes")
    failed_graph_can_recover = (
        job.status == "failed"
        and job.current_stage == "graph"
        and job.graph_extraction_requested
        and job.document_revision_id is not None
    )
    if (
        (job.status != "processing" and not failed_graph_can_recover)
        or job.current_stage not in {"embedding", "graph"}
        or job.embedding_job_id is None
    ):
        return projection
    embedding_job = await db.get(EmbeddingJob, job.embedding_job_id)
    if embedding_job is None:
        projection.update(
            status="failed",
            last_error="embedding job is missing",
            finished_at=datetime.now(timezone.utc),
        )
        return projection
    if embedding_job.status == "failed":
        projection.update(
            status="failed",
            last_error=embedding_job.last_error or "embedding failed",
            finished_at=embedding_job.finished_at,
        )
        return projection
    if embedding_job.status in {"pending", "processing"}:
        projection["current_stage"] = "embedding"
        return projection
    if embedding_job.status == "superseded":
        projection.update(status="superseded", current_stage="completed")
        return projection
    if not job.graph_extraction_requested or job.document_revision_id is None:
        projection.update(
            status="succeeded",
            current_stage="completed",
            finished_at=embedding_job.finished_at,
        )
        return projection
    projection["current_stage"] = "graph"
    graph_job = await _latest_graph_job(db, revision_id=job.document_revision_id)
    if graph_job is None:
        # GET/list projections are read-only.  The embedder write path creates
        # the batch discovery run and document jobs after all revisions are
        # ready; a projection may only report that coordination state.
        projection["schema_discovery_state"] = "waiting_schema"
        return projection
    if graph_job.status in {"queued", "processing"}:
        projection.update(
            status="processing",
            last_error=None,
            retry_target_type=None,
            retry_target_id=None,
            finished_at=None,
        )
        return projection
    if graph_job.status == "succeeded":
        projection.update(
            status="succeeded",
            current_stage="completed",
            last_error=None,
            retry_target_type=None,
            retry_target_id=None,
            finished_at=graph_job.finished_at,
        )
    elif graph_job.status == "partially_succeeded":
        projection.update(
            status="failed",
            last_error=graph_job.error_message or "graph extraction partially succeeded",
            retry_target_type="graph",
            retry_target_id=graph_job.id,
            finished_at=graph_job.finished_at,
        )
    elif graph_job.status == "superseded":
        projection.update(
            status="superseded",
            current_stage="completed",
            retry_target_type=None,
            retry_target_id=None,
        )
    else:
        projection.update(
            status="failed",
            last_error=graph_job.error_message or "graph extraction failed",
            retry_target_type="graph" if graph_job.status == "failed" else None,
            retry_target_id=graph_job.id if graph_job.status == "failed" else None,
            finished_at=graph_job.finished_at,
        )
    return projection
