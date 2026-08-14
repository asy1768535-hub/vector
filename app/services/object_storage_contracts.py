from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ObjectStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class StorageObjectVersion:
    object_version: str | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class StorageObjectStat:
    size_bytes: int
    object_version: str | None
    etag: str | None


class ObjectStorageAdapter(Protocol):
    provider: str
    endpoint_ref: str
    bucket: str | None

    async def put(
        self, object_key: str, content: bytes, content_type: str | None
    ) -> StorageObjectVersion: ...

    async def put_file(
        self, object_key: str, source_path: Path, content_type: str | None
    ) -> StorageObjectVersion: ...

    async def read(self, object_key: str, object_version: str | None) -> bytes: ...

    async def stat(
        self, object_key: str, object_version: str | None
    ) -> StorageObjectStat: ...

    async def delete(self, object_key: str, object_version: str | None) -> None: ...

    async def download_url(
        self, object_key: str, object_version: str | None, expires_seconds: int
    ) -> str | None: ...
