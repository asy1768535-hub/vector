from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.models.file_resource import FileResource
from app.schemas.storage import StorageLocatorV1
from app.services.object_storage_contracts import ObjectStorageError
from app.services.revision_files import (
    require_storage_adapter_identity,
    verify_stored_object,
)

_SAFE_SUFFIX = re.compile(r"\.[a-z0-9][a-z0-9._-]{0,15}")


@dataclass(frozen=True, slots=True)
class PreparedFileResource:
    library_id: uuid.UUID
    upload_context_id: uuid.UUID
    file_name: str
    content_type: str | None
    relative_path: str | None
    size_bytes: int
    sha256: str
    locator: StorageLocatorV1
    verified_at: datetime


def _metadata(
    file_name: str, content_type: str | None, relative_path: str | None
) -> tuple[str, str | None, str | None]:
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
    if relative_path is not None and (
        not isinstance(relative_path, str)
        or len(relative_path) > 4096
        or "\x00" in relative_path
        or "\r" in relative_path
        or "\n" in relative_path
    ):
        raise ObjectStorageError("invalid_file_metadata", "relative path is invalid")
    return (
        file_name.strip(),
        content_type.strip() if content_type else None,
        relative_path.strip() if relative_path else None,
    )


def _suffix(file_name: str) -> str:
    suffix = "." + file_name.rsplit(".", 1)[1].lower() if "." in file_name else ""
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""


def resource_object_key(
    library_id: uuid.UUID, upload_context_id: uuid.UUID, file_name: str
) -> str:
    file_name, _, _ = _metadata(file_name, None, None)
    return f"libraries/{library_id}/file-resources/{upload_context_id}/original{_suffix(file_name)}"


def resource_id_for_upload_context(upload_context_id: uuid.UUID) -> uuid.UUID:
    """Keep the resource identity stable across Complete retries."""

    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"vectorDatabase:file-resource:{upload_context_id}"
    )


async def _hash_path(source_path: Path) -> tuple[int, str]:
    def calculate() -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        with source_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        return size_bytes, digest.hexdigest()

    return await asyncio.to_thread(calculate)


def _locator_from_resource(resource: FileResource) -> StorageLocatorV1:
    return StorageLocatorV1(
        provider=resource.storage_provider,
        endpoint_ref=resource.endpoint_ref,
        bucket=resource.bucket,
        object_key=resource.object_key,
        object_version=resource.object_version,
        etag=resource.etag,
        immutability_mode=resource.immutability_mode,
    )


def build_storing_file_resource(
    *,
    library_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID | None,
    upload_context_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    relative_path: str | None,
    size_bytes: int,
    sha256: str,
    adapter,
    resource_id: uuid.UUID | None = None,
) -> FileResource:
    file_name, content_type, relative_path = _metadata(
        file_name, content_type, relative_path
    )
    object_key = resource_object_key(library_id, upload_context_id, file_name)
    return FileResource(
        id=resource_id or resource_id_for_upload_context(upload_context_id),
        library_id=library_id,
        uploaded_by_user_id=uploaded_by_user_id,
        file_name=file_name,
        relative_path=relative_path,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_path=object_key,
        storage_provider=adapter.provider,
        endpoint_ref=adapter.endpoint_ref,
        bucket=adapter.bucket,
        object_key=object_key,
        immutability_mode="content_hash",
        storage_status="storing",
    )


def _prepared(
    *,
    resource: FileResource,
    locator: StorageLocatorV1,
    verified_at: datetime,
) -> PreparedFileResource:
    return PreparedFileResource(
        library_id=resource.library_id,
        upload_context_id=resource.id,
        file_name=resource.file_name,
        content_type=resource.content_type,
        relative_path=resource.relative_path,
        size_bytes=resource.size_bytes,
        sha256=resource.sha256,
        locator=locator,
        verified_at=verified_at,
    )


async def _store_and_verify(
    *,
    adapter,
    resource: FileResource,
    source_path: Path,
    expected_size_bytes: int,
    expected_sha256: str,
) -> PreparedFileResource:
    if not source_path.is_file() or source_path.is_symlink():
        raise ObjectStorageError("source_file_invalid", "source file is invalid")
    size_bytes, sha256 = await _hash_path(source_path)
    if size_bytes != expected_size_bytes or sha256 != expected_sha256:
        raise ObjectStorageError(
            "source_file_changed", "source file changed after upload completion"
        )

    object_key = resource.object_key
    object_version = resource.object_version
    etag = resource.etag
    wrote_object = False
    stat = None
    try:
        stat = await adapter.stat(object_key, object_version)
    except KeyError:
        pass
    except ObjectStorageError as exc:
        if exc.code != "object_not_found":
            raise
    if stat is None:
        version = await adapter.put_file(object_key, source_path, resource.content_type)
        object_version = version.object_version
        etag = version.etag
        wrote_object = True
    else:
        object_version = stat.object_version
        etag = stat.etag

    locator = StorageLocatorV1(
        provider=resource.storage_provider,
        endpoint_ref=resource.endpoint_ref,
        bucket=resource.bucket,
        object_key=object_key,
        object_version=object_version,
        etag=etag,
        immutability_mode="version_id" if object_version else "content_hash",
    )
    try:
        await verify_stored_object(
            adapter=adapter,
            locator=locator,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
    except BaseException:
        if wrote_object:
            try:
                await adapter.delete(object_key, object_version)
            except Exception:
                pass
        raise
    return _prepared(
        resource=resource,
        locator=locator,
        verified_at=datetime.now(timezone.utc),
    )


async def store_file_resource_object(
    *,
    adapter,
    resource: FileResource,
    source_path: Path,
) -> PreparedFileResource:
    """Write or safely reuse a resource object, then verify its bytes."""

    return await _store_and_verify(
        adapter=adapter,
        resource=resource,
        source_path=source_path,
        expected_size_bytes=resource.size_bytes,
        expected_sha256=resource.sha256,
    )


async def prepare_file_resource(
    *,
    adapter,
    library_id: uuid.UUID,
    upload_context_id: uuid.UUID,
    file_name: str,
    content_type: str | None,
    relative_path: str | None = None,
    source_path: Path,
    expected_size_bytes: int,
    expected_sha256: str,
) -> PreparedFileResource:
    resource = build_storing_file_resource(
        library_id=library_id,
        uploaded_by_user_id=None,
        upload_context_id=upload_context_id,
        file_name=file_name,
        content_type=content_type,
        relative_path=relative_path,
        size_bytes=expected_size_bytes,
        sha256=expected_sha256,
        adapter=adapter,
    )
    return await store_file_resource_object(
        adapter=adapter,
        resource=resource,
        source_path=source_path,
    )


def build_file_resource(
    prepared: PreparedFileResource,
    *,
    library_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID | None,
    file_name: str,
    relative_path: str | None,
    resource_id: uuid.UUID | None = None,
) -> FileResource:
    if prepared.library_id != library_id:
        raise ObjectStorageError("library_mismatch", "file resource library mismatch")
    if prepared.file_name != file_name.strip():
        raise ObjectStorageError("file_name_mismatch", "file resource name mismatch")
    if prepared.relative_path != (relative_path.strip() if relative_path else None):
        raise ObjectStorageError(
            "relative_path_mismatch", "file resource path mismatch"
        )
    locator = prepared.locator
    return FileResource(
        id=resource_id or resource_id_for_upload_context(prepared.upload_context_id),
        library_id=library_id,
        uploaded_by_user_id=uploaded_by_user_id,
        file_name=prepared.file_name,
        relative_path=prepared.relative_path,
        content_type=prepared.content_type,
        size_bytes=prepared.size_bytes,
        sha256=prepared.sha256,
        storage_path=locator.object_key,
        storage_provider=locator.provider,
        endpoint_ref=locator.endpoint_ref,
        bucket=locator.bucket,
        object_key=locator.object_key,
        object_version=locator.object_version,
        etag=locator.etag,
        immutability_mode=locator.immutability_mode,
        storage_status="available",
        storage_verified_at=prepared.verified_at,
    )


async def materialize_file_resource(
    *,
    adapter,
    resource: FileResource,
    destination_path: Path,
) -> Path:
    if resource.storage_status != "available":
        raise ObjectStorageError(
            "file_resource_unavailable", "file resource is not available"
        )
    locator = _locator_from_resource(resource)
    require_storage_adapter_identity(adapter, locator)
    await adapter.materialize(
        locator.object_key,
        locator.object_version,
        destination_path,
    )
    actual_size, actual_sha256 = await _hash_path(destination_path)
    if actual_size != resource.size_bytes or actual_sha256 != resource.sha256:
        destination_path.unlink(missing_ok=True)
        raise ObjectStorageError(
            "object_verification_failed", "materialized file content verification failed"
        )
    return destination_path


async def file_resource_download_url(
    *,
    adapter,
    resource: FileResource,
    expires_seconds: int,
) -> str:
    """Return a provider-signed URL only for one verified file resource."""
    if resource.storage_status != "available":
        raise ObjectStorageError(
            "file_resource_unavailable", "file resource is not available"
        )
    locator = _locator_from_resource(resource)
    require_storage_adapter_identity(adapter, locator)
    url = await adapter.download_url(
        locator.object_key,
        locator.object_version,
        expires_seconds,
    )
    if not isinstance(url, str) or not url.strip():
        raise ObjectStorageError(
            "download_url_unsupported", "storage provider cannot create a download URL"
        )
    return url


async def delete_file_resource_object(*, adapter, resource: FileResource, db=None) -> None:
    """Delete a resource's object with an idempotent lifecycle transition.

    This is deliberately separate from processing failure and staging cleanup:
    callers invoke it only for an explicit resource-deletion operation.  The
    optional session is flushed after each state transition so a crashed
    caller leaves ``deleting`` for a later retry instead of silently losing
    the locator.
    """
    if resource.storage_status == "deleted":
        return
    if resource.storage_status not in {"available", "storage_failed", "deleting"}:
        raise ObjectStorageError(
            "file_resource_unavailable", "file resource is not ready for deletion"
        )
    locator = _locator_from_resource(resource)
    require_storage_adapter_identity(adapter, locator)
    resource.storage_status = "deleting"
    resource.storage_error_code = None
    if db is not None:
        await db.flush()
    try:
        await adapter.delete(locator.object_key, locator.object_version)
    except ObjectStorageError as exc:
        if exc.code != "object_not_found":
            resource.storage_status = "storage_failed"
            resource.storage_error_code = exc.code
            if db is not None:
                await db.flush()
            raise
    resource.storage_status = "deleted"
    resource.storage_verified_at = None
    resource.storage_error_code = None
    if db is not None:
        await db.flush()
