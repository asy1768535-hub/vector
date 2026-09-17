"""Run a one-object MinIO connectivity smoke test.

All credentials are read from the process environment.  The script creates a
unique object under ``smoke/``, verifies size and SHA-256, deletes it, and
verifies that it is gone.  It never lists or mutates any other object.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from io import BytesIO
from urllib.parse import urlparse


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"missing runtime environment variable: {name}")
    return value


def _endpoint() -> tuple[str, bool]:
    raw = _required("MINIO_ENDPOINT")
    secure = os.getenv("MINIO_SECURE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    parsed = urlparse(raw if "://" in raw else f"{'https' if secure else 'http'}://{raw}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise SystemExit("MINIO_ENDPOINT must be a host or an http(s) URL without a path")
    return parsed.netloc, parsed.scheme == "https"


def main() -> int:
    try:
        endpoint, secure = _endpoint()
        access_key = _required("MINIO_ACCESS_KEY")
        secret_key = _required("MINIO_SECRET_KEY")
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        from minio import Minio
    except ImportError:
        print("minio SDK is not installed; install the object-storage extra", file=sys.stderr)
        return 2

    bucket = os.getenv("MINIO_BUCKET", "vector-database-raw").strip() or "vector-database-raw"
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    object_key = f"smoke/{uuid.uuid4().hex}.bin"
    payload = f"vector-database-minio-smoke:{uuid.uuid4().hex}".encode("ascii")
    digest = hashlib.sha256(payload).hexdigest()
    version_id: str | None = None

    try:
        result = client.put_object(
            bucket,
            object_key,
            BytesIO(payload),
            len(payload),
            content_type="application/octet-stream",
        )
        version_id = getattr(result, "version_id", None)
        stat = client.stat_object(bucket, object_key, version_id=version_id)
        if stat.size != len(payload):
            raise RuntimeError("size verification failed")
        response = client.get_object(bucket, object_key, version_id=version_id)
        try:
            downloaded = response.read()
        finally:
            response.close()
            response.release_conn()
        if hashlib.sha256(downloaded).hexdigest() != digest:
            raise RuntimeError("sha256 verification failed")
    except Exception as exc:  # noqa: BLE001
        print(f"MinIO smoke failed before cleanup: {exc.__class__.__name__}", file=sys.stderr)
        try:
            client.remove_object(bucket, object_key, version_id=version_id)
        except Exception as cleanup_error:  # noqa: BLE001
            print(
                "MinIO smoke cleanup after failure also failed: "
                f"{cleanup_error.__class__.__name__}",
                file=sys.stderr,
            )
        return 1

    try:
        client.remove_object(bucket, object_key, version_id=version_id)
        try:
            client.stat_object(bucket, object_key, version_id=version_id)
        except Exception as exc:
            if getattr(exc, "code", None) not in {"NoSuchKey", "NoSuchObject", "NotFound"} and getattr(
                exc, "status", None
            ) != 404:
                raise
        else:
            raise RuntimeError("object still exists after deletion")
    except Exception as exc:  # noqa: BLE001
        print(f"MinIO smoke cleanup failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1

    print(f"MinIO smoke passed: bucket={bucket} key={object_key} size={len(payload)} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
