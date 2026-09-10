from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.schemas.storage import SourceLocatorV1, StorageLocatorV1
from app.services.object_storage_contracts import ObjectStorageError

_SAFE_SUFFIX = re.compile(r"\.[a-z0-9][a-z0-9._-]{0,15}")


@dataclass(frozen=True, slots=True)
class PreparedStoredFile:
    library_id: uuid.UUID
    file_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    locator: StorageLocatorV1
    managed_snapshot: bool
    source_locator: SourceLocatorV1 | None = field(repr=False)
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedRevisionFileCapture(PreparedStoredFile):
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    file_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RevisionFileAccess:
    file_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    locator: StorageLocatorV1
    lifecycle_status: str


def _file_metadata(file_name: str, content_type: str | None) -> tuple[str, str | None]:
    if not isinstance(file_name, str) or not file_name.strip() or len(file_name) > 512:
        raise ObjectStorageError("invalid_file_metadata", "file name is invalid")
    if content_type is not None and (
        not isinstance(content_type, str)
        or not content_type.strip()
        or len(content_type) > 255
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise ObjectStorageError("invalid_file_metadata", "content type is invalid")
    return file_name.strip(), content_type.strip() if content_type else None


def _suffix(file_name: str) -> str:
    suffix = "." + file_name.rsplit(".", 1)[1].lower() if "." in file_name else ""
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""


def _managed_object_key(library_id: uuid.UUID, digest: str, file_name: str) -> str:
    return f"libraries/{library_id}/objects/{digest}{_suffix(file_name)}"


def require_storage_adapter_identity(adapter, locator: StorageLocatorV1) -> None:
    if (
        adapter.provider != locator.provider
        or adapter.endpoint_ref != locator.endpoint_ref
        or adapter.bucket != locator.bucket
    ):
        raise ObjectStorageError(
            "storage_identity_mismatch", "storage adapter and locator identity differ"
        )


async def _verify_object(
    *,
    adapter,
    locator: StorageLocatorV1,
    expected_sha256: str,
    expected_size_bytes: int,
) -> bytes:
    require_storage_adapter_identity(adapter, locator)
    stat = await adapter.stat(locator.object_key, locator.object_version)
    if stat.size_bytes != expected_size_bytes:
        raise ObjectStorageError(
            "object_verification_failed", "stored object size verification failed"
        )
    if (
        locator.object_version is not None
        and stat.object_version != locator.object_version
    ):
        raise ObjectStorageError(
            "object_verification_failed", "stored object version verification failed"
        )
    content = await adapter.read(locator.object_key, locator.object_version)
    actual_hash = hashlib.sha256(content).hexdigest()
    if len(content) != expected_size_bytes or actual_hash != expected_sha256:
        raise ObjectStorageError(
            "object_verification_failed", "stored object content verification failed"
        )
    return content


async def verify_stored_object(
    *,
    adapter,
    locator: StorageLocatorV1,
    expected_sha256: str,
    expected_size_bytes: int,
) -> bytes:
    """Verify a stored object before any database row claims its ownership."""

    return await _verify_object(
        adapter=adapter,
        locator=locator,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
    )


async def prepare_managed_file_object(
    *,
    adapter,
    library_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    content: bytes,
    source_locator: SourceLocatorV1 | None = None,
) -> PreparedStoredFile:
    file_name, content_type = _file_metadata(file_name, content_type)
    if not isinstance(content, bytes):
        raise ObjectStorageError("invalid_file_content", "file content must be bytes")
    digest = hashlib.sha256(content).hexdigest()
    object_key = _managed_object_key(library_id, digest, file_name)
    version = await adapter.put(object_key, content, content_type)
    mode = "version_id" if version.object_version else "content_hash"
    locator = StorageLocatorV1(
        provider=adapter.provider,
        endpoint_ref=adapter.endpoint_ref,
        bucket=adapter.bucket,
        object_key=object_key,
        object_version=version.object_version,
        etag=version.etag,
        immutability_mode=mode,
    )
    await _verify_object(
        adapter=adapter,
        locator=locator,
        expected_sha256=digest,
        expected_size_bytes=len(content),
    )
    return PreparedStoredFile(
        library_id=library_id,
        file_name=file_name,
        content_type=content_type,
        size_bytes=len(content),
        sha256=digest,
        locator=locator,
        managed_snapshot=True,
        source_locator=source_locator or SourceLocatorV1(kind="upload"),
        verified_at=datetime.now(timezone.utc),
    )


async def prepare_managed_file_path(
    *,
    adapter,
    library_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    source_path: Path,
    expected_sha256: str | None = None,
    source_locator: SourceLocatorV1 | None = None,
) -> PreparedStoredFile:
    file_name, content_type = _file_metadata(file_name, content_type)
    if not source_path.is_file() or source_path.is_symlink():
        raise ObjectStorageError("source_file_invalid", "source file is invalid")
    digest = hashlib.sha256()
    size_bytes = 0
    with source_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    sha256 = digest.hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ObjectStorageError(
            "source_file_changed", "source file changed after upload completion"
        )
    object_key = _managed_object_key(library_id, sha256, file_name)
    version = await adapter.put_file(object_key, source_path, content_type)
    locator = StorageLocatorV1(
        provider=adapter.provider,
        endpoint_ref=adapter.endpoint_ref,
        bucket=adapter.bucket,
        object_key=object_key,
        object_version=version.object_version,
        etag=version.etag,
        immutability_mode="version_id" if version.object_version else "content_hash",
    )
    require_storage_adapter_identity(adapter, locator)
    stat = await adapter.stat(locator.object_key, locator.object_version)
    if stat.size_bytes != size_bytes:
        raise ObjectStorageError(
            "object_verification_failed", "stored object size verification failed"
        )
    return PreparedStoredFile(
        library_id=library_id,
        file_name=file_name,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        locator=locator,
        managed_snapshot=True,
        source_locator=source_locator or SourceLocatorV1(kind="upload"),
        verified_at=datetime.now(timezone.utc),
    )


def bind_prepared_file_object(
    prepared: PreparedStoredFile,
    *,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    file_id: uuid.UUID | None = None,
) -> PreparedRevisionFileCapture:
    return PreparedRevisionFileCapture(
        library_id=prepared.library_id,
        file_name=prepared.file_name,
        content_type=prepared.content_type,
        size_bytes=prepared.size_bytes,
        sha256=prepared.sha256,
        locator=prepared.locator,
        managed_snapshot=prepared.managed_snapshot,
        source_locator=prepared.source_locator,
        verified_at=prepared.verified_at,
        document_id=document_id,
        document_revision_id=document_revision_id,
        file_id=file_id,
    )


async def prepare_managed_revision_file_capture(
    *,
    adapter,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    content: bytes,
    source_locator: SourceLocatorV1 | None = None,
) -> PreparedRevisionFileCapture:
    prepared = await prepare_managed_file_object(
        adapter=adapter,
        library_id=library_id,
        file_name=file_name,
        content_type=content_type,
        content=content,
        source_locator=source_locator,
    )
    return bind_prepared_file_object(
        prepared,
        document_id=document_id,
        document_revision_id=document_revision_id,
    )


async def prepare_direct_revision_file_capture(
    *,
    adapter,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    expected_sha256: str,
    expected_size_bytes: int,
    locator: StorageLocatorV1,
    source_locator: SourceLocatorV1,
) -> PreparedRevisionFileCapture:
    file_name, content_type = _file_metadata(file_name, content_type)
    if locator.immutability_mode != "version_id" or not locator.object_version:
        raise ObjectStorageError(
            "immutable_version_required",
            "direct external storage requires an exact immutable object version",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ObjectStorageError("invalid_file_identity", "file SHA-256 is invalid")
    if expected_size_bytes < 0:
        raise ObjectStorageError("invalid_file_identity", "file size is invalid")
    if source_locator.kind != "external_object":
        raise ObjectStorageError(
            "invalid_source_locator", "direct storage requires an external source locator"
        )
    source_identity = (
        source_locator.provider,
        source_locator.endpoint_ref,
        source_locator.bucket,
        source_locator.object_key,
        source_locator.object_version,
    )
    locator_identity = (
        locator.provider,
        locator.endpoint_ref,
        locator.bucket,
        locator.object_key,
        locator.object_version,
    )
    if source_identity != locator_identity:
        raise ObjectStorageError(
            "storage_identity_mismatch", "source and revision storage identity differ"
        )
    await _verify_object(
        adapter=adapter,
        locator=locator,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
    )
    return PreparedRevisionFileCapture(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        file_name=file_name,
        content_type=content_type,
        size_bytes=expected_size_bytes,
        sha256=expected_sha256,
        locator=locator,
        managed_snapshot=False,
        source_locator=source_locator,
        verified_at=datetime.now(timezone.utc),
    )


def prepared_capture_values(prepared: Any) -> dict[str, Any]:
    source_locator = prepared.source_locator
    if source_locator is None:
        source_payload = None
    elif hasattr(source_locator, "model_dump"):
        source_payload = source_locator.model_dump(mode="json")
    else:
        source_payload = source_locator
    return {
        "file_name": prepared.file_name,
        "content_type": prepared.content_type,
        "storage_path": prepared.locator.object_key,
        "size_bytes": prepared.size_bytes,
        "sha256": prepared.sha256,
        "storage_provider": prepared.locator.provider,
        "endpoint_ref": prepared.locator.endpoint_ref,
        "bucket": prepared.locator.bucket,
        "object_key": prepared.locator.object_key,
        "object_version": prepared.locator.object_version,
        "etag": prepared.locator.etag,
        "immutability_mode": prepared.locator.immutability_mode,
        "managed_snapshot": prepared.managed_snapshot,
        "source_locator": source_payload,
        "verified_at": prepared.verified_at,
    }


def storage_locator_from_row(row: DocumentRevisionFile) -> StorageLocatorV1:
    return StorageLocatorV1(
        provider=row.storage_provider,
        endpoint_ref=row.endpoint_ref,
        bucket=row.bucket,
        object_key=row.object_key,
        object_version=row.object_version,
        etag=row.etag,
        immutability_mode=row.immutability_mode,
    )


def _same_capture_identity(
    row: DocumentRevisionFile, prepared: PreparedRevisionFileCapture
) -> bool:
    if prepared.file_id is not None and row.id != prepared.file_id:
        return False
    values = prepared_capture_values(prepared)
    fields = (
        "document_revision_id",
        "document_id",
        "library_id",
        "size_bytes",
        "sha256",
        "storage_provider",
        "endpoint_ref",
        "bucket",
        "object_key",
        "immutability_mode",
        "managed_snapshot",
        "source_locator",
    )
    candidate = {
        **values,
        "document_revision_id": prepared.document_revision_id,
        "document_id": prepared.document_id,
        "library_id": prepared.library_id,
    }
    if not all(getattr(row, name) == candidate[name] for name in fields):
        return False
    if prepared.managed_snapshot:
        return True
    return (
        row.object_version == candidate["object_version"]
        and row.etag == candidate["etag"]
    )


async def _locked_revision_file(
    db, document_revision_id: uuid.UUID
) -> DocumentRevisionFile | None:
    result = await db.execute(
        select(DocumentRevisionFile)
        .where(DocumentRevisionFile.document_revision_id == document_revision_id)
        .with_for_update()
    )
    return result.scalars().first()


async def persist_revision_file_capture(
    db,
    *,
    prepared: PreparedRevisionFileCapture,
) -> DocumentRevisionFile:
    if not isinstance(prepared, PreparedRevisionFileCapture):
        raise ObjectStorageError(
            "invalid_revision_file_capture", "revision file capture is invalid"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", prepared.sha256) or prepared.size_bytes < 0:
        raise ObjectStorageError(
            "invalid_revision_file_capture", "revision file identity is invalid"
        )
    document_result = await db.execute(
        select(Document)
        .where(Document.id == prepared.document_id)
        .with_for_update()
    )
    document = document_result.scalars().first()
    revision_result = await db.execute(
        select(DocumentRevision)
        .where(DocumentRevision.id == prepared.document_revision_id)
        .with_for_update()
    )
    revision = revision_result.scalars().first()
    if document is None or revision is None:
        raise ObjectStorageError(
            "revision_file_scope_missing", "revision file scope was not found"
        )
    if (
        document.library_id != prepared.library_id
        or revision.library_id != prepared.library_id
        or revision.document_id != prepared.document_id
    ):
        raise ObjectStorageError(
            "revision_file_scope_mismatch", "revision file scope does not match"
        )
    if document.deleted_at is not None or revision.status in {
        "failed",
        "superseded",
        "deleted",
    }:
        raise ObjectStorageError(
            "revision_file_scope_stale", "revision file scope is no longer writable"
        )
    if prepared.document_revision_id not in {
        document.latest_revision_id,
        document.current_revision_id,
    }:
        raise ObjectStorageError(
            "revision_file_scope_stale", "revision is neither latest nor current"
        )
    existing = await _locked_revision_file(db, prepared.document_revision_id)
    if existing is not None:
        if not _same_capture_identity(existing, prepared):
            raise ObjectStorageError(
                "revision_file_identity_conflict",
                "Revision file already has a different immutable identity",
            )
        return existing
    row = DocumentRevisionFile(
        id=prepared.file_id or uuid.uuid4(),
        document_revision_id=prepared.document_revision_id,
        document_id=prepared.document_id,
        library_id=prepared.library_id,
        **prepared_capture_values(prepared),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        winner = await _locked_revision_file(db, prepared.document_revision_id)
        if winner is None:
            raise
        if not _same_capture_identity(winner, prepared):
            raise ObjectStorageError(
                "revision_file_identity_conflict",
                "Revision file race produced a different immutable identity",
            ) from None
        return winner
    return row


async def current_revision_file(
    db,
    *,
    library_id: uuid.UUID,
    document: Document,
) -> DocumentRevisionFile | None:
    if document.library_id != library_id or document.deleted_at is not None:
        raise ObjectStorageError("document_not_found", "document was not found")
    if document.current_revision_id is None:
        return None
    result = await db.execute(
        select(DocumentRevisionFile).where(
            DocumentRevisionFile.library_id == library_id,
            DocumentRevisionFile.document_id == document.id,
            DocumentRevisionFile.document_revision_id == document.current_revision_id,
        )
    )
    return result.scalars().first()


async def verified_revision_file_bytes(adapter, row: DocumentRevisionFile) -> bytes:
    access = (
        row if isinstance(row, RevisionFileAccess) else revision_file_access_from_row(row)
    )
    if access.lifecycle_status != "available":
        raise ObjectStorageError(
            "revision_file_unavailable", "revision file is not available"
        )
    return await _verify_object(
        adapter=adapter,
        locator=access.locator,
        expected_sha256=access.sha256,
        expected_size_bytes=access.size_bytes,
    )


def revision_file_access_from_row(row: DocumentRevisionFile) -> RevisionFileAccess:
    lifecycle_status = getattr(row, "lifecycle_status", "available")
    if lifecycle_status != "available":
        raise ObjectStorageError(
            "revision_file_unavailable", "revision file is not available"
        )
    return RevisionFileAccess(
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        locator=storage_locator_from_row(row),
        lifecycle_status=lifecycle_status,
    )
