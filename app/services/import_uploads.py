from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, Settings, settings
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.library import Library
from app.models.user import User
from app.schemas.documents import ImportSessionCreate
from app.services.file_resources import (
    build_file_resource,
    prepare_file_resource,
    resource_id_for_upload_context,
)
from app.services.import_upload_preflight import inspect_office_upload
from app.services.object_storage import build_object_storage_adapter

IMAGE_IMPORT_EXTENSIONS = (
    ".bmp",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
)


ALLOWED_IMPORT_EXTENSIONS = (
    *IMAGE_IMPORT_EXTENSIONS,
    ".cfg",
    ".csv",
    ".conf",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".ini",
    ".json",
    ".log",
    ".markdown",
    ".md",
    ".pdf",
    ".pptx",
    ".rst",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

# Staging is transient. This capacity guard is deliberately separate from the
# per-file and per-selection controls that library administrators can tune.
STAGING_LIBRARY_MAX_BYTES = 2 * 1024**4
MAX_CONFIGURABLE_FILE_BYTES = 50 * 1024**3
MAX_CONFIGURABLE_FILES_PER_SELECTION = 100_000
_RETAINED_STAGING_STATUSES = ("uploading", "queued", "failed")
_PROCESSING_STAGING_STAGES = ("converting", "validating", "parsing", "chunking")
_ADVISORY_LOCK_PREFIX = "import-staging-quota:"
_UPLOAD_CLAIM_PREFIX = "upload:"
_UPLOAD_CLAIM_LOCK_NAME = "import-upload-inflight:v1"
UPLOAD_PREFLIGHT_ERROR_PREFIX = "upload_preflight:v1:"
UPLOAD_PREFLIGHT_CLEANUP_STATES = ("cleanup_pending", "cleanup_complete")
EXPIRED_UPLOAD_CLEANUP_PENDING = "staging_cleanup:v1:cleanup_pending:upload_expired"
EXPIRED_UPLOAD_CLEANUP_COMPLETE = "staging_cleanup:v1:cleanup_complete:upload_expired"
UPLOAD_PREFLIGHT_CODES = (
    "metadata_file",
    "office_lock_file",
    "encrypted_office_file",
    "file_signature_mismatch",
    "file_signature_unconfirmed",
)
_UPLOAD_PREFLIGHT_CLEANUP_STATES = frozenset(UPLOAD_PREFLIGHT_CLEANUP_STATES)
_UPLOAD_PREFLIGHT_CODES = frozenset(UPLOAD_PREFLIGHT_CODES)
_OFFICE_SUGGESTED_EXTENSIONS = frozenset({".doc", ".docx", ".xls", ".xlsx", ".pptx"})


class ImportUploadError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        upload_offset: int | None = None,
        retry_after_seconds: int | None = None,
        suggested_extension: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.upload_offset = upload_offset
        self.retry_after_seconds = retry_after_seconds
        self.suggested_extension = (
            suggested_extension
            if suggested_extension in _OFFICE_SUGGESTED_EXTENSIONS
            else None
        )


def _upload_basename(file_name: str) -> str:
    return file_name.replace("\\", "/").rsplit("/", 1)[-1]


def _validate_upload_filename(file_name: str) -> None:
    basename = _upload_basename(file_name)
    if basename.startswith("._"):
        raise ImportUploadError(
            "metadata_file",
            "macOS 元数据文件，已忽略",
            status_code=415,
        )
    if basename.startswith("~$"):
        raise ImportUploadError(
            "office_lock_file",
            "Office 临时锁文件，已忽略",
            status_code=415,
        )


def preflight_completed_upload(
    path: Path,
    file_name: str,
    *,
    handle: BinaryIO | None = None,
) -> None:
    rejection = inspect_office_upload(path, file_name, handle=handle)
    if rejection is None:
        return
    if rejection.code == "encrypted_office_file":
        message = "检测到加密的 Office 文件，请在本地解密后重新上传"
    elif rejection.code == "file_signature_mismatch":
        suggestion = rejection.suggested_extension
        if suggestion not in _OFFICE_SUGGESTED_EXTENSIONS:
            suggestion = None
        message = (
            f"文件扩展名与实际格式不一致，请改为 {suggestion} 后重新上传"
            if suggestion
            else "文件扩展名与实际格式不一致，请检查后重新上传"
        )
        raise ImportUploadError(
            rejection.code,
            message,
            status_code=415,
            suggested_extension=suggestion,
        )
    else:
        message = "无法确认文件的真实格式，请检查文件后重新上传"
    raise ImportUploadError(rejection.code, message, status_code=415)


def _encode_upload_preflight_error(
    error: ImportUploadError,
    *,
    cleanup_state: str,
) -> str:
    if cleanup_state not in _UPLOAD_PREFLIGHT_CLEANUP_STATES:
        raise ValueError("invalid upload preflight cleanup state")
    if error.code not in _UPLOAD_PREFLIGHT_CODES:
        raise ValueError("invalid upload preflight error code")
    return f"{UPLOAD_PREFLIGHT_ERROR_PREFIX}{cleanup_state}:{error.code}:{error}"


def decode_upload_preflight_error(
    value: str | None,
) -> tuple[str, ImportUploadError] | None:
    if not isinstance(value, str) or not value.startswith(UPLOAD_PREFLIGHT_ERROR_PREFIX):
        return None
    payload = value[len(UPLOAD_PREFLIGHT_ERROR_PREFIX) :]
    try:
        cleanup_state, code, message = payload.split(":", 2)
    except ValueError:
        cleanup_state, code, message = "cleanup_pending", "file_signature_unconfirmed", ""
    if cleanup_state not in _UPLOAD_PREFLIGHT_CLEANUP_STATES:
        cleanup_state = "cleanup_pending"
    if code not in _UPLOAD_PREFLIGHT_CODES:
        code = "file_signature_unconfirmed"
        message = ""
    if not message:
        message = "文件预检失败，请检查文件后重新上传"
    suggested_extension = None
    if code == "file_signature_mismatch":
        match = re.search(r"请改为 (\.[A-Za-z0-9]+) 后重新上传$", message)
        if match and match.group(1).lower() in _OFFICE_SUGGESTED_EXTENSIONS:
            suggested_extension = match.group(1).lower()
    return cleanup_state, ImportUploadError(
        code,
        message,
        status_code=415,
        suggested_extension=suggested_extension,
    )


def project_upload_error(value: str | None) -> str | None:
    stored_preflight = decode_upload_preflight_error(value)
    if stored_preflight is not None:
        return str(stored_preflight[1])
    if value in {EXPIRED_UPLOAD_CLEANUP_PENDING, EXPIRED_UPLOAD_CLEANUP_COMPLETE}:
        return "上传未完成且已过期"
    return value


@dataclass(frozen=True)
class UploadOperationClaim:
    job_id: uuid.UUID
    owner_token: str | None
    operation: str
    staging_key: str
    file_name: str
    size_bytes: int
    upload_offset: int
    already_queued: bool = False
    library_id: uuid.UUID | None = None
    uploaded_by_user_id: uuid.UUID | None = None
    relative_path: str | None = None
    content_type: str | None = None


class UploadClaimLease:
    def __init__(
        self,
        claim: UploadOperationClaim,
        *,
        retry_after_seconds: int,
    ) -> None:
        self.claim = claim
        self.retry_after_seconds = retry_after_seconds
        self._lost_error: ImportUploadError | None = None
        self.thread_stop_event = threading.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._owner_task = asyncio.current_task()

    def attach(self, task: asyncio.Task[None]) -> None:
        self._heartbeat_task = task

    def lose(self, error: Exception | None = None) -> None:
        if self._lost_error is not None:
            return
        self._lost_error = ImportUploadError(
            "upload_claim_lost",
            "upload ownership was lost; retry from the committed offset",
            status_code=409,
            upload_offset=self.claim.upload_offset,
            retry_after_seconds=self.retry_after_seconds,
        )
        self.thread_stop_event.set()
        if self._owner_task is not None:
            self._owner_task.cancel()

    def ensure_current(self) -> None:
        if self._lost_error is not None:
            raise self._lost_error

    async def stop_renewal(self) -> None:
        task = self._heartbeat_task
        if task is None:
            return
        self._heartbeat_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def effective_import_limits(
    library: Library | None,
    config: Settings = settings,
) -> tuple[int, int]:
    configured_file_bytes = (
        getattr(library, "import_max_file_bytes", None)
        or config.import_staging_max_file_bytes
    )
    configured_file_count = (
        getattr(library, "import_max_files_per_selection", None)
        or config.import_selection_max_files
    )
    return (
        min(configured_file_bytes, MAX_CONFIGURABLE_FILE_BYTES),
        min(configured_file_count, MAX_CONFIGURABLE_FILES_PER_SELECTION),
    )


def staging_reservation_bytes(file_name: str, size_bytes: int) -> int:
    return size_bytes * 2 if Path(file_name).suffix.lower() == ".doc" else size_bytes


def import_configuration(
    config: Settings = settings,
    *,
    library: Library | None = None,
) -> dict:
    max_file_bytes, max_files_per_selection = effective_import_limits(library, config)
    return {
        "max_file_bytes": max_file_bytes,
        "chunk_bytes": config.import_upload_chunk_bytes,
        "max_files_per_selection": max_files_per_selection,
        "max_configurable_file_bytes": MAX_CONFIGURABLE_FILE_BYTES,
        "max_configurable_files_per_selection": MAX_CONFIGURABLE_FILES_PER_SELECTION,
        "upload_concurrency": config.import_upload_file_concurrency,
        "doc_max_file_bytes": config.doc_conversion_max_bytes,
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


def _validate_payload(
    payload: ImportSessionCreate,
    config: Settings,
    *,
    library: Library | None = None,
) -> str | None:
    _validate_upload_filename(payload.file_name)
    suffix = Path(payload.file_name).suffix.lower()
    if suffix not in ALLOWED_IMPORT_EXTENSIONS:
        raise ImportUploadError(
            "unsupported_file_type",
            f"unsupported file type: {suffix or '(none)'}",
            status_code=415,
        )
    if suffix == ".doc" and payload.replace_document_id is not None:
        raise ImportUploadError(
            "doc_replacement_unsupported",
            "legacy DOC replacement is not supported; use a new asynchronous import",
            status_code=415,
        )
    if suffix == ".doc" and payload.size_bytes > config.doc_conversion_max_bytes:
        raise ImportUploadError(
            "doc_too_large",
            f"DOC file exceeds {config.doc_conversion_max_bytes} bytes",
            status_code=413,
        )
    max_file_bytes, _ = effective_import_limits(library, config)
    if payload.size_bytes > max_file_bytes:
        raise ImportUploadError(
            "file_too_large",
            f"file exceeds {max_file_bytes} bytes",
            status_code=413,
        )
    return normalize_relative_path(payload.relative_path, payload.file_name)


def _staging_advisory_lock_key(library_id: uuid.UUID) -> int:
    digest = hashlib.sha256(
        f"{_ADVISORY_LOCK_PREFIX}{library_id}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _database_dialect_name(db: AsyncSession) -> str | None:
    try:
        bind = db.sync_session.get_bind()
    except (AttributeError, RuntimeError):
        try:
            bind = db.get_bind()
        except (AttributeError, RuntimeError):
            return None
    return getattr(getattr(bind, "dialect", None), "name", None)


def _upload_claim_lock_key() -> int:
    digest = hashlib.sha256(_UPLOAD_CLAIM_LOCK_NAME.encode("ascii")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _lock_upload_claim_capacity(db: AsyncSession) -> None:
    dialect = _database_dialect_name(db)
    if dialect == "sqlite":
        return
    if dialect not in {None, "postgresql"}:
        raise ImportUploadError(
            "upload_capacity_unavailable",
            "upload concurrency control requires PostgreSQL",
            status_code=503,
        )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _upload_claim_lock_key()},
    )


async def _lock_staging_quota(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
) -> None:
    dialect = _database_dialect_name(db)
    # SQLite is only a DB-free/unit-test escape hatch. A missing dialect is
    # retained for lightweight fakes, which can still assert the SQL call;
    # every identified non-PostgreSQL deployment fails closed.
    if dialect == "sqlite":
        return
    if dialect not in {None, "postgresql"}:
        raise ImportUploadError(
            "staging_quota_unavailable",
            "import staging quota requires PostgreSQL",
            status_code=503,
        )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _staging_advisory_lock_key(library_id)},
    )


async def _active_staging_bytes(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
) -> int:
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            func.lower(DocumentImportJob.file_name).like("%.doc"),
                            DocumentImportJob.size_bytes * 2,
                        ),
                        else_=DocumentImportJob.size_bytes,
                    )
                ),
                0,
            )
        ).where(
            DocumentImportJob.library_id == library_id,
            or_(
                and_(
                    DocumentImportJob.status.in_(_RETAINED_STAGING_STATUSES),
                    or_(
                        DocumentImportJob.status != "failed",
                        DocumentImportJob.last_error.is_(None),
                        DocumentImportJob.last_error.not_like(
                            f"{UPLOAD_PREFLIGHT_ERROR_PREFIX}cleanup_complete:%"
                        ),
                    ),
                ),
                and_(
                    DocumentImportJob.status == "processing",
                    DocumentImportJob.current_stage.in_(_PROCESSING_STAGING_STAGES),
                ),
                and_(
                    DocumentImportJob.status == "cancelled",
                    DocumentImportJob.last_error == EXPIRED_UPLOAD_CLEANUP_PENDING,
                ),
            ),
        )
    )
    return int(result.scalar_one() or 0)


async def _check_staging_quota(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    requested_bytes: int,
    acquire_lock: bool = True,
) -> None:
    if (
        isinstance(STAGING_LIBRARY_MAX_BYTES, bool)
        or not isinstance(STAGING_LIBRARY_MAX_BYTES, int)
        or STAGING_LIBRARY_MAX_BYTES <= 0
    ):
        raise ImportUploadError(
            "staging_quota_unavailable",
            "import staging quota is unavailable",
            status_code=503,
        )
    if acquire_lock:
        await _lock_staging_quota(db, library_id=library_id)
    active_bytes = await _active_staging_bytes(db, library_id=library_id)
    if active_bytes + requested_bytes > STAGING_LIBRARY_MAX_BYTES:
        raise ImportUploadError(
            "staging_quota_exceeded",
            "import staging byte quota exceeded",
            status_code=429,
        )


async def create_session(
    db: AsyncSession,
    *,
    library: Library,
    user: User,
    payload: ImportSessionCreate,
    config: Settings = settings,
) -> DocumentImportJob:
    relative_path = _validate_payload(payload, config, library=library)
    _, max_files_per_selection = effective_import_limits(library, config)
    # Keep the idempotency lookup inside the same library-scoped transaction
    # lock as the quota check, so concurrent retries cannot consume quota or
    # turn an existing upload into a spurious 429.
    await _lock_staging_quota(db, library_id=library.id)
    existing = (
        await db.execute(
            select(DocumentImportJob).where(
                DocumentImportJob.library_id == library.id,
                DocumentImportJob.requested_by_user_id == user.id,
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

    await _check_staging_quota(
        db,
        library_id=library.id,
        requested_bytes=staging_reservation_bytes(payload.file_name, payload.size_bytes),
        acquire_lock=False,
    )

    batch_count = (
        await db.execute(
            select(func.count(DocumentImportJob.id)).where(
                DocumentImportJob.library_id == library.id,
                DocumentImportJob.requested_by_user_id == user.id,
                DocumentImportJob.batch_id == payload.batch_id,
                DocumentImportJob.status != "cancelled",
            )
        )
    ).scalar_one()
    if batch_count >= max_files_per_selection:
        raise ImportUploadError(
            "selection_file_limit",
            f"selection exceeds {max_files_per_selection} files",
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


def _claim_is_live(job: DocumentImportJob, *, cutoff: datetime) -> bool:
    claimed_at = getattr(job, "claimed_at", None)
    worker_id = getattr(job, "worker_id", None)
    if not worker_id or not worker_id.startswith(_UPLOAD_CLAIM_PREFIX):
        return False
    if claimed_at is None:
        return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return claimed_at >= cutoff


async def claim_upload_operation(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    job_id: uuid.UUID,
    user: User,
    operation: str,
    expected_offset: int | None = None,
    config: Settings = settings,
) -> UploadOperationClaim:
    if operation not in {"content", "complete"}:
        raise ValueError("unsupported upload operation")

    # One transaction-wide lock makes count + claim atomic across API processes.
    await _lock_upload_claim_capacity(db)
    job = await get_owned_job(
        db,
        library_id=library_id,
        job_id=job_id,
        user=user,
        for_update=True,
    )
    stored_preflight = decode_upload_preflight_error(getattr(job, "last_error", None))
    if operation == "complete" and job.status == "failed" and stored_preflight:
        raise stored_preflight[1]
    if operation == "complete" and job.status in {
        "queued",
        "processing",
        "succeeded",
        "failed",
        "superseded",
    }:
        return UploadOperationClaim(
            job_id=job.id,
            owner_token=None,
            operation=operation,
            staging_key=job.staging_key,
            file_name=job.file_name,
            size_bytes=job.size_bytes,
            upload_offset=job.upload_offset,
            already_queued=True,
            library_id=job.library_id,
            uploaded_by_user_id=job.requested_by_user_id,
            relative_path=job.relative_path,
            content_type=job.content_type,
        )
    if job.status != "uploading":
        raise ImportUploadError(
            "upload_not_active",
            "upload is not active",
            status_code=409,
            upload_offset=job.upload_offset,
        )
    if operation == "content" and expected_offset != job.upload_offset:
        raise ImportUploadError(
            "upload_offset_mismatch",
            f"expected upload offset {job.upload_offset}",
            status_code=409,
            upload_offset=job.upload_offset,
        )
    if operation == "complete" and job.upload_offset != job.size_bytes:
        raise ImportUploadError(
            "upload_incomplete",
            "upload is incomplete",
            status_code=409,
            upload_offset=job.upload_offset,
        )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=config.import_upload_claim_stale_seconds)
    if _claim_is_live(job, cutoff=cutoff):
        raise ImportUploadError(
            "upload_busy",
            "another upload request owns this session",
            status_code=409,
            upload_offset=job.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )

    actor_prefix = f"{_UPLOAD_CLAIM_PREFIX}{user.id.hex}:"
    counts = await db.execute(
        select(
            func.count(DocumentImportJob.id),
            func.coalesce(
                func.sum(
                    case(
                        (DocumentImportJob.worker_id.like(f"{actor_prefix}%"), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(
            DocumentImportJob.status == "uploading",
            DocumentImportJob.worker_id.like(f"{_UPLOAD_CLAIM_PREFIX}%"),
            DocumentImportJob.claimed_at >= cutoff,
        )
    )
    global_inflight, user_inflight = counts.one()
    if int(global_inflight or 0) >= config.import_upload_global_inflight_limit:
        raise ImportUploadError(
            "upload_global_limit",
            "upload service is at its in-flight limit",
            status_code=429,
            upload_offset=job.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )
    if int(user_inflight or 0) >= config.import_upload_user_inflight_limit:
        raise ImportUploadError(
            "upload_user_limit",
            "this user already has an upload request in flight",
            status_code=429,
            upload_offset=job.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )

    owner_token = f"{actor_prefix}{operation}:{uuid.uuid4().hex}"
    job.worker_id = owner_token
    job.claimed_at = now
    await db.flush()
    return UploadOperationClaim(
        job_id=job.id,
        owner_token=owner_token,
        operation=operation,
        staging_key=job.staging_key,
        file_name=job.file_name,
        size_bytes=job.size_bytes,
        upload_offset=job.upload_offset,
        library_id=job.library_id,
        uploaded_by_user_id=job.requested_by_user_id,
        relative_path=job.relative_path,
        content_type=job.content_type,
    )


async def _renew_upload_claim(claim: UploadOperationClaim) -> bool:
    if claim.owner_token is None:
        return False
    from app.db import async_session_factory

    async with async_session_factory() as db:
        result = await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == claim.job_id,
                DocumentImportJob.status == "uploading",
                DocumentImportJob.worker_id == claim.owner_token,
            )
            .values(claimed_at=datetime.now(timezone.utc))
        )
        await db.commit()
        return (result.rowcount or 0) == 1


@asynccontextmanager
async def keep_upload_claim_alive(
    claim: UploadOperationClaim,
    *,
    config: Settings = settings,
):
    lease = UploadClaimLease(
        claim,
        retry_after_seconds=config.import_upload_retry_after_seconds,
    )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(config.import_upload_claim_heartbeat_seconds)
            try:
                renewed = await _renew_upload_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lease.lose(exc)
                return
            if not renewed:
                lease.lose()
                return

    task = asyncio.create_task(heartbeat())
    lease.attach(task)
    try:
        yield lease
    finally:
        await lease.stop_renewal()


async def release_upload_claim(
    db: AsyncSession,
    claim: UploadOperationClaim,
) -> None:
    if claim.owner_token is None:
        return
    with suppress(Exception):
        await db.rollback()
    try:
        await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == claim.job_id,
                DocumentImportJob.status == "uploading",
                DocumentImportJob.worker_id == claim.owner_token,
            )
            .values(worker_id=None, claimed_at=None)
        )
        await db.commit()
    except Exception:
        with suppress(Exception):
            await db.rollback()


def _try_lock_file(
    handle: BinaryIO,
    *,
    upload_offset: int = 0,
    retry_after_seconds: int | None = None,
) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            position = handle.tell()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            finally:
                handle.seek(position)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ImportUploadError(
            "upload_busy",
            "another upload request is using this staging file",
            status_code=409,
            upload_offset=upload_offset,
            retry_after_seconds=(
                retry_after_seconds or settings.import_upload_retry_after_seconds
            ),
        ) from exc


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        position = handle.tell()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.seek(position)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def _to_thread_before_cancellation(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await task
        raise


async def _durably_truncate(handle: BinaryIO, size: int) -> None:
    handle.truncate(size)
    handle.flush()
    await _to_thread_before_cancellation(os.fsync, handle.fileno())


async def append_claimed_content(
    db: AsyncSession,
    *,
    claim: UploadOperationClaim,
    body: AsyncIterator[bytes],
    config: Settings = settings,
) -> int:
    if claim.owner_token is None or claim.operation != "content":
        raise ValueError("an active content claim is required")

    try:
        path = staging_path(claim.staging_key, config)
        handle = path.open("a+b", buffering=0)
    except BaseException:
        await release_upload_claim(db, claim)
        raise
    locked = False
    finalization_started = False
    lease: UploadClaimLease | None = None
    try:
        _try_lock_file(
            handle,
            upload_offset=claim.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )
        locked = True
        async with keep_upload_claim_alive(claim, config=config) as lease:
            handle.seek(0, os.SEEK_END)
            current_size = handle.tell()
            if current_size < claim.upload_offset:
                raise ImportUploadError(
                    "staging_file_incomplete",
                    "staging file is incomplete",
                    status_code=409,
                    upload_offset=claim.upload_offset,
                )
            if current_size != claim.upload_offset:
                await _durably_truncate(handle, claim.upload_offset)
            handle.seek(0, os.SEEK_END)

            written = 0
            try:
                async for chunk in body:
                    lease.ensure_current()
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > config.import_upload_chunk_bytes:
                        raise ImportUploadError(
                            "upload_chunk_too_large",
                            f"upload chunk exceeds {config.import_upload_chunk_bytes} bytes",
                            status_code=413,
                            upload_offset=claim.upload_offset,
                        )
                    if claim.upload_offset + written > claim.size_bytes:
                        raise ImportUploadError(
                            "upload_exceeds_declared_size",
                            "upload exceeds declared file size",
                            status_code=413,
                            upload_offset=claim.upload_offset,
                        )
                    if handle.write(chunk) != len(chunk):
                        raise OSError("short write to staging file")
                if written == 0:
                    raise ImportUploadError(
                        "empty_upload_chunk",
                        "upload chunk is empty",
                        upload_offset=claim.upload_offset,
                    )
                handle.flush()
                await _to_thread_before_cancellation(os.fsync, handle.fileno())
                lease.ensure_current()
            except asyncio.CancelledError:
                if lease._lost_error is not None:
                    raise lease._lost_error from None
                raise

            await lease.stop_renewal()
            lease.ensure_current()
            next_offset = claim.upload_offset + written
            finalization_started = True
            result = await db.execute(
                update(DocumentImportJob)
                .where(
                    DocumentImportJob.id == claim.job_id,
                    DocumentImportJob.status == "uploading",
                    DocumentImportJob.worker_id == claim.owner_token,
                    DocumentImportJob.upload_offset == claim.upload_offset,
                )
                .values(
                    upload_offset=next_offset,
                    worker_id=None,
                    claimed_at=None,
                )
                .returning(DocumentImportJob.upload_offset)
            )
            committed_offset = result.scalar_one_or_none()
            if committed_offset is None:
                await db.rollback()
                finalization_started = False
                await _durably_truncate(handle, claim.upload_offset)
                raise ImportUploadError(
                    "upload_claim_lost",
                    "upload ownership was lost; retry from the committed offset",
                    status_code=409,
                    upload_offset=claim.upload_offset,
                    retry_after_seconds=config.import_upload_retry_after_seconds,
                )
            await db.commit()
            return int(committed_offset)
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        if locked and not finalization_started:
            with suppress(Exception):
                handle.seek(0, os.SEEK_END)
                if handle.tell() > claim.upload_offset:
                    await _durably_truncate(handle, claim.upload_offset)
        await release_upload_claim(db, claim)
        raise
    finally:
        if locked:
            with suppress(OSError):
                _unlock_file(handle)
        handle.close()


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
        await asyncio.to_thread(os.fsync, handle.fileno())
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


def _sha256_handle(
    handle: BinaryIO,
    stop_event: threading.Event | None = None,
) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(1024 * 1024):
        if stop_event is not None and stop_event.is_set():
            raise ImportUploadError(
                "upload_claim_lost",
                "upload ownership was lost during file validation",
                status_code=409,
            )
        digest.update(chunk)
    return digest.hexdigest()


async def complete_claimed_upload(
    db: AsyncSession,
    *,
    claim: UploadOperationClaim,
    config: Settings = settings,
) -> str:
    if claim.owner_token is None or claim.operation != "complete":
        raise ValueError("an active complete claim is required")

    try:
        path = staging_path(claim.staging_key, config)
        handle = path.open("r+b", buffering=0)
    except BaseException as exc:
        await release_upload_claim(db, claim)
        if isinstance(exc, OSError):
            raise ImportUploadError(
                "staging_size_mismatch",
                "staging file is missing or invalid",
                status_code=409,
                upload_offset=claim.upload_offset,
            ) from exc
        raise

    locked = False
    lease: UploadClaimLease | None = None
    sha256: str | None = None
    try:
        _try_lock_file(
            handle,
            upload_offset=claim.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )
        locked = True
        async with keep_upload_claim_alive(claim, config=config) as lease:
            try:
                actual_size = os.fstat(handle.fileno()).st_size
                if actual_size != claim.size_bytes:
                    raise ImportUploadError(
                        "staging_size_mismatch",
                        "staging file size is invalid",
                        status_code=409,
                        upload_offset=claim.upload_offset,
                    )
                sha256 = await _to_thread_before_cancellation(
                    _sha256_handle,
                    handle,
                    lease.thread_stop_event,
                )
                lease.ensure_current()
            except asyncio.CancelledError:
                if lease._lost_error is not None:
                    raise lease._lost_error from None
                raise

            # The staging handle is locked while the upload is validated. The
            # storage adapter opens the source path independently; release the
            # OS file lock before that copy so Windows does not deny the second
            # reader. The database claim still fences other upload operations.
            _unlock_file(handle)
            locked = False
            if claim.library_id is None:
                raise ValueError("complete claim is missing library identity")
            adapter = build_object_storage_adapter(config)
            prepared = await prepare_file_resource(
                adapter=adapter,
                library_id=claim.library_id,
                upload_context_id=claim.job_id,
                file_name=claim.file_name,
                content_type=claim.content_type,
                relative_path=claim.relative_path,
                source_path=path,
                expected_size_bytes=claim.size_bytes,
                expected_sha256=sha256,
            )
            resource = build_file_resource(
                prepared,
                library_id=claim.library_id,
                uploaded_by_user_id=claim.uploaded_by_user_id,
                file_name=claim.file_name,
                relative_path=claim.relative_path,
                resource_id=resource_id_for_upload_context(claim.job_id),
            )
            db.add(resource)
            await lease.stop_renewal()
            lease.ensure_current()
            result = await db.execute(
                update(DocumentImportJob)
                .where(
                    DocumentImportJob.id == claim.job_id,
                    DocumentImportJob.status == "uploading",
                    DocumentImportJob.worker_id == claim.owner_token,
                    DocumentImportJob.upload_offset == claim.size_bytes,
                )
                .values(
                    sha256=sha256,
                    file_resource_id=resource.id,
                    status="queued",
                    current_stage="queued",
                    upload_completed_at=datetime.now(timezone.utc),
                    worker_id=None,
                    claimed_at=None,
                )
                .returning(DocumentImportJob.id)
            )
            if result.scalar_one_or_none() is None:
                await db.rollback()
                raise ImportUploadError(
                    "upload_claim_lost",
                    "upload ownership was lost; retry from the committed offset",
                    status_code=409,
                    upload_offset=claim.upload_offset,
                    retry_after_seconds=config.import_upload_retry_after_seconds,
                )
            await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        await release_upload_claim(db, claim)
        raise
    finally:
        if locked:
            with suppress(OSError):
                _unlock_file(handle)
        handle.close()

    assert sha256 is not None
    return sha256


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
    if Path(job.file_name).suffix.lower() == ".doc":
        from app.services.doc_conversion import DocConversionError, validate_doc_source

        try:
            await asyncio.to_thread(
                validate_doc_source,
                path,
                max_bytes=config.doc_conversion_max_bytes,
            )
        except DocConversionError as exc:
            raise ImportUploadError(
                "invalid_doc_file",
                str(exc),
                status_code=415,
            ) from exc
    job.sha256 = await asyncio.to_thread(_sha256_file, path)
    await asyncio.to_thread(preflight_completed_upload, path, job.file_name)
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
) -> str:
    if job.status not in {"uploading", "failed"}:
        raise ImportUploadError(
            "job_not_cancellable", "import job cannot be cancelled", status_code=409
        )
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=config.import_upload_claim_stale_seconds
    )
    if job.status == "uploading" and _claim_is_live(job, cutoff=cutoff):
        raise ImportUploadError(
            "upload_busy",
            "upload request is still writing this file",
            status_code=409,
            upload_offset=job.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )
    job.status = "cancelled"
    job.worker_id = None
    job.claimed_at = None
    job.finished_at = datetime.now(timezone.utc)
    await db.flush()
    return job.staging_key


async def remove_staging_file(
    staging_key: str,
    config: Settings = settings,
) -> bool:
    """Delete a staging file only after its owning transaction has committed."""
    try:
        from app.services.doc_conversion import converted_staging_path

        paths = (staging_path(staging_key, config), converted_staging_path(staging_key, config))
        for path in paths:
            await asyncio.to_thread(path.unlink, missing_ok=True)
    except (ImportUploadError, OSError):
        return False
    return True


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
    if decode_upload_preflight_error(getattr(job, "last_error", None)) is not None:
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
    if (
        Path(getattr(job, "file_name", "")).suffix.lower() == ".doc"
        and getattr(job, "conversion_sha256", None) is None
    ):
        job.conversion_attempt_count = 0
    await db.flush()
    return job


def session_projection(job: DocumentImportJob, config: Settings = settings) -> dict:
    stored_preflight = decode_upload_preflight_error(job.last_error)
    import_retryable = (
        job.status == "failed"
        and job.current_stage != "graph"
        and job.attempt_count < config.import_worker_max_attempts
        and stored_preflight is None
    )
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
        "retry_target_type": "import" if import_retryable else None,
        "retry_target_id": job.id if import_retryable else None,
        "attempt_count": job.attempt_count,
        "last_error": project_upload_error(job.last_error),
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


def _requires_related_projection(job: DocumentImportJob) -> bool:
    failed_graph_can_recover = (
        job.status == "failed"
        and job.current_stage == "graph"
        and job.graph_extraction_requested
        and job.document_revision_id is not None
    )
    return (
        (job.status == "processing" or failed_graph_can_recover)
        and job.current_stage in {"embedding", "graph"}
        and job.embedding_job_id is not None
    )


def _project_job(
    job: DocumentImportJob,
    *,
    embedding_job: EmbeddingJob | None = None,
    graph_job: GraphExtractionJob | None = None,
) -> dict:
    projection = session_projection(job)
    projection.pop("chunk_bytes")
    if not _requires_related_projection(job):
        return projection
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
    if graph_job is None:
        # GET/list projections are read-only.  The embedder write path creates
        # the batch discovery run and document jobs after all revisions are
        # ready; a projection may only report that coordination state.
        projection["schema_discovery_state"] = "waiting_schema"
        return projection
    if graph_job.status in {"waiting_schema", "queued", "processing"}:
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


async def job_projection(db: AsyncSession, job: DocumentImportJob) -> dict:
    if not _requires_related_projection(job):
        return _project_job(job)
    embedding_job = await db.get(EmbeddingJob, job.embedding_job_id)
    graph_job = None
    if (
        embedding_job is not None
        and embedding_job.status not in {"failed", "pending", "processing", "superseded"}
        and job.graph_extraction_requested
        and job.document_revision_id is not None
    ):
        graph_job = await _latest_graph_job(db, revision_id=job.document_revision_id)
    return _project_job(job, embedding_job=embedding_job, graph_job=graph_job)


async def job_projections(
    db: AsyncSession,
    jobs: list[DocumentImportJob],
) -> list[dict]:
    """Project a bounded set of import jobs with at most three database reads."""
    related_jobs = [job for job in jobs if _requires_related_projection(job)]
    embedding_ids = {job.embedding_job_id for job in related_jobs if job.embedding_job_id is not None}
    embedding_by_id: dict[uuid.UUID, EmbeddingJob] = {}
    if embedding_ids:
        embedding_rows = (
            await db.execute(select(EmbeddingJob).where(EmbeddingJob.id.in_(embedding_ids)))
        ).scalars().all()
        embedding_by_id = {row.id: row for row in embedding_rows}

    graph_revisions = {
        job.document_revision_id
        for job in related_jobs
        if (
            job.document_revision_id is not None
            and job.graph_extraction_requested
            and (embedding := embedding_by_id.get(job.embedding_job_id)) is not None
            and embedding.status not in {"failed", "pending", "processing", "superseded"}
        )
    }
    graph_by_revision: dict[uuid.UUID, GraphExtractionJob] = {}
    if graph_revisions:
        ranked_graphs = (
            select(
                GraphExtractionJob.id.label("id"),
                func.row_number()
                .over(
                    partition_by=GraphExtractionJob.document_revision_id,
                    order_by=(GraphExtractionJob.created_at.desc(), GraphExtractionJob.id.desc()),
                )
                .label("rank"),
            )
            .where(GraphExtractionJob.document_revision_id.in_(graph_revisions))
            .subquery()
        )
        graph_rows = (
            await db.execute(
                select(GraphExtractionJob)
                .join(ranked_graphs, GraphExtractionJob.id == ranked_graphs.c.id)
                .where(ranked_graphs.c.rank == 1)
            )
        ).scalars().all()
        graph_by_revision = {row.document_revision_id: row for row in graph_rows}

    return [
        _project_job(
            job,
            embedding_job=embedding_by_id.get(job.embedding_job_id),
            graph_job=graph_by_revision.get(job.document_revision_id),
        )
        for job in jobs
    ]
