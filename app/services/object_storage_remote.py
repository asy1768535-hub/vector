from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from app.schemas.storage import validate_object_key
from app.services.object_storage_contracts import (
    ObjectStorageError,
    StorageObjectStat,
    StorageObjectVersion,
)


_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchObject", "NoSuchVersion", "NotFound"}


def _validate_remote_key(object_key: str) -> None:
    try:
        validate_object_key(object_key)
    except ValueError:
        raise ObjectStorageError("unsafe_object_key", "object key is unsafe") from None


def _is_not_found(exc: Exception) -> bool:
    try:
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
    except Exception:
        return False
    return isinstance(code, str) and code in _NOT_FOUND_CODES or status == 404


def _provider_text_or_none(value: Any, *, limit: int = 512) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        raise ObjectStorageError(
            "provider_metadata_invalid", "remote object metadata is invalid"
        ) from None
    if not text:
        return None
    if len(text) > limit or "\r" in text or "\n" in text:
        raise ObjectStorageError(
            "provider_metadata_invalid", "remote object metadata is invalid"
        )
    return text


def _provider_size(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ObjectStorageError(
            "provider_metadata_invalid", "remote object metadata is invalid"
        ) from None
    if isinstance(value, bool) or size < 0:
        raise ObjectStorageError(
            "provider_metadata_invalid", "remote object metadata is invalid"
        )
    return size


class MinioObjectStorageAdapter:
    provider = "minio"

    def __init__(
        self,
        *,
        client,
        endpoint_ref: str,
        bucket: str,
        max_read_bytes: int,
    ) -> None:
        self._client = client
        self.endpoint_ref = endpoint_ref
        self.bucket = bucket
        self._max_read_bytes = max_read_bytes

    async def _call(self, code: str, operation: Callable[[], Any]):
        try:
            return await asyncio.to_thread(operation)
        except ObjectStorageError:
            raise
        except Exception as exc:
            if _is_not_found(exc):
                raise ObjectStorageError(
                    "object_not_found", "stored object was not found"
                ) from None
            raise ObjectStorageError(code, "remote object storage operation failed") from None

    async def put(
        self, object_key: str, content: bytes, content_type: str | None
    ) -> StorageObjectVersion:
        _validate_remote_key(object_key)
        result = await self._call(
            "provider_write_failed",
            lambda: self._client.put_object(
                self.bucket,
                object_key,
                io.BytesIO(content),
                len(content),
                content_type=content_type or "application/octet-stream",
            ),
        )
        return StorageObjectVersion(
            object_version=_provider_text_or_none(getattr(result, "version_id", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def put_file(
        self, object_key: str, source_path: Path, content_type: str | None
    ) -> StorageObjectVersion:
        _validate_remote_key(object_key)
        result = await self._call(
            "provider_write_failed",
            lambda: self._client.fput_object(
                self.bucket,
                object_key,
                str(source_path),
                content_type=content_type or "application/octet-stream",
            ),
        )
        return StorageObjectVersion(
            object_version=_provider_text_or_none(getattr(result, "version_id", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def read(self, object_key: str, object_version: str | None) -> bytes:
        _validate_remote_key(object_key)

        def operation() -> bytes:
            response = self._client.get_object(
                self.bucket, object_key, version_id=object_version
            )
            try:
                content = response.read(self._max_read_bytes + 1)
                if len(content) > self._max_read_bytes:
                    raise ObjectStorageError(
                        "object_too_large", "stored object exceeds the read limit"
                    )
                return content
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                release = getattr(response, "release_conn", None)
                if callable(release):
                    release()

        return await self._call("provider_read_failed", operation)

    async def stat(
        self, object_key: str, object_version: str | None
    ) -> StorageObjectStat:
        _validate_remote_key(object_key)
        result = await self._call(
            "provider_stat_failed",
            lambda: self._client.stat_object(
                self.bucket, object_key, version_id=object_version
            ),
        )
        return StorageObjectStat(
            size_bytes=_provider_size(getattr(result, "size", None)),
            object_version=_provider_text_or_none(getattr(result, "version_id", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def delete(self, object_key: str, object_version: str | None) -> None:
        _validate_remote_key(object_key)
        await self._call(
            "provider_delete_failed",
            lambda: self._client.remove_object(
                self.bucket, object_key, version_id=object_version
            ),
        )

    async def download_url(
        self, object_key: str, object_version: str | None, expires_seconds: int
    ) -> str:
        _validate_remote_key(object_key)
        return await self._call(
            "provider_sign_failed",
            lambda: self._client.presigned_get_object(
                self.bucket,
                object_key,
                expires=timedelta(seconds=expires_seconds),
                version_id=object_version,
            ),
        )


class OssObjectStorageAdapter:
    provider = "oss"

    def __init__(
        self,
        *,
        bucket_client,
        endpoint_ref: str,
        bucket: str,
        max_read_bytes: int,
    ) -> None:
        self._bucket_client = bucket_client
        self.endpoint_ref = endpoint_ref
        self.bucket = bucket
        self._max_read_bytes = max_read_bytes

    async def _call(self, code: str, operation: Callable[[], Any]):
        try:
            return await asyncio.to_thread(operation)
        except ObjectStorageError:
            raise
        except Exception as exc:
            if _is_not_found(exc):
                raise ObjectStorageError(
                    "object_not_found", "stored object was not found"
                ) from None
            raise ObjectStorageError(code, "remote object storage operation failed") from None

    @staticmethod
    def _params(object_version: str | None) -> dict[str, str] | None:
        return {"versionId": object_version} if object_version else None

    async def put(
        self, object_key: str, content: bytes, content_type: str | None
    ) -> StorageObjectVersion:
        _validate_remote_key(object_key)
        headers = {"Content-Type": content_type} if content_type else None
        result = await self._call(
            "provider_write_failed",
            lambda: self._bucket_client.put_object(object_key, content, headers=headers),
        )
        return StorageObjectVersion(
            object_version=_provider_text_or_none(getattr(result, "versionid", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def put_file(
        self, object_key: str, source_path: Path, content_type: str | None
    ) -> StorageObjectVersion:
        _validate_remote_key(object_key)
        headers = {"Content-Type": content_type} if content_type else None
        result = await self._call(
            "provider_write_failed",
            lambda: self._bucket_client.put_object_from_file(
                object_key, str(source_path), headers=headers
            ),
        )
        return StorageObjectVersion(
            object_version=_provider_text_or_none(getattr(result, "versionid", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def read(self, object_key: str, object_version: str | None) -> bytes:
        _validate_remote_key(object_key)

        def operation() -> bytes:
            result = self._bucket_client.get_object(
                object_key, params=self._params(object_version)
            )
            content = result.read(self._max_read_bytes + 1)
            if len(content) > self._max_read_bytes:
                raise ObjectStorageError(
                    "object_too_large", "stored object exceeds the read limit"
                )
            return content

        return await self._call("provider_read_failed", operation)

    async def stat(
        self, object_key: str, object_version: str | None
    ) -> StorageObjectStat:
        _validate_remote_key(object_key)
        result = await self._call(
            "provider_stat_failed",
            lambda: self._bucket_client.head_object(
                object_key, params=self._params(object_version)
            ),
        )
        return StorageObjectStat(
            size_bytes=_provider_size(getattr(result, "content_length", None)),
            object_version=_provider_text_or_none(getattr(result, "versionid", None)),
            etag=_provider_text_or_none(getattr(result, "etag", None)),
        )

    async def delete(self, object_key: str, object_version: str | None) -> None:
        _validate_remote_key(object_key)
        await self._call(
            "provider_delete_failed",
            lambda: self._bucket_client.delete_object(
                object_key, params=self._params(object_version)
            ),
        )

    async def download_url(
        self, object_key: str, object_version: str | None, expires_seconds: int
    ) -> str:
        _validate_remote_key(object_key)
        return await self._call(
            "provider_sign_failed",
            lambda: self._bucket_client.sign_url(
                "GET",
                object_key,
                expires_seconds,
                params=self._params(object_version),
            ),
        )
