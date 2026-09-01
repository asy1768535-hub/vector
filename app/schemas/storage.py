from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


StorageProvider = Literal["local", "minio", "oss"]
ImmutabilityMode = Literal["content_hash", "version_id"]
_ENDPOINT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def validate_object_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("object key must be a string")
    if not value or len(value) > 2_048:
        raise ValueError("object key must be nonblank and bounded")
    if "\\" in value or "\x00" in value or "://" in value or "?" in value or "#" in value:
        raise ValueError("object key contains a forbidden character")
    if value.startswith("/"):
        raise ValueError("object key must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object key contains an unsafe path segment")
    return value


def _bounded_optional(value: str | None, *, label: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > limit or "\r" in value or "\n" in value:
        raise ValueError(f"{label} must be nonblank and bounded")
    return value


class StorageLocatorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    provider: StorageProvider
    endpoint_ref: str = Field(min_length=1, max_length=128)
    bucket: str | None = Field(default=None, max_length=255)
    object_key: str = Field(min_length=1, max_length=2_048)
    object_version: str | None = Field(default=None, max_length=512)
    etag: str | None = Field(default=None, max_length=512)
    immutability_mode: ImmutabilityMode

    @field_validator("endpoint_ref")
    @classmethod
    def _validate_endpoint_ref(cls, value: str) -> str:
        if not _ENDPOINT_REF.fullmatch(value):
            raise ValueError("endpoint reference is invalid")
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str | None) -> str | None:
        value = _bounded_optional(value, label="bucket", limit=255)
        if value is not None and not _BUCKET.fullmatch(value):
            raise ValueError("bucket is invalid")
        return value

    @field_validator("object_key")
    @classmethod
    def _validate_object_key(cls, value: str) -> str:
        return validate_object_key(value)

    @field_validator("object_version")
    @classmethod
    def _validate_version(cls, value: str | None) -> str | None:
        return _bounded_optional(value, label="object version", limit=512)

    @field_validator("etag")
    @classmethod
    def _validate_etag(cls, value: str | None) -> str | None:
        return _bounded_optional(value, label="ETag", limit=512)

    @model_validator(mode="after")
    def _validate_provider_shape(self):
        if self.provider == "local" and self.bucket is not None:
            raise ValueError("local storage cannot have a bucket")
        if self.provider != "local" and self.bucket is None:
            raise ValueError("remote storage requires a bucket")
        if self.immutability_mode == "version_id" and self.object_version is None:
            raise ValueError("version-id immutability requires an object version")
        if self.immutability_mode == "content_hash" and self.object_version is not None:
            raise ValueError("content-hash immutability cannot have an object version")
        if self.provider == "local" and self.immutability_mode != "content_hash":
            raise ValueError("local storage uses content-hash immutability")
        return self


class SourceLocatorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["upload", "external_object"]
    provider: StorageProvider | None = None
    endpoint_ref: str | None = Field(default=None, max_length=128)
    bucket: str | None = Field(default=None, max_length=255)
    object_key: str | None = Field(default=None, max_length=2_048)
    object_version: str | None = Field(default=None, max_length=512)
    etag: str | None = Field(default=None, max_length=512)

    @field_validator("endpoint_ref")
    @classmethod
    def _validate_endpoint_ref(cls, value: str | None) -> str | None:
        if value is not None and not _ENDPOINT_REF.fullmatch(value):
            raise ValueError("endpoint reference is invalid")
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str | None) -> str | None:
        value = _bounded_optional(value, label="bucket", limit=255)
        if value is not None and not _BUCKET.fullmatch(value):
            raise ValueError("bucket is invalid")
        return value

    @field_validator("object_key")
    @classmethod
    def _validate_key(cls, value: str | None) -> str | None:
        return validate_object_key(value) if value is not None else None

    @field_validator("object_version")
    @classmethod
    def _validate_version(cls, value: str | None) -> str | None:
        return _bounded_optional(value, label="object version", limit=512)

    @field_validator("etag")
    @classmethod
    def _validate_etag(cls, value: str | None) -> str | None:
        return _bounded_optional(value, label="ETag", limit=512)

    @model_validator(mode="after")
    def _validate_kind_shape(self):
        object_fields = (
            self.provider,
            self.endpoint_ref,
            self.bucket,
            self.object_key,
            self.object_version,
            self.etag,
        )
        if self.kind == "upload":
            if any(value is not None for value in object_fields):
                raise ValueError("upload source locator cannot identify an external object")
            return self
        if any(
            value is None
            for value in (
                self.provider,
                self.endpoint_ref,
                self.object_key,
                self.object_version,
            )
        ):
            raise ValueError("external source locator requires an immutable object version")
        if self.provider == "local" and self.bucket is not None:
            raise ValueError("local source locator cannot have a bucket")
        if self.provider != "local" and self.bucket is None:
            raise ValueError("remote source locator requires a bucket")
        return self
