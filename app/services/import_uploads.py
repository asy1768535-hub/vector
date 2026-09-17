from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import stat
import threading
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import BASE_DIR, Settings, settings
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.file_resource import FileResource
from app.models.folder import Folder
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.user import User
from app.schemas.documents import ImportSessionCreate
from app.services import cleanup as cleanup_service
from app.services.file_resources import (
    build_storing_file_resource,
    file_resource_download_url,
    resource_id_for_upload_context,
    store_file_resource_object,
)
from app.services.import_upload_preflight import inspect_office_upload
from app.services.object_storage import build_object_storage_adapter
from app.services.object_storage_contracts import ObjectStorageError
from app.services.video_transcription import (
    AUDIO_IMPORT_EXTENSIONS,
    MEDIA_IMPORT_EXTENSIONS,
    VIDEO_IMPORT_EXTENSIONS,
)
from app.services.video_transcription import is_configured as video_transcription_configured

log = logging.getLogger(__name__)

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
    ".zip",
    *MEDIA_IMPORT_EXTENSIONS,
)
ARCHIVE_CHILD_EXTENSIONS = frozenset(ALLOWED_IMPORT_EXTENSIONS) - {".zip"}
ARCHIVE_MAX_ENTRIES = 1000
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 2 * 1024**3
ARCHIVE_MAX_COMPRESSION_RATIO = 1000
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
_PERSONAL_TASK_SCOPES = frozenset({"30d", "all"})
_PERSONAL_TASK_PAGE_LIMITS = frozenset({20, 50})
_PERSONAL_TASK_CURSOR_VERSION = "personal-import-task-v1"
_PERSONAL_TASK_CURSOR_MAX_LENGTH = 512
_PERSONAL_FILE_PAGE_SIZES = frozenset({20, 50})


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


def archive_folder_name(file_name: str) -> str:
    stem = Path(_upload_basename(file_name)).stem.strip() or "压缩包"
    return f"{stem[:249]} [ZIP]"


def _archive_relative_path(name: str, archive_folder: str) -> tuple[str, str] | None:
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw or "\x00" in raw:
        return None
    try:
        _validate_upload_text(raw, code="invalid_archive_path", label="archive path")
    except ImportUploadError:
        return None
    parts = PurePosixPath(raw).parts
    if (
        not parts
        or any(part in {"", ".", ".."} or len(part) > 255 for part in parts)
        or len(parts[-1]) > 512
    ):
        return None
    relative_path = "/".join((archive_folder, *parts))
    if len(relative_path) > 2048:
        return None
    return parts[-1], relative_path


def _validated_archive_entries(
    infos: list[zipfile.ZipInfo],
    *,
    archive_folder: str,
    max_files: int,
    max_file_bytes: int,
    allowed_extensions: frozenset[str] = ARCHIVE_CHILD_EXTENSIONS,
    video_max_file_bytes: int | None = None,
) -> tuple[list[tuple[zipfile.ZipInfo, str, str]], list[str]]:
    if len(infos) > ARCHIVE_MAX_ENTRIES:
        raise ImportUploadError(
            "archive_entry_limit",
            f"压缩包内条目数超过上限 {ARCHIVE_MAX_ENTRIES}",
            status_code=413,
        )

    planned: list[tuple[zipfile.ZipInfo, str, str]] = []
    skipped: list[str] = []
    seen_paths: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        if info.is_dir():
            continue
        file_size = max(0, int(info.file_size))
        compressed_size = max(0, int(info.compress_size))
        total_uncompressed += file_size
        if total_uncompressed > ARCHIVE_MAX_UNCOMPRESSED_BYTES:
            raise ImportUploadError(
                "archive_size_limit",
                "压缩包解压后总大小超过安全上限",
                status_code=413,
            )
        mode = (int(info.external_attr) >> 16) & 0xFFFF
        if (int(info.flag_bits) & 0x1) or stat.S_ISLNK(mode):
            raise ImportUploadError(
                "unsafe_archive",
                "压缩包包含加密文件或符号链接，无法自动解压",
                status_code=415,
            )
        if file_size and file_size / max(1, compressed_size) > ARCHIVE_MAX_COMPRESSION_RATIO:
            raise ImportUploadError(
                "archive_compression_ratio",
                "压缩包压缩率超过安全上限",
                status_code=413,
            )
        safe = _archive_relative_path(info.filename, archive_folder)
        if safe is None:
            skipped.append(info.filename)
            continue
        file_name, relative_path = safe
        path_key = relative_path.casefold()
        suffix = Path(file_name).suffix.lower()
        try:
            _validate_upload_filename(file_name)
        except ImportUploadError:
            skipped.append(info.filename)
            continue
        if (
            path_key in seen_paths
            or suffix not in allowed_extensions
            or file_size <= 0
            or file_size > max_file_bytes
            or (
                suffix in VIDEO_IMPORT_EXTENSIONS
                and video_max_file_bytes is not None
                and file_size > video_max_file_bytes
            )
        ):
            skipped.append(info.filename)
            continue
        if len(planned) >= max_files:
            raise ImportUploadError(
                "selection_file_limit",
                f"压缩包内支持文件数超过上限 {max_files}",
                status_code=413,
            )
        seen_paths.add(path_key)
        planned.append((info, file_name, relative_path))
    return planned, skipped


@dataclass(frozen=True, slots=True)
class PersonalImportTaskCursor:
    created_at: datetime
    job_id: uuid.UUID
    scope: str


@dataclass(frozen=True, slots=True)
class PersonalImportTaskPage:
    jobs: tuple[DocumentImportJob, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PersonalImportTaskFolder:
    name: str
    path: str
    file_total: int


@dataclass(frozen=True, slots=True)
class PersonalImportTaskFilePage:
    path: str
    folders: tuple[PersonalImportTaskFolder, ...]
    jobs: tuple[DocumentImportJob, ...]
    folder_total: int
    file_total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class PersonalFilePage:
    path: str
    folders: tuple[Folder, ...]
    files: tuple[Document, ...]
    folder_total: int
    file_total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class StoredFileEntry:
    file_resource_id: uuid.UUID
    document_id: uuid.UUID | None
    file_name: str
    relative_path: str | None
    content_type: str | None
    size_bytes: int
    storage_status: str
    processing_status: str
    processing_stage: str
    result_operation: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredFilePage:
    path: str
    folders: tuple[PersonalImportTaskFolder, ...]
    files: tuple[StoredFileEntry, ...]
    folder_total: int
    file_total: int
    page: int
    page_size: int


def _upload_basename(file_name: str) -> str:
    return file_name.replace("\\", "/").rsplit("/", 1)[-1]


def _validate_upload_text(value: str, *, code: str, label: str) -> None:
    """Reject path text that cannot be safely persisted or addressed.

    Do not normalize or replace invalid characters: the client must retry with
    the original file renamed.  ``unicodedata`` catches both C0/C1 controls
    (including NUL) and lone UTF-16 surrogates that can break DB/JSON layers.
    """
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs":
            raise ImportUploadError(
                code,
                f"{label} contains invalid Unicode surrogate characters",
                status_code=400,
            )
        if category == "Cc":
            reason = "NUL" if character == "\x00" else "control"
            raise ImportUploadError(
                code,
                f"{label} contains an invalid {reason} character",
                status_code=400,
            )


def _validate_upload_filename(file_name: str) -> None:
    _validate_upload_text(file_name, code="invalid_file_name", label="file name")
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
    _validate_upload_filename(file_name)
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
    video_max_file_bytes = None
    allowed_extensions = list(ALLOWED_IMPORT_EXTENSIONS)
    if video_transcription_configured(config):
        video_max_file_bytes = min(
            max_file_bytes,
            config.video_transcription_max_input_bytes,
            config.document_storage_max_read_bytes,
        )
    return {
        "max_file_bytes": max_file_bytes,
        "chunk_bytes": config.import_upload_chunk_bytes,
        "max_files_per_selection": max_files_per_selection,
        "max_configurable_file_bytes": MAX_CONFIGURABLE_FILE_BYTES,
        "max_configurable_files_per_selection": MAX_CONFIGURABLE_FILES_PER_SELECTION,
        "upload_concurrency": config.import_upload_file_concurrency,
        "doc_max_file_bytes": config.doc_conversion_max_bytes,
        "video_max_file_bytes": video_max_file_bytes,
        "allowed_extensions": allowed_extensions,
    }


def media_processing_required(
    file_name: str,
    size_bytes: int,
    config: Settings = settings,
) -> bool:
    """Whether a saved media file may enter the transcript/indexing worker.

    Storage is independent from this decision: media that exceeds a transcription
    bound remains a valid stored file instead of becoming an upload failure.
    """

    suffix = Path(file_name).suffix.lower()
    if suffix not in MEDIA_IMPORT_EXTENSIONS or not video_transcription_configured(config):
        return False
    max_bytes = min(
        config.video_transcription_max_input_bytes,
        config.document_storage_max_read_bytes,
    )
    if suffix in AUDIO_IMPORT_EXTENSIONS:
        max_bytes = min(max_bytes, config.video_transcription_max_audio_bytes)
    return size_bytes <= max_bytes


def normalize_relative_path(relative_path: str | None, file_name: str) -> str | None:
    if not relative_path:
        return None
    _validate_upload_text(relative_path, code="invalid_relative_path", label="relative path")
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


def _archive_batch_id(parent_job_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(parent_job_id, "archive-batch")


def _archive_child_job_id(parent_job_id: uuid.UUID, relative_path: str) -> uuid.UUID:
    return uuid.uuid5(parent_job_id, f"archive-entry:{relative_path.casefold()}")


def _extract_archive_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    *,
    max_file_bytes: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > max_file_bytes:
                raise ImportUploadError(
                    "archive_entry_size_limit",
                    "压缩包内文件超过单文件大小上限",
                    status_code=413,
                )
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    if written != int(info.file_size):
        raise ImportUploadError(
            "archive_entry_size_mismatch",
            "压缩包内文件大小校验失败",
            status_code=415,
        )
    return written, digest.hexdigest()


async def _persist_archive_child(
    db: AsyncSession,
    *,
    claim: UploadOperationClaim,
    archive_batch_id: uuid.UUID,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    file_name: str,
    relative_path: str,
    adapter,
    max_file_bytes: int,
    config: Settings,
) -> bool:
    child_id = _archive_child_job_id(claim.job_id, relative_path)
    child = await db.get(DocumentImportJob, child_id)
    if child is not None and child.status != "uploading":
        return child.status != "failed"
    if child is None:
        child = DocumentImportJob(
            id=child_id,
            library_id=claim.library_id,
            requested_by_user_id=claim.uploaded_by_user_id,
            batch_id=archive_batch_id,
            file_name=file_name,
            relative_path=relative_path,
            content_type=mimetypes.guess_type(file_name)[0],
            size_bytes=int(info.file_size),
            staging_key=f"{child_id.hex}.upload",
            security_level=None,
            graph_extraction_requested=False,
        )
        parent = await db.get(DocumentImportJob, claim.job_id)
        if parent is None:
            raise ImportUploadError(
                "archive_parent_missing",
                "压缩包上传任务不存在",
                status_code=409,
            )
        child.security_level = parent.security_level
        child.graph_extraction_requested = parent.graph_extraction_requested
        db.add(child)
        await db.commit()

    path = staging_path(child.staging_key, config)
    resource: FileResource | None = None
    try:
        size_bytes, sha256 = await asyncio.to_thread(
            _extract_archive_entry,
            archive,
            info,
            path,
            max_file_bytes=max_file_bytes,
        )
        resource_id = resource_id_for_upload_context(child.id)
        resource = await db.get(FileResource, resource_id)
        if resource is None:
            resource = build_storing_file_resource(
                library_id=child.library_id,
                uploaded_by_user_id=child.requested_by_user_id,
                upload_context_id=child.id,
                file_name=child.file_name,
                content_type=child.content_type,
                relative_path=child.relative_path,
                size_bytes=size_bytes,
                sha256=sha256,
                adapter=adapter,
                resource_id=resource_id,
            )
            db.add(resource)
        elif resource.storage_status != "available":
            resource.storage_status = "storing"
            resource.storage_error_code = None
        child.size_bytes = size_bytes
        child.upload_offset = size_bytes
        child.sha256 = sha256
        child.file_resource_id = resource.id
        await db.commit()

        if resource.storage_status != "available":
            prepared = await store_file_resource_object(
                adapter=adapter,
                resource=resource,
                source_path=path,
            )
            resource.storage_path = prepared.locator.object_key
            resource.storage_provider = prepared.locator.provider
            resource.endpoint_ref = prepared.locator.endpoint_ref
            resource.bucket = prepared.locator.bucket
            resource.object_key = prepared.locator.object_key
            resource.object_version = prepared.locator.object_version
            resource.etag = prepared.locator.etag
            resource.immutability_mode = prepared.locator.immutability_mode
            resource.storage_status = "available"
            resource.storage_verified_at = prepared.verified_at
            resource.storage_error_code = None

        child.upload_completed_at = datetime.now(timezone.utc)
        child.worker_id = None
        child.claimed_at = None
        child.last_error = None
        if (
            Path(child.file_name).suffix.lower() in MEDIA_IMPORT_EXTENSIONS
            and not media_processing_required(child.file_name, child.size_bytes, config)
        ):
            child.status = "succeeded"
            child.current_stage = "completed"
            child.result_operation = "stored_only"
            child.graph_extraction_requested = False
            child.finished_at = child.upload_completed_at
        await db.commit()
        return True
    except Exception as exc:
        with suppress(Exception):
            await db.rollback()
        child = await db.get(DocumentImportJob, child_id)
        if child is not None:
            child.status = "failed"
            child.current_stage = "validating"
            child.worker_id = None
            child.claimed_at = None
            child.finished_at = datetime.now(timezone.utc)
            child.last_error = (str(exc).strip() or exc.__class__.__name__)[:4000]
        if resource is not None and resource.storage_status != "available":
            resource.storage_status = "storage_failed"
            resource.storage_error_code = getattr(exc, "code", "archive_entry_failed")
        with suppress(Exception):
            await db.commit()
        log.exception("archive child import setup failed parent=%s entry=%s", claim.job_id, info.filename)
        return False
    finally:
        with suppress(OSError):
            path.unlink(missing_ok=True)


async def _expand_zip_upload(
    db: AsyncSession,
    *,
    claim: UploadOperationClaim,
    path: Path,
    adapter,
    config: Settings,
) -> int:
    if claim.library_id is None or claim.uploaded_by_user_id is None:
        raise ImportUploadError(
            "archive_identity_missing",
            "压缩包上传缺少资源身份",
            status_code=409,
        )
    library = await db.get(Library, claim.library_id)
    if library is None or library.deleted_at is not None:
        raise ImportUploadError(
            "library_unavailable",
            "知识库不可用",
            status_code=409,
        )
    max_file_bytes, max_files = effective_import_limits(library, config)
    archive_extensions = ARCHIVE_CHILD_EXTENSIONS
    archive_batch_id = _archive_batch_id(claim.job_id)
    await db.execute(
        update(DocumentImportJob)
        .where(DocumentImportJob.id == claim.job_id)
        .values(batch_id=archive_batch_id)
    )
    await db.commit()
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportUploadError(
            "invalid_archive",
            "压缩包损坏或不是有效 ZIP 文件",
            status_code=415,
        ) from exc

    with archive:
        planned, skipped = _validated_archive_entries(
            archive.infolist(),
            archive_folder=archive_folder_name(claim.file_name),
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            allowed_extensions=archive_extensions,
        )
        if not planned:
            raise ImportUploadError(
                "archive_has_no_supported_files",
                "压缩包内没有可导入的支持格式文件",
                status_code=415,
            )
        for info, file_name, relative_path in planned:
            await _persist_archive_child(
                db,
                claim=claim,
                archive_batch_id=archive_batch_id,
                archive=archive,
                info=info,
                file_name=file_name,
                relative_path=relative_path,
                adapter=adapter,
                max_file_bytes=max_file_bytes,
                config=config,
            )
        now = datetime.now(timezone.utc)
        await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.library_id == claim.library_id,
                DocumentImportJob.batch_id == archive_batch_id,
                DocumentImportJob.id != claim.job_id,
                DocumentImportJob.status == "uploading",
                DocumentImportJob.file_resource_id.is_not(None),
            )
            .values(
                status="queued",
                current_stage="queued",
                upload_completed_at=now,
            )
        )
        await db.execute(
            update(DocumentImportJob)
            .where(
                DocumentImportJob.id == claim.job_id,
                DocumentImportJob.status == "processing",
            )
            .values(
                status="succeeded",
                current_stage="completed",
                result_operation="archive_expanded",
                graph_extraction_requested=False,
                worker_id=None,
                claimed_at=None,
                finished_at=now,
                last_error=None,
            )
        )
        await db.commit()
    if skipped:
        log.info(
            "archive entries skipped parent=%s skipped=%s",
            claim.job_id,
            len(skipped),
        )
    return len(planned)


def _validate_payload(
    payload: ImportSessionCreate,
    config: Settings,
    *,
    library: Library | None = None,
) -> str | None:
    _validate_upload_filename(payload.file_name)
    if payload.replace_document_id is not None and payload.relative_path is not None:
        raise ImportUploadError(
            "replacement_relative_path_not_allowed",
            "replacement uploads cannot set a relative path",
            status_code=400,
        )
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
    if payload.replace_document_id is not None:
        target = (
            await db.execute(
                select(Document.id)
                .where(
                    Document.id == payload.replace_document_id,
                    Document.library_id == library.id,
                    Document.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().first()
        if target is None:
            raise ImportUploadError(
                "replacement_target_not_found",
                "replacement document is unavailable",
                status_code=404,
            )
    _, max_files_per_selection = effective_import_limits(library, config)
    # Keep the idempotency lookup inside the same library-scoped transaction
    # lock as the quota check, so concurrent retries cannot consume quota or
    # turn an existing upload into a spurious 429.
    await _lock_staging_quota(db, library_id=library.id)
    replacement_match = (
        DocumentImportJob.replace_document_id == payload.replace_document_id
        if payload.replace_document_id is not None
        else DocumentImportJob.replace_document_id.is_(None)
    )
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
                replacement_match,
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


async def complete_direct_upload(
    db: AsyncSession,
    *,
    library: Library,
    user: User,
    payload: ImportSessionCreate,
    content: bytes,
    config: Settings = settings,
) -> DocumentImportJob:
    """Run a one-request upload through the canonical resumable upload flow."""
    if len(content) != payload.size_bytes:
        raise ImportUploadError(
            "upload_size_mismatch",
            "uploaded content size does not match metadata",
            status_code=409,
        )
    job = await create_session(
        db,
        library=library,
        user=user,
        payload=payload,
        config=config,
    )
    await db.commit()

    offset = job.upload_offset
    while offset < len(content):
        chunk = content[offset : offset + config.import_upload_chunk_bytes]
        claim = await claim_upload_operation(
            db,
            library_id=library.id,
            job_id=job.id,
            user=user,
            operation="content",
            expected_offset=offset,
            config=config,
        )
        await db.commit()

        async def body(data=chunk):
            yield data

        offset = await append_claimed_content(
            db,
            claim=claim,
            body=body(),
            config=config,
        )

    claim = await claim_upload_operation(
        db,
        library_id=library.id,
        job_id=job.id,
        user=user,
        operation="complete",
        config=config,
    )
    await db.commit()
    if not claim.already_queued:
        await complete_claimed_upload(db, claim=claim, config=config)
    return await get_owned_job(
        db,
        library_id=library.id,
        job_id=job.id,
        user=user,
    )


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


def _personal_task_scope_cutoff(
    scope: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    if scope not in _PERSONAL_TASK_SCOPES:
        raise ImportUploadError(
            "personal_task_scope_invalid",
            "personal task scope is invalid",
            status_code=422,
        )
    if scope == "all":
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=30)


def _validate_personal_task_limit(limit: int) -> None:
    if isinstance(limit, bool) or limit not in _PERSONAL_TASK_PAGE_LIMITS:
        raise ImportUploadError(
            "personal_task_limit_invalid",
            "personal task limit is invalid",
            status_code=422,
        )


def normalize_personal_file_path(path: str | None) -> str:
    if path is None or path in {"", "/"}:
        return ""
    if not isinstance(path, str) or len(path) > 2048 or "\\" in path:
        raise ImportUploadError(
            "personal_file_path_invalid",
            "personal file path is invalid",
            status_code=422,
        )
    _validate_upload_text(
        path,
        code="personal_file_path_invalid",
        label="personal file path",
    )
    parts = path.strip("/").split("/")
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(len(part) > 255 for part in parts)
    ):
        raise ImportUploadError(
            "personal_file_path_invalid",
            "personal file path is invalid",
            status_code=422,
        )
    return "/" + "/".join(parts)


def personal_file_name(document: Document) -> str:
    name = getattr(document, "display_name", None) or getattr(document, "title", None)
    if not name and getattr(document, "source_path", None):
        name = PurePosixPath(document.source_path).name
    return str(name or "未命名文件")[:512]


async def list_personal_files(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    path: str | None,
    page: int,
    page_size: int,
) -> PersonalFilePage:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(page_size, bool)
        or page_size not in _PERSONAL_FILE_PAGE_SIZES
    ):
        raise ImportUploadError(
            "personal_file_page_invalid",
            "personal file page is invalid",
            status_code=422,
        )
    normalized_path = normalize_personal_file_path(path)
    current_folder_id: uuid.UUID | None = None
    if normalized_path:
        current_folder = (
            await db.execute(
                select(Folder).where(
                    Folder.library_id == library_id,
                    Folder.path == normalized_path,
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if current_folder is None:
            raise ImportUploadError(
                "personal_file_folder_not_found",
                "personal file folder not found",
                status_code=404,
            )
        current_folder_id = current_folder.id

    descendant = aliased(Folder)
    owned_document_in_subtree = exists(
        select(Document.id)
        .select_from(Document)
        .outerjoin(descendant, Document.folder_id == descendant.id)
        .where(
            Document.library_id == library_id,
            Document.created_by == user_id,
            Document.deleted_at.is_(None),
            Document.status == "ready",
            or_(
                Document.folder_id == Folder.id,
                and_(
                    descendant.library_id == library_id,
                    descendant.deleted_at.is_(None),
                    func.substr(
                        descendant.path,
                        1,
                        func.length(Folder.path) + 1,
                    )
                    == Folder.path.concat("/"),
                ),
            ),
        )
        .correlate(Folder)
    )
    folder_conditions = [
        Folder.library_id == library_id,
        Folder.deleted_at.is_(None),
        owned_document_in_subtree,
    ]
    folder_conditions.append(
        Folder.parent_id.is_(None)
        if current_folder_id is None
        else Folder.parent_id == current_folder_id
    )
    file_conditions = [
        Document.library_id == library_id,
        Document.created_by == user_id,
        Document.deleted_at.is_(None),
        Document.status == "ready",
        (
            Document.folder_id.is_(None)
            if current_folder_id is None
            else Document.folder_id == current_folder_id
        ),
    ]
    offset = (page - 1) * page_size
    folder_total = int(
        (
            await db.execute(
                select(func.count()).select_from(Folder).where(*folder_conditions)
            )
        ).scalar_one()
        or 0
    )
    folders = tuple(
        (
            await db.execute(
                select(Folder)
                .where(*folder_conditions)
                .order_by(func.lower(Folder.name), Folder.id)
                .limit(page_size)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    file_total = int(
        (
            await db.execute(
                select(func.count()).select_from(Document).where(*file_conditions)
            )
        ).scalar_one()
        or 0
    )
    files = tuple(
        (
            await db.execute(
                select(Document)
                .where(*file_conditions)
                .order_by(
                    func.lower(func.coalesce(Document.display_name, Document.title, "")),
                    Document.id,
                )
                .limit(page_size - len(folders))
                .offset(max(0, offset - folder_total))
            )
        )
        .scalars()
        .all()
    ) if len(folders) < page_size else ()
    return PersonalFilePage(
        path=normalized_path,
        folders=folders,
        files=files,
        folder_total=folder_total,
        file_total=file_total,
        page=page,
        page_size=page_size,
    )


async def list_stored_files(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    path: str | None,
    page: int,
    page_size: int,
) -> StoredFilePage:
    """List a user's verified original files, independent of parse success.

    Folders come from ``FileResource.relative_path`` rather than the knowledge
    catalog's ``Folder`` records, so a newly saved audio/video upload is visible
    before (or without) a Document being created.
    """

    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(page_size, bool)
        or page_size not in _PERSONAL_FILE_PAGE_SIZES
    ):
        raise ImportUploadError(
            "personal_file_page_invalid",
            "stored file page is invalid",
            status_code=422,
        )
    normalized_path = normalize_personal_file_path(path)
    stored_path = func.coalesce(FileResource.relative_path, FileResource.file_name)
    conditions = [
        DocumentImportJob.library_id == library_id,
        DocumentImportJob.requested_by_user_id == user_id,
        FileResource.library_id == library_id,
        FileResource.uploaded_by_user_id == user_id,
        FileResource.storage_status == "available",
    ]
    if normalized_path:
        prefix = normalized_path.lstrip("/") + "/"
        conditions.append(func.substr(stored_path, 1, len(prefix)) == prefix)
        remainder = func.substr(stored_path, len(prefix) + 1)
    else:
        remainder = stored_path
    separator = func.strpos(remainder, "/")
    folder_name = func.substr(remainder, 1, separator - 1)
    folder_conditions = [*conditions, separator > 0]
    file_conditions = [*conditions, separator == 0]
    offset = (page - 1) * page_size

    folder_total = int(
        (
            await db.execute(
                select(func.count(func.distinct(folder_name)))
                .select_from(DocumentImportJob)
                .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
                .where(*folder_conditions)
            )
        ).scalar_one()
        or 0
    )
    folder_rows = (
        await db.execute(
            select(
                folder_name.label("name"),
                func.count(DocumentImportJob.id).label("file_total"),
            )
            .select_from(DocumentImportJob)
            .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
            .where(*folder_conditions)
            .group_by(folder_name)
            .order_by(func.lower(folder_name))
            .limit(page_size)
            .offset(offset)
        )
    ).all()
    file_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DocumentImportJob)
                .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
                .where(*file_conditions)
            )
        ).scalar_one()
        or 0
    )
    if normalized_path and folder_total + file_total == 0:
        raise ImportUploadError(
            "personal_file_folder_not_found",
            "stored file folder not found",
            status_code=404,
        )
    rows = (
        await db.execute(
            select(DocumentImportJob, FileResource)
            .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
            .where(*file_conditions)
            .order_by(func.lower(FileResource.file_name), FileResource.id)
            .limit(max(0, page_size - len(folder_rows)))
            .offset(max(0, offset - folder_total))
        )
    ).all() if len(folder_rows) < page_size else ()
    folders = tuple(
        PersonalImportTaskFolder(
            name=str(name),
            path=f"{normalized_path}/{name}" if normalized_path else f"/{name}",
            file_total=int(file_count or 0),
        )
        for name, file_count in folder_rows
    )
    files = tuple(
        StoredFileEntry(
            file_resource_id=resource.id,
            document_id=job.document_id,
            file_name=resource.file_name,
            relative_path=resource.relative_path,
            content_type=resource.content_type,
            size_bytes=resource.size_bytes,
            storage_status=resource.storage_status,
            processing_status=job.status,
            processing_stage=job.current_stage,
            result_operation=job.result_operation,
            created_at=resource.created_at,
        )
        for job, resource in rows
    )
    return StoredFilePage(
        path=normalized_path,
        folders=folders,
        files=files,
        folder_total=folder_total,
        file_total=file_total,
        page=page,
        page_size=page_size,
    )


async def _owned_stored_file(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    file_resource_id: uuid.UUID,
    for_update: bool = False,
) -> tuple[DocumentImportJob, FileResource]:
    """Resolve exactly one original visible in the caller's net-disk view."""
    statement = (
        select(DocumentImportJob, FileResource)
        .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
        .where(
            DocumentImportJob.library_id == library_id,
            DocumentImportJob.requested_by_user_id == user_id,
            FileResource.id == file_resource_id,
            FileResource.library_id == library_id,
            FileResource.uploaded_by_user_id == user_id,
            FileResource.storage_status == "available",
        )
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise ImportUploadError(
            "stored_file_not_found", "stored file not found", status_code=404
        )
    return row


async def owned_stored_file_download_url(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    file_resource_id: uuid.UUID,
    config: Settings = settings,
) -> str:
    _, resource = await _owned_stored_file(
        db,
        user_id=user_id,
        library_id=library_id,
        file_resource_id=file_resource_id,
    )
    try:
        adapter = build_object_storage_adapter(
            config, provider=resource.storage_provider
        )
        return await file_resource_download_url(
            adapter=adapter,
            resource=resource,
            expires_seconds=config.document_storage_signed_url_seconds,
        )
    except ObjectStorageError as exc:
        raise ImportUploadError(
            "stored_file_download_unavailable",
            "stored file download is unavailable",
            status_code=503,
        ) from exc


async def request_owned_stored_file_delete(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library: Library,
    file_resource_id: uuid.UUID,
) -> None:
    """Fence a storage-only original, then let the cleanup worker delete it."""
    job, resource = await _owned_stored_file(
        db,
        user_id=user_id,
        library_id=library.id,
        file_resource_id=file_resource_id,
        for_update=True,
    )
    _ensure_storage_only_file_can_be_deleted(job)
    resource.storage_status = "deleting"
    await cleanup_service.enqueue_delete_file_resource(db, library, resource.id)


def _ensure_storage_only_file_can_be_deleted(job: DocumentImportJob) -> None:
    if job.document_id is not None:
        raise ImportUploadError(
            "stored_file_delete_document_required",
            "processed files must use the document delete flow",
            status_code=409,
        )
    if job.status not in {"succeeded", "failed", "cancelled", "superseded"}:
        raise ImportUploadError(
            "stored_file_processing_active",
            "stored file is still being processed",
            status_code=409,
        )


async def delete_personal_file_folder(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library: Library,
    path: str,
) -> tuple[int, bool]:
    normalized_path = normalize_personal_file_path(path)
    if not normalized_path:
        raise ImportUploadError(
            "personal_file_path_invalid",
            "personal file path is invalid",
            status_code=422,
        )
    folders = tuple(
        (
            await db.execute(
                select(Folder)
                .where(
                    Folder.library_id == library.id,
                    Folder.deleted_at.is_(None),
                    or_(
                        Folder.path == normalized_path,
                        func.substr(Folder.path, 1, func.length(normalized_path) + 1)
                        == normalized_path + "/",
                    ),
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    stored_path = func.coalesce(FileResource.relative_path, FileResource.file_name)
    resource_prefix = normalized_path.lstrip("/") + "/"
    storage_only_rows = tuple(
        (
            await db.execute(
                select(DocumentImportJob, FileResource)
                .join(FileResource, DocumentImportJob.file_resource_id == FileResource.id)
                .where(
                    DocumentImportJob.library_id == library.id,
                    DocumentImportJob.requested_by_user_id == user_id,
                    DocumentImportJob.document_id.is_(None),
                    FileResource.library_id == library.id,
                    FileResource.uploaded_by_user_id == user_id,
                    FileResource.storage_status == "available",
                    func.substr(stored_path, 1, len(resource_prefix)) == resource_prefix,
                )
                .with_for_update()
            )
        ).all()
    )
    has_folder = any(folder.path == normalized_path for folder in folders)
    if not has_folder and not storage_only_rows:
        raise ImportUploadError(
            "personal_file_folder_not_found",
            "personal file folder not found",
            status_code=404,
        )
    for job, _resource in storage_only_rows:
        _ensure_storage_only_file_can_be_deleted(job)

    folder_ids = [folder.id for folder in folders]
    document_ids = []
    if folder_ids:
        document_ids = list(
            (
                await db.execute(
                    select(Document.id)
                    .where(
                        Document.library_id == library.id,
                        Document.created_by == user_id,
                        Document.deleted_at.is_(None),
                        Document.folder_id.in_(folder_ids),
                    )
                    .with_for_update()
                )
            ).scalars().all()
        )
    now = datetime.now(timezone.utc)
    if document_ids:
        await db.execute(
            update(Document)
            .where(Document.id.in_(document_ids))
            .values(deleted_at=now, status="deleted", updated_at=now)
        )
        await db.execute(
            update(EmbeddingJob)
            .where(
                EmbeddingJob.library_id == library.id,
                EmbeddingJob.document_id.in_(document_ids),
                EmbeddingJob.status.in_(("pending", "processing")),
            )
            .values(status="superseded", finished_at=now)
        )
        for document_id in document_ids:
            await cleanup_service.enqueue_delete_document(db, library, document_id)

    for _job, resource in storage_only_rows:
        resource.storage_status = "deleting"
        await cleanup_service.enqueue_delete_file_resource(db, library, resource.id)

    remaining_document = None
    if folder_ids:
        remaining_document = (
            await db.execute(
                select(Document.id)
                .where(
                    Document.library_id == library.id,
                    Document.deleted_at.is_(None),
                    Document.folder_id.in_(folder_ids),
                )
                .limit(1)
            )
        ).scalars().first()
    folder_deleted = remaining_document is None
    if folder_deleted and folder_ids:
        await db.execute(
            update(Folder)
            .where(Folder.id.in_(folder_ids), Folder.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
    return len(document_ids) + len(storage_only_rows), folder_deleted


async def list_personal_import_task_files(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    scope: str,
    path: str | None,
    page: int,
    page_size: int,
    now: datetime | None = None,
) -> PersonalImportTaskFilePage:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(page_size, bool)
        or page_size not in _PERSONAL_FILE_PAGE_SIZES
    ):
        raise ImportUploadError(
            "personal_file_page_invalid",
            "personal task file page is invalid",
            status_code=422,
        )
    normalized_path = normalize_personal_file_path(path)
    cutoff = _personal_task_scope_cutoff(scope, now=now)
    task_path = func.coalesce(
        DocumentImportJob.relative_path,
        DocumentImportJob.file_name,
    )
    conditions = [
        DocumentImportJob.requested_by_user_id == user_id,
        DocumentImportJob.library_id == library_id,
    ]
    if cutoff is not None:
        conditions.append(DocumentImportJob.created_at >= cutoff)
    if normalized_path:
        prefix = normalized_path.lstrip("/") + "/"
        conditions.append(func.substr(task_path, 1, len(prefix)) == prefix)
        remainder = func.substr(task_path, len(prefix) + 1)
    else:
        remainder = task_path
    separator = func.strpos(remainder, "/")
    folder_name = func.substr(remainder, 1, separator - 1)
    folder_conditions = [*conditions, separator > 0]
    file_conditions = [*conditions, separator == 0]
    offset = (page - 1) * page_size

    folder_total = int(
        (
            await db.execute(
                select(func.count(func.distinct(folder_name)))
                .select_from(DocumentImportJob)
                .where(*folder_conditions)
            )
        ).scalar_one()
        or 0
    )
    folder_rows = (
        await db.execute(
            select(
                folder_name.label("name"),
                func.count(DocumentImportJob.id).label("file_total"),
            )
            .select_from(DocumentImportJob)
            .where(*folder_conditions)
            .group_by(folder_name)
            .order_by(func.lower(folder_name))
            .limit(page_size)
            .offset(offset)
        )
    ).all()
    file_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DocumentImportJob)
                .where(*file_conditions)
            )
        ).scalar_one()
        or 0
    )
    if normalized_path and folder_total + file_total == 0:
        raise ImportUploadError(
            "personal_file_folder_not_found",
            "personal task folder not found",
            status_code=404,
        )
    jobs = tuple(
        (
            await db.execute(
                select(DocumentImportJob)
                .where(*file_conditions)
                .order_by(
                    DocumentImportJob.created_at.desc(),
                    DocumentImportJob.id.desc(),
                )
                .limit(max(0, page_size - len(folder_rows)))
                .offset(max(0, offset - folder_total))
            )
        )
        .scalars()
        .all()
    ) if len(folder_rows) < page_size else ()
    folders = tuple(
        PersonalImportTaskFolder(
            name=str(name),
            path=f"{normalized_path}/{name}" if normalized_path else f"/{name}",
            file_total=int(file_count or 0),
        )
        for name, file_count in folder_rows
    )
    return PersonalImportTaskFilePage(
        path=normalized_path,
        folders=folders,
        jobs=jobs,
        folder_total=folder_total,
        file_total=file_total,
        page=page,
        page_size=page_size,
    )


def encode_personal_import_task_cursor(cursor: PersonalImportTaskCursor) -> str:
    if (
        not isinstance(cursor, PersonalImportTaskCursor)
        or not isinstance(cursor.created_at, datetime)
        or cursor.created_at.tzinfo is None
        or cursor.created_at.utcoffset() is None
        or not isinstance(cursor.job_id, uuid.UUID)
        or cursor.scope not in _PERSONAL_TASK_SCOPES
    ):
        raise ImportUploadError(
            "personal_task_cursor_invalid",
            "personal task cursor is invalid",
            status_code=422,
        )
    payload = {
        "created_at": cursor.created_at.astimezone(timezone.utc).isoformat(),
        "job_id": str(cursor.job_id),
        "scope": cursor.scope,
        "version": _PERSONAL_TASK_CURSOR_VERSION,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_personal_import_task_cursor(
    value: str | None,
    *,
    scope: str,
) -> PersonalImportTaskCursor | None:
    _personal_task_scope_cutoff(scope)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _PERSONAL_TASK_CURSOR_MAX_LENGTH:
        raise ImportUploadError(
            "personal_task_cursor_invalid",
            "personal task cursor is invalid",
            status_code=422,
        )
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.b64decode(
                value + padding,
                altchars=b"-_",
                validate=True,
            ).decode("ascii")
        )
        if not isinstance(payload, dict) or set(payload) != {
            "created_at",
            "job_id",
            "scope",
            "version",
        }:
            raise ValueError("cursor shape")
        created_at = datetime.fromisoformat(payload["created_at"])
        cursor = PersonalImportTaskCursor(
            created_at=created_at,
            job_id=uuid.UUID(payload["job_id"]),
            scope=payload["scope"],
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ImportUploadError(
            "personal_task_cursor_invalid",
            "personal task cursor is invalid",
            status_code=422,
        ) from exc
    if (
        payload["version"] != _PERSONAL_TASK_CURSOR_VERSION
        or cursor.scope != scope
        or encode_personal_import_task_cursor(cursor) != value
    ):
        raise ImportUploadError(
            "personal_task_cursor_invalid",
            "personal task cursor is invalid",
            status_code=422,
        )
    return cursor


async def get_personal_import_task(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    library_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    for_update: bool = False,
) -> DocumentImportJob:
    """Load one current user's import task without granting admin visibility."""
    if not library_ids:
        raise ImportUploadError("job_not_found", "import job not found", status_code=404)
    stmt = select(DocumentImportJob).where(
        DocumentImportJob.id == job_id,
        DocumentImportJob.requested_by_user_id == user_id,
        DocumentImportJob.library_id.in_(library_ids),
    )
    if for_update:
        stmt = stmt.with_for_update()
    job = (await db.execute(stmt)).scalar_one_or_none()
    # Keep this second check even though the SQL predicate is authoritative.
    # It protects alternate session adapters used by tests and maintenance tools.
    if (
        job is None
        or job.requested_by_user_id != user_id
        or job.library_id not in library_ids
    ):
        raise ImportUploadError("job_not_found", "import job not found", status_code=404)
    return job


async def list_personal_import_tasks(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    scope: str,
    limit: int,
    cursor_value: str | None = None,
    now: datetime | None = None,
) -> PersonalImportTaskPage:
    _validate_personal_task_limit(limit)
    cutoff = _personal_task_scope_cutoff(scope, now=now)
    cursor = decode_personal_import_task_cursor(cursor_value, scope=scope)
    if not library_ids:
        return PersonalImportTaskPage((), None)
    stmt = select(DocumentImportJob).where(
        DocumentImportJob.requested_by_user_id == user_id,
        DocumentImportJob.library_id.in_(library_ids),
    )
    if cutoff is not None:
        stmt = stmt.where(DocumentImportJob.created_at >= cutoff)
    if cursor is not None:
        stmt = stmt.where(
            or_(
                DocumentImportJob.created_at < cursor.created_at,
                and_(
                    DocumentImportJob.created_at == cursor.created_at,
                    DocumentImportJob.id < cursor.job_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(DocumentImportJob.created_at.desc(), DocumentImportJob.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    selected = tuple(rows[:limit])
    next_cursor = None
    if len(rows) > limit and selected:
        last = selected[-1]
        next_cursor = encode_personal_import_task_cursor(
            PersonalImportTaskCursor(last.created_at, last.id, scope)
        )
    return PersonalImportTaskPage(selected, next_cursor)


def _personal_task_summary_statement(
    *,
    user_id: uuid.UUID,
    library_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    cutoff: datetime | None,
):
    ranked_graphs = (
        select(
            GraphExtractionJob.id.label("id"),
            GraphExtractionJob.document_revision_id.label("document_revision_id"),
            GraphExtractionJob.status.label("status"),
            func.row_number()
            .over(
                partition_by=GraphExtractionJob.document_revision_id,
                order_by=(
                    GraphExtractionJob.created_at.desc(),
                    GraphExtractionJob.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(
            GraphExtractionJob.execution_mode == "production",
            GraphExtractionJob.library_id.in_(library_ids),
        )
        .cte("personal_task_ranked_graphs")
    )
    latest_graph = (
        select(
            ranked_graphs.c.id,
            ranked_graphs.c.document_revision_id,
            ranked_graphs.c.status,
        )
        .where(ranked_graphs.c.rank == 1)
        .cte("personal_task_latest_graph")
    )
    follows_downstream = and_(
        or_(
            DocumentImportJob.status == "processing",
            and_(
                DocumentImportJob.status == "failed",
                DocumentImportJob.current_stage == "graph",
                DocumentImportJob.graph_extraction_requested.is_(True),
                DocumentImportJob.document_revision_id.is_not(None),
            ),
        ),
        DocumentImportJob.current_stage.in_(("embedding", "graph")),
        DocumentImportJob.embedding_job_id.is_not(None),
    )
    graph_requested = and_(
        DocumentImportJob.graph_extraction_requested.is_(True),
        DocumentImportJob.document_revision_id.is_not(None),
    )
    effective_status = case(
        (DocumentImportJob.status.in_(("uploading", "queued")), "pending"),
        (DocumentImportJob.status == "succeeded", "succeeded"),
        (DocumentImportJob.status == "cancelled", "cancelled"),
        (DocumentImportJob.status == "superseded", "superseded"),
        (and_(follows_downstream, EmbeddingJob.id.is_(None)), "failed"),
        (and_(follows_downstream, EmbeddingJob.status == "failed"), "failed"),
        (and_(follows_downstream, EmbeddingJob.status == "pending"), "pending"),
        (and_(follows_downstream, EmbeddingJob.status == "processing"), "processing"),
        (and_(follows_downstream, EmbeddingJob.status == "superseded"), "superseded"),
        (and_(follows_downstream, EmbeddingJob.status == "done", ~graph_requested), "succeeded"),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                graph_requested,
                latest_graph.c.id.is_(None),
            ),
            "processing",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status.in_(("queued", "processing")),
            ),
            "processing",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "waiting_schema",
            ),
            "pending",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "succeeded",
            ),
            "succeeded",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "partially_succeeded",
            ),
            "failed",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "failed",
            ),
            "failed",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "cancelled",
            ),
            "failed",
        ),
        (
            and_(
                follows_downstream,
                EmbeddingJob.status == "done",
                latest_graph.c.status == "superseded",
            ),
            "superseded",
        ),
        (DocumentImportJob.status == "failed", "failed"),
        else_="processing",
    )
    statement = (
        select(effective_status.label("status"))
        .select_from(DocumentImportJob)
        .outerjoin(EmbeddingJob, DocumentImportJob.embedding_job_id == EmbeddingJob.id)
        .outerjoin(
            latest_graph,
            DocumentImportJob.document_revision_id == latest_graph.c.document_revision_id,
        )
        .where(
            DocumentImportJob.requested_by_user_id == user_id,
            DocumentImportJob.library_id.in_(library_ids),
        )
    )
    if cutoff is not None:
        statement = statement.where(DocumentImportJob.created_at >= cutoff)
    projection = statement.subquery("personal_task_status_projection")
    return select(projection.c.status, func.count().label("count")).group_by(
        projection.c.status
    )


async def personal_import_task_summary(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    scope: str,
    now: datetime | None = None,
) -> dict[str, int]:
    cutoff = _personal_task_scope_cutoff(scope, now=now)
    result = {
        "total": 0,
        "pending": 0,
        "processing": 0,
        "succeeded": 0,
        "failed": 0,
    }
    if not library_ids:
        return result
    rows = await db.execute(
        _personal_task_summary_statement(
            user_id=user_id,
            library_ids=library_ids,
            cutoff=cutoff,
        )
    )
    for status, count in rows.all():
        value = int(count or 0)
        result["total"] += value
        if status in {"pending", "processing", "succeeded", "failed"}:
            result[status] += value
    return result


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
    resource: FileResource | None = None
    try:
        _try_lock_file(
            handle,
            upload_offset=claim.upload_offset,
            retry_after_seconds=config.import_upload_retry_after_seconds,
        )
        locked = True
        async with keep_upload_claim_alive(claim, config=config) as lease:
            actual_size = os.fstat(handle.fileno()).st_size
            if actual_size != claim.size_bytes:
                raise ImportUploadError(
                    "staging_size_mismatch",
                    "staging file size is invalid",
                    status_code=409,
                    upload_offset=claim.upload_offset,
                )
            office_rejection = inspect_office_upload(
                path, claim.file_name, handle=handle
            )
            if (
                office_rejection is not None
                and office_rejection.code != "encrypted_office_file"
            ):
                preflight_completed_upload(
                    path,
                    claim.file_name,
                    handle=handle,
                )
            sha256 = await _to_thread_before_cancellation(
                _sha256_handle,
                handle,
                lease.thread_stop_event,
            )
            lease.ensure_current()

            _unlock_file(handle)
            locked = False
            if claim.library_id is None or claim.uploaded_by_user_id is None:
                raise ValueError("complete claim is missing resource identity")

            adapter = build_object_storage_adapter(config)
            resource_id = resource_id_for_upload_context(claim.job_id)
            resource = (
                await db.get(FileResource, resource_id)
                if hasattr(db, "get")
                else None
            )
            if resource is None:
                resource = build_storing_file_resource(
                    library_id=claim.library_id,
                    uploaded_by_user_id=claim.uploaded_by_user_id,
                    upload_context_id=claim.job_id,
                    file_name=claim.file_name,
                    content_type=claim.content_type,
                    relative_path=claim.relative_path,
                    size_bytes=claim.size_bytes,
                    sha256=sha256,
                    adapter=adapter,
                    resource_id=resource_id,
                )
                db.add(resource)
                await db.commit()
            else:
                if (
                    resource.library_id != claim.library_id
                    or resource.file_name != claim.file_name
                    or resource.relative_path != claim.relative_path
                    or resource.size_bytes != claim.size_bytes
                    or resource.sha256 != sha256
                ):
                    raise ObjectStorageError(
                        "file_resource_identity_conflict",
                        "file resource identity does not match upload",
                    )
                if resource.storage_status in {"deleting", "deleted"}:
                    raise ObjectStorageError(
                        "file_resource_unavailable",
                        "file resource is not writable",
                    )
                if resource.storage_status == "storage_failed":
                    resource.storage_status = "storing"
                    resource.storage_error_code = None
                    resource.storage_verified_at = None
                    await db.commit()

            prepared = await store_file_resource_object(
                adapter=adapter,
                resource=resource,
                source_path=path,
            )
            resource.storage_path = prepared.locator.object_key
            resource.storage_provider = prepared.locator.provider
            resource.endpoint_ref = prepared.locator.endpoint_ref
            resource.bucket = prepared.locator.bucket
            resource.object_key = prepared.locator.object_key
            resource.object_version = prepared.locator.object_version
            resource.etag = prepared.locator.etag
            resource.immutability_mode = prepared.locator.immutability_mode
            resource.storage_status = "available"
            resource.storage_verified_at = prepared.verified_at
            resource.storage_error_code = None

            await lease.stop_renewal()
            lease.ensure_current()
            stored_only = (
                Path(claim.file_name).suffix.lower() in MEDIA_IMPORT_EXTENSIONS
                and not media_processing_required(
                    claim.file_name, claim.size_bytes, config
                )
            )
            completion_values = {
                "sha256": sha256,
                "file_resource_id": resource.id,
                "status": "succeeded" if stored_only else "queued",
                "current_stage": "completed" if stored_only else "queued",
                "upload_completed_at": datetime.now(timezone.utc),
                "worker_id": None,
                "claimed_at": None,
            }
            if stored_only:
                completion_values.update(
                    result_operation="stored_only",
                    graph_extraction_requested=False,
                    finished_at=datetime.now(timezone.utc),
                    last_error=None,
                )
            result = await db.execute(
                update(DocumentImportJob)
                .where(
                    DocumentImportJob.id == claim.job_id,
                    DocumentImportJob.status == "uploading",
                    DocumentImportJob.worker_id == claim.owner_token,
                    DocumentImportJob.upload_offset == claim.size_bytes,
                )
                .values(**completion_values)
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
    except ObjectStorageError as exc:
        if resource is not None and resource.storage_status != "available":
            resource.storage_status = "storage_failed"
            resource.storage_error_code = exc.code
            with suppress(Exception):
                await db.commit()
        else:
            with suppress(Exception):
                await db.rollback()
        raise ImportUploadError(
            exc.code,
            str(exc),
            status_code=409 if exc.code.startswith("source_file") else 503,
        ) from exc
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


async def skip_duplicate_upload(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    config: Settings = settings,
) -> bool:
    """Finish a queued upload without parsing when its raw file is unchanged.

    The upload has already been fully written and hashed by this point.  We
    only compare against an active document's current successful import, so a
    historical revision or a deleted document cannot suppress a new upload.
    When graph extraction was requested, the same revision must also have a
    successful production graph job. Explicit replacement requests always
    continue through the normal importer.
    """
    if job.status != "queued" or not job.sha256 or job.replace_document_id is not None:
        return False

    job = (
        await db.execute(
            select(DocumentImportJob)
            .where(DocumentImportJob.id == job.id)
            .with_for_update()
        )
    ).scalars().first()
    if (
        job is None
        or job.status != "queued"
        or not job.sha256
        or job.replace_document_id is not None
    ):
        return False

    source_path = source_path_for_job(job)
    stmt = (
        select(DocumentImportJob, Document)
        .join(Document, Document.id == DocumentImportJob.document_id)
        .where(
            DocumentImportJob.id != job.id,
            DocumentImportJob.library_id == job.library_id,
            DocumentImportJob.sha256 == job.sha256,
            DocumentImportJob.status == "succeeded",
            DocumentImportJob.document_id.is_not(None),
            or_(
                DocumentImportJob.document_revision_id == Document.current_revision_id,
                and_(
                    DocumentImportJob.document_revision_id.is_(None),
                    Document.current_revision_id.is_(None),
                ),
            ),
            Document.deleted_at.is_(None),
        )
        .order_by(DocumentImportJob.created_at.desc())
        .limit(1)
    )
    if source_path is not None:
        # Folder uploads use the normalized relative path as the document
        # identity.  A changed file at the same path must still be imported.
        stmt = stmt.where(Document.source_path == source_path)
    elif job.external_id is not None:
        stmt = stmt.where(Document.external_id == job.external_id)
    else:
        # Match the existing text-ingest identity for single-file sessions
        # without an external id, while not collapsing folder identities.
        stmt = stmt.where(
            Document.source_path.is_(None),
            Document.external_id.is_(None),
        )

    row = (await db.execute(stmt)).first()
    if row is None:
        return False

    previous_job, document = row
    if (
        job.security_level is not None
        and job.security_level != getattr(document, "security_level", None)
    ):
        return False
    if getattr(job, "graph_extraction_requested", False):
        revision_id = (
            previous_job.document_revision_id
            or document.latest_revision_id
            or document.current_revision_id
        )
        if revision_id is None:
            return False
        graph_job = await _latest_graph_job(db, revision_id=revision_id)
        if graph_job is None or graph_job.status != "succeeded":
            # Raw-file equality is not enough when this upload also promises a
            # graph.  Do not hide a missing, failed, or partial graph behind
            # an "unchanged" import result.
            return False
    job.status = "succeeded"
    job.current_stage = "completed"
    job.result_operation = "unchanged"
    job.document_id = document.id
    job.document_revision_id = (
        previous_job.document_revision_id
        or document.latest_revision_id
        or document.current_revision_id
    )
    job.embedding_job_id = None
    job.worker_id = None
    job.claimed_at = None
    job.finished_at = datetime.now(timezone.utc)
    job.last_error = None
    await db.commit()

    if not await remove_staging_file(job.staging_key, config):
        log.warning(
            "failed to remove duplicate import staging file key=%s",
            job.staging_key,
        )
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
    if (
        Path(getattr(job, "file_name", "")).suffix.lower() == ".zip"
        or decode_upload_preflight_error(getattr(job, "last_error", None)) is not None
    ):
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
        and Path(getattr(job, "file_name", "")).suffix.lower() != ".zip"
        and job.attempt_count < config.import_worker_max_attempts
        and stored_preflight is None
    )
    file_resource = getattr(job, "file_resource", None)
    return {
        "id": job.id,
        "library_id": job.library_id,
        "batch_id": job.batch_id,
        "file_name": job.file_name,
        "relative_path": job.relative_path,
        "size_bytes": job.size_bytes,
        "file_resource_id": getattr(job, "file_resource_id", None),
        "file_status": getattr(file_resource, "storage_status", None),
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
def _attach_file_resource_projection(
    projection: dict, resource: FileResource | None
) -> dict:
    projection["file_resource_id"] = projection.get(
        "file_resource_id", resource.id if resource is not None else None
    )
    projection["file_status"] = (
        resource.storage_status
        if resource is not None
        else projection.get("file_status")
    )
    return projection



async def _latest_graph_job(
    db: AsyncSession,
    *,
    revision_id: uuid.UUID,
) -> GraphExtractionJob | None:
    return (
        await db.execute(
            select(GraphExtractionJob)
            .where(
                GraphExtractionJob.document_revision_id == revision_id,
                GraphExtractionJob.execution_mode == "production",
            )
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
    resource = None
    resource_id = getattr(job, "file_resource_id", None)
    if resource_id is not None:
        resource = await db.get(FileResource, resource_id)
    if not _requires_related_projection(job):
        return _attach_file_resource_projection(_project_job(job), resource)
    embedding_job = await db.get(EmbeddingJob, job.embedding_job_id)
    graph_job = None
    if (
        embedding_job is not None
        and embedding_job.status not in {"failed", "pending", "processing", "superseded"}
        and job.graph_extraction_requested
        and job.document_revision_id is not None
    ):
        graph_job = await _latest_graph_job(db, revision_id=job.document_revision_id)
    return _attach_file_resource_projection(
        _project_job(job, embedding_job=embedding_job, graph_job=graph_job),
        resource,
    )


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
            .where(
                GraphExtractionJob.document_revision_id.in_(graph_revisions),
                GraphExtractionJob.execution_mode == "production",
            )
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

    resource_ids = {
        getattr(job, "file_resource_id", None)
        for job in jobs
        if getattr(job, "file_resource_id", None) is not None
    }
    resource_by_id: dict[uuid.UUID, FileResource] = {}
    if resource_ids:
        resource_rows = (
            await db.execute(select(FileResource).where(FileResource.id.in_(resource_ids)))
        ).scalars().all()
        resource_by_id = {row.id: row for row in resource_rows}

    projections = []
    for job in jobs:
        projections.append(
            _attach_file_resource_projection(
                _project_job(
                    job,
                    embedding_job=embedding_by_id.get(job.embedding_job_id),
                    graph_job=graph_by_revision.get(job.document_revision_id),
                ),
                resource_by_id.get(getattr(job, "file_resource_id", None)),
            )
        )
    return projections


async def personal_task_projections(
    db: AsyncSession,
    jobs: list[DocumentImportJob] | tuple[DocumentImportJob, ...],
) -> list[dict]:
    """Return the user-safe retry state for a bounded set of root import jobs."""
    projections = [dict(row) for row in await job_projections(db, list(jobs))]
    embedding_ids = {
        job.embedding_job_id
        for job, projection in zip(jobs, projections)
        if (
            job.embedding_job_id is not None
            and projection.get("status") == "failed"
            and projection.get("current_stage") == "embedding"
        )
    }
    embedding_by_id: dict[uuid.UUID, EmbeddingJob] = {}
    if embedding_ids:
        embeddings = (
            await db.execute(select(EmbeddingJob).where(EmbeddingJob.id.in_(embedding_ids)))
        ).scalars().all()
        embedding_by_id = {embedding.id: embedding for embedding in embeddings}
    for job, projection in zip(jobs, projections):
        embedding = embedding_by_id.get(job.embedding_job_id)
        if embedding is not None and embedding.status == "failed":
            # A manual retry intentionally starts a new automatic retry budget.
            # The caller must still pass the document/version fences below.
            projection["retry_target_type"] = "embedding"
            projection["retry_target_id"] = embedding.id

    graph_ids = {
        projection["retry_target_id"]
        for projection in projections
        if projection.get("retry_target_type") == "graph"
        and isinstance(projection.get("retry_target_id"), uuid.UUID)
    }
    retryable_graph_ids: set[uuid.UUID] = set()
    if graph_ids:
        rows = await db.execute(
            select(GraphExtractionUnit.job_id)
            .where(
                GraphExtractionUnit.job_id.in_(graph_ids),
                GraphExtractionUnit.status == "failed",
                GraphExtractionUnit.retryable.is_(True),
                GraphExtractionUnit.model_attempt_count
                < settings.graph_extraction_worker_max_model_attempts,
            )
            .group_by(GraphExtractionUnit.job_id)
        )
        retryable_graph_ids = {row[0] for row in rows.all()}
    for projection in projections:
        if (
            projection.get("retry_target_type") == "graph"
            and projection.get("retry_target_id") not in retryable_graph_ids
        ):
            projection["retry_target_type"] = None
            projection["retry_target_id"] = None
    return projections


_PERSONAL_FAILURE_MESSAGES = (
    (
        ("file_too_large", "size exceeds", "too large"),
        "文件超过知识库允许的大小",
        "压缩或拆分后重新上传",
    ),
    (
        ("unsupported_type", "unsupported file", "unsupported extension"),
        "暂不支持此文件格式",
        "转换为支持的格式后上传",
    ),
    (
        ("upload_incomplete", "staging_size_mismatch", "upload_claim_lost"),
        "文件上传不完整",
        "重新选择并上传文件",
    ),
    (
        ("duplicate_file",),
        "该文件已经上传",
        "到知识资产中查看现有文件",
    ),
    (
        ("permission_denied", "organization_forbidden"),
        "没有向该知识库上传文件的权限",
        "联系知识库管理员",
    ),
    (
        (
            "metadata_file",
            "office_lock_file",
            "encrypted_office_file",
            "file_signature_mismatch",
            "file_signature_unconfirmed",
        ),
        "文件无法导入",
        "检查文件后重新选择并上传",
    ),
)


def _personal_failure_message(
    *,
    status: str,
    stage: str,
    raw_error: object,
) -> tuple[str | None, str | None]:
    if status != "failed":
        return None, None
    normalized = str(raw_error or "").casefold()
    for codes, message, action in _PERSONAL_FAILURE_MESSAGES:
        if any(code in normalized for code in codes):
            return message, action
    if stage in {"converting", "conversion_ready", "validating", "parsing", "chunking"}:
        return "文件内容解析失败", "检查文件是否损坏后重试"
    if stage == "embedding":
        return "知识内容处理失败", "稍后重试任务"
    if stage == "graph":
        return "知识图谱构建失败", "稍后重试任务"
    return "文件处理失败", "稍后重试任务"


def personal_task_projection(
    job: DocumentImportJob,
    *,
    library_name: str,
    library_slug: str,
    projection: dict,
) -> dict:
    """Drop internal job fields and turn failures into stable Chinese guidance."""
    status = str(projection.get("status") or getattr(job, "status", "processing"))
    stage = str(
        projection.get("current_stage") or getattr(job, "current_stage", "queued")
    )
    failure_message, failure_action = _personal_failure_message(
        status=status,
        stage=stage,
        raw_error=projection.get("last_error"),
    )
    retry_target_type = projection.get("retry_target_type")
    return {
        "id": job.id,
        "document_id": getattr(job, "document_id", None),
        "file_name": job.file_name,
        "relative_path": getattr(job, "relative_path", None),
        "library_id": job.library_id,
        "library_name": library_name,
        "library_slug": library_slug,
        "operation_type": "replace"
        if getattr(job, "replace_document_id", None) is not None
        else "import",
        "status": status,
        "stage": stage,
        "created_at": projection.get("created_at") or job.created_at,
        "finished_at": projection.get("finished_at"),
        "failure_message": failure_message,
        "failure_action": failure_action,
        "can_retry": bool(
            status == "failed"
            and retry_target_type in {"import", "embedding", "graph"}
        ),
    }


def _personal_task_retry_error(code: str = "job_not_retryable") -> ImportUploadError:
    return ImportUploadError(
        code,
        "personal import task is not retryable",
        status_code=409,
    )


async def _retry_personal_embedding_task(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    embedding_job_id: uuid.UUID,
) -> None:
    if job.document_id is None:
        raise _personal_task_retry_error("task_stale")
    embedding = (
        await db.execute(
            select(EmbeddingJob)
            .where(
                EmbeddingJob.id == embedding_job_id,
                EmbeddingJob.library_id == job.library_id,
                EmbeddingJob.document_id == job.document_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        embedding is None
        or embedding.document_revision_id != job.document_revision_id
        or embedding.status != "failed"
    ):
        raise _personal_task_retry_error("task_stale")
    document = (
        await db.execute(
            select(Document)
            .where(
                Document.id == job.document_id,
                Document.library_id == job.library_id,
                Document.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None or document.deleted_at is not None:
        raise _personal_task_retry_error("task_stale")
    if embedding.document_revision_id is not None:
        revision_current = document.latest_revision_id == embedding.document_revision_id
    else:
        revision_current = document.current_revision == embedding.document_revision
    if not revision_current:
        raise _personal_task_retry_error("task_stale")
    # The worker marks an embedding job failed only after its automatic budget
    # is exhausted. A user-initiated retry deliberately starts a new budget.
    embedding.status = "pending"
    embedding.attempt_count = 0
    embedding.worker_id = None
    embedding.claimed_at = None
    embedding.finished_at = None
    embedding.last_error = None
    await db.flush()


async def _retry_personal_graph_task(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    graph_job_id: uuid.UUID,
) -> None:
    if job.document_id is None or job.document_revision_id is None:
        raise _personal_task_retry_error("task_stale")
    graph_job = (
        await db.execute(
            select(GraphExtractionJob)
            .where(
                GraphExtractionJob.id == graph_job_id,
                GraphExtractionJob.library_id == job.library_id,
                GraphExtractionJob.document_id == job.document_id,
                GraphExtractionJob.document_revision_id == job.document_revision_id,
                GraphExtractionJob.execution_mode == "production",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if graph_job is None:
        raise _personal_task_retry_error("task_stale")
    document = (
        await db.execute(
            select(Document)
            .where(
                Document.id == job.document_id,
                Document.library_id == job.library_id,
                Document.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        document is None
        or document.deleted_at is not None
        or document.current_revision_id != job.document_revision_id
    ):
        raise _personal_task_retry_error("task_stale")
    latest_graph_id = (
        await db.execute(
            select(GraphExtractionJob.id)
            .where(
                GraphExtractionJob.library_id == job.library_id,
                GraphExtractionJob.document_revision_id == job.document_revision_id,
                GraphExtractionJob.execution_mode == "production",
            )
            .order_by(GraphExtractionJob.created_at.desc(), GraphExtractionJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_graph_id != graph_job.id:
        raise _personal_task_retry_error("task_stale")
    library = await db.get(Library, job.library_id)
    if library is None or library.deleted_at is not None:
        raise _personal_task_retry_error("task_stale")
    from app.services.graph_extraction_jobs import (
        GraphExtractionJobError,
        retry_graph_extraction_job,
    )

    try:
        await retry_graph_extraction_job(db, library=library, job_id=graph_job.id)
    except GraphExtractionJobError as exc:
        raise _personal_task_retry_error("job_not_retryable") from exc


async def retry_personal_import_task(
    db: AsyncSession,
    *,
    job: DocumentImportJob,
    config: Settings = settings,
) -> DocumentImportJob:
    """Retry one root import task after its current downstream fence is checked."""
    projections = await personal_task_projections(db, [job])
    projection = projections[0]
    target_type = projection.get("retry_target_type")
    target_id = projection.get("retry_target_id")
    if target_type == "import":
        await retry_job(db, job=job, config=config)
    elif target_type == "embedding" and isinstance(target_id, uuid.UUID):
        await _retry_personal_embedding_task(
            db,
            job=job,
            embedding_job_id=target_id,
        )
    elif target_type == "graph" and isinstance(target_id, uuid.UUID):
        await _retry_personal_graph_task(db, job=job, graph_job_id=target_id)
    else:
        raise _personal_task_retry_error()
    await db.flush()
    return job
