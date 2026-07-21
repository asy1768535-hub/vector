from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.schemas.storage import validate_object_key
from app.services.object_storage_contracts import (
    ObjectStorageError,
    StorageObjectStat,
    StorageObjectVersion,
)


class LocalObjectStorageAdapter:
    provider = "local"
    bucket = None

    def __init__(self, *, root: Path, endpoint_ref: str, max_read_bytes: int) -> None:
        self.endpoint_ref = endpoint_ref
        self._root = root.resolve()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ObjectStorageError(
                "local_root_invalid", "local storage root is invalid"
            ) from None
        if not self._root.is_dir():
            raise ObjectStorageError("local_root_invalid", "local storage root is invalid")
        self._max_read_bytes = max_read_bytes

    def path_for_read(self, object_key: str) -> Path:
        return self._resolve(object_key, require_file=True)

    def _resolve(self, object_key: str, *, require_file: bool = False) -> Path:
        try:
            validate_object_key(object_key)
        except ValueError as exc:
            raise ObjectStorageError("unsafe_object_key", "object key is unsafe") from exc
        candidate = self._root.joinpath(*object_key.split("/"))
        current = self._root
        for part in object_key.split("/"):
            current = current / part
            if current.exists() and current.is_symlink():
                raise ObjectStorageError("unsafe_object_key", "object key crosses a symlink")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise ObjectStorageError("unsafe_object_key", "object key escapes the storage root") from exc
        if require_file and (not resolved.is_file() or resolved.is_symlink()):
            raise ObjectStorageError("object_not_found", "stored object was not found")
        return resolved

    async def put(
        self, object_key: str, content: bytes, content_type: str | None
    ) -> StorageObjectVersion:
        return await asyncio.to_thread(self._put, object_key, content, content_type)

    def _put(
        self, object_key: str, content: bytes, content_type: str | None
    ) -> StorageObjectVersion:
        del content_type
        path = self._resolve(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._resolve(object_key)
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise ObjectStorageError("unsafe_object_key", "object path is not a regular file")
            with path.open("rb") as handle:
                existing = handle.read(len(content) + 1)
            if existing != content:
                raise ObjectStorageError(
                    "object_identity_conflict",
                    "stored object key belongs to different bytes",
                )
            return StorageObjectVersion(object_version=None, etag=None)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temp_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
        return StorageObjectVersion(object_version=None, etag=None)

    async def read(self, object_key: str, object_version: str | None) -> bytes:
        return await asyncio.to_thread(self._read, object_key, object_version)

    def _read(self, object_key: str, object_version: str | None) -> bytes:
        if object_version is not None:
            raise ObjectStorageError(
                "invalid_object_version", "local storage does not use object versions"
            )
        path = self.path_for_read(object_key)
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > self._max_read_bytes:
                raise ObjectStorageError(
                    "object_too_large", "stored object exceeds the read limit"
                )
            content = handle.read(self._max_read_bytes + 1)
        if len(content) > self._max_read_bytes:
            raise ObjectStorageError("object_too_large", "stored object exceeds the read limit")
        return content

    async def stat(
        self, object_key: str, object_version: str | None
    ) -> StorageObjectStat:
        return await asyncio.to_thread(self._stat, object_key, object_version)

    def _stat(
        self, object_key: str, object_version: str | None
    ) -> StorageObjectStat:
        if object_version is not None:
            raise ObjectStorageError(
                "invalid_object_version", "local storage does not use object versions"
            )
        path = self.path_for_read(object_key)
        return StorageObjectStat(
            size_bytes=path.stat().st_size,
            object_version=None,
            etag=None,
        )

    async def delete(self, object_key: str, object_version: str | None) -> None:
        await asyncio.to_thread(self._delete, object_key, object_version)

    def _delete(self, object_key: str, object_version: str | None) -> None:
        if object_version is not None:
            raise ObjectStorageError(
                "invalid_object_version", "local storage does not use object versions"
            )
        path = self._resolve(object_key)
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise ObjectStorageError("unsafe_object_key", "object path is not a regular file")
            path.unlink()

    async def download_url(
        self, object_key: str, object_version: str | None, expires_seconds: int
    ) -> None:
        del object_key, object_version, expires_seconds
        return None
