from __future__ import annotations

import asyncio
import hashlib
import io
import inspect
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint


MIGRATION = Path("alembic/versions/0026_v08_object_storage.py")


def _constraint_sql(table, name: str) -> str:
    constraint = next(item for item in table.constraints if item.name == name)
    assert isinstance(constraint, CheckConstraint)
    return " ".join(str(constraint.sqltext).lower().split())


def test_revision_file_model_and_migration_expose_the_same_descriptor_contract():
    from app.models.document_revision_file import DocumentRevisionFile

    columns = DocumentRevisionFile.__table__.columns
    expected = {
        "storage_provider",
        "endpoint_ref",
        "bucket",
        "object_key",
        "object_version",
        "etag",
        "immutability_mode",
        "managed_snapshot",
        "source_locator",
        "verified_at",
    }
    assert expected <= set(columns.keys())
    assert columns.endpoint_ref.type.length == 128
    assert columns.bucket.type.length == 255
    assert columns.object_version.type.length == 512
    assert columns.etag.type.length == 512
    assert columns.source_locator.nullable is True
    assert columns.verified_at.nullable is True
    assert _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_storage_provider"
    ) == "storage_provider in ('local','minio','oss')"
    assert "bucket is null" in _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_provider_shape"
    )
    assert "object_version is not null" in _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_immutability"
    )
    assert "object_version is null" in _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_immutability"
    )
    assert "length(object_key) between 1 and 2048" in _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_locator_bounds"
    )
    assert "sha256 ~ '^[0-9a-f]{64}$'" in _constraint_sql(
        DocumentRevisionFile.__table__, "ck_document_revision_files_file_identity"
    )
    assert "octet_length(source_locator::text) <= 8192" in _constraint_sql(
        DocumentRevisionFile.__table__,
        "ck_document_revision_files_source_locator_object",
    )
    assert "source_locator->>'kind' = 'external_object'" in _constraint_sql(
        DocumentRevisionFile.__table__,
        "ck_document_revision_files_external_ownership",
    )
    unique = {
        item.name
        for item in DocumentRevisionFile.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert "uq_document_revision_files_revision" in unique

    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0026"' in text
    assert 'down_revision: Union[str, None] = "0025"' in text
    for name in expected:
        assert f'"{name}"' in text
    assert "INSERT INTO document_revision_files" in text
    assert "document_files" in text
    assert "storage_path" in text
    assert "gen_random_uuid" not in text
    assert "DELETE FROM document_revision_files" not in text


def test_storage_config_is_default_off_and_remote_providers_fail_closed(monkeypatch):
    from app.config import Settings, validate_revision_file_storage_startup

    config = Settings(_env_file=None)
    assert config.revision_file_storage_enabled is False
    assert config.document_storage_provider == "local"
    validate_revision_file_storage_startup(config)
    validate_revision_file_storage_startup(
        Settings(
            _env_file=None,
            revision_file_storage_enabled=False,
            document_storage_provider="unused-provider",
            document_storage_max_read_bytes=0,
        )
    )

    with pytest.raises(RuntimeError):
        validate_revision_file_storage_startup(
            Settings(
                _env_file=None,
                revision_file_storage_enabled=True,
            )
        )

    enabled = {
        "revision_file_storage_enabled": True,
        "enable_evidence_write_path": True,
        "document_storage_provider": "minio",
        "document_storage_endpoint_url": "https://minio.invalid",
        "document_storage_bucket": "documents",
        "document_storage_access_key": SecretStr("access"),
        "document_storage_secret_key": SecretStr("secret"),
    }
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="optional dependency"):
        validate_revision_file_storage_startup(Settings(_env_file=None, **enabled))

    invalid_endpoint = {
        **enabled,
        "document_storage_endpoint_url": "https://minio.invalid/path",
    }
    with pytest.raises(RuntimeError, match="endpoint"):
        validate_revision_file_storage_startup(
            Settings(_env_file=None, **invalid_endpoint)
        )


def test_storage_and_source_locators_are_strict_and_never_accept_urls_or_secrets():
    from app.schemas.storage import SourceLocatorV1, StorageLocatorV1

    local = StorageLocatorV1(
        provider="local",
        endpoint_ref="primary",
        object_key="libraries/a/objects/hash.txt",
        immutability_mode="content_hash",
    )
    assert local.bucket is None and local.object_version is None

    remote = StorageLocatorV1(
        provider="minio",
        endpoint_ref="primary",
        bucket="documents",
        object_key="libraries/a/objects/hash.pdf",
        object_version="v1",
        etag="etag",
        immutability_mode="version_id",
    )
    assert remote.object_version == "v1"

    for object_key in (
        "../secret",
        "/absolute",
        r"folder\file",
        "https://host/object",
        "folder/object?X-Amz-Signature=secret",
    ):
        with pytest.raises(ValidationError):
            StorageLocatorV1(
                provider="local",
                endpoint_ref="primary",
                object_key=object_key,
                immutability_mode="content_hash",
            )

    with pytest.raises(ValidationError):
        StorageLocatorV1(
            provider="oss",
            endpoint_ref="primary",
            object_key="object",
            immutability_mode="version_id",
            access_key="must-not-exist",
        )
    with pytest.raises(ValidationError):
        StorageLocatorV1(
            provider="minio",
            endpoint_ref="primary",
            bucket="documents",
            object_key="object",
            object_version="v1",
            immutability_mode="content_hash",
        )
    with pytest.raises(ValidationError):
        StorageLocatorV1(
            provider="local",
            endpoint_ref="primary",
            object_key="object",
            object_version="v1",
            immutability_mode="version_id",
        )
    with pytest.raises(ValidationError):
        SourceLocatorV1(kind="upload", provider="minio")
    with pytest.raises(ValidationError):
        SourceLocatorV1(
            kind="external_object",
            provider="oss",
            endpoint_ref="primary",
            bucket="documents",
            object_key="source/file",
        )


def test_local_adapter_is_atomic_confined_and_idempotent(tmp_path):
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_local import LocalObjectStorageAdapter

    adapter = LocalObjectStorageAdapter(
        root=tmp_path,
        endpoint_ref="primary",
        max_read_bytes=1024,
    )
    payload = b"immutable bytes"
    version = asyncio.run(
        adapter.put("libraries/lib/objects/hash.txt", payload, "text/plain")
    )
    assert version.object_version is None
    assert asyncio.run(adapter.read("libraries/lib/objects/hash.txt", None)) == payload
    stat = asyncio.run(adapter.stat("libraries/lib/objects/hash.txt", None))
    assert stat.size_bytes == len(payload)
    asyncio.run(adapter.put("libraries/lib/objects/hash.txt", payload, "text/plain"))
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            adapter.put(
                "libraries/lib/objects/hash.txt",
                b"different",
                "text/plain",
            )
        )
    assert exc_info.value.code == "object_identity_conflict"
    with pytest.raises(ObjectStorageError):
        asyncio.run(adapter.read("../outside", None))

    asyncio.run(adapter.delete("libraries/lib/objects/hash.txt", None))
    asyncio.run(adapter.delete("libraries/lib/objects/hash.txt", None))

    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_bytes(b"file")
    with pytest.raises(ObjectStorageError) as exc_info:
        LocalObjectStorageAdapter(
            root=invalid_root,
            endpoint_ref="primary",
            max_read_bytes=1024,
        )
    assert exc_info.value.code == "local_root_invalid"


def test_local_adapter_rejects_a_symlink_escape(tmp_path):
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_local import LocalObjectStorageAdapter

    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available on this Windows account")
    adapter = LocalObjectStorageAdapter(
        root=tmp_path,
        endpoint_ref="primary",
        max_read_bytes=1024,
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.put("linked/escape.txt", b"no", "text/plain"))
    assert exc_info.value.code == "unsafe_object_key"
    assert not (outside / "escape.txt").exists()


class _MemoryAdapter:
    provider = "minio"
    endpoint_ref = "primary"
    bucket = "documents"

    def __init__(self, *, corrupt_read=False):
        self.objects = {}
        self.corrupt_read = corrupt_read

    async def put(self, object_key, content, content_type):
        from app.services.object_storage_contracts import StorageObjectVersion

        self.objects[(object_key, "version-1")] = bytes(content)
        return StorageObjectVersion(object_version="version-1", etag="etag-1")

    async def read(self, object_key, object_version):
        value = self.objects[(object_key, object_version)]
        return value + b"corrupt" if self.corrupt_read else value

    async def stat(self, object_key, object_version):
        from app.services.object_storage_contracts import StorageObjectStat

        value = self.objects[(object_key, object_version)]
        return StorageObjectStat(
            size_bytes=len(value), object_version=object_version, etag="etag-1"
        )

    async def delete(self, object_key, object_version):
        self.objects.pop((object_key, object_version), None)

    async def download_url(self, object_key, object_version, expires_seconds):
        return "https://signed.invalid/never-persist-this"


def test_managed_capture_verifies_readback_and_hides_content_from_repr():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.revision_files import prepare_managed_revision_file_capture

    content = b"source document bytes"
    scope = {
        "library_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_revision_id": uuid.uuid4(),
    }
    prepared = asyncio.run(
        prepare_managed_revision_file_capture(
            adapter=_MemoryAdapter(),
            file_name="source.pdf",
            content_type="application/pdf",
            content=content,
            **scope,
        )
    )
    assert prepared.sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.locator.object_version == "version-1"
    assert prepared.locator.immutability_mode == "version_id"
    assert prepared.managed_snapshot is True
    assert content.decode() not in repr(prepared)
    assert "signed.invalid" not in repr(prepared)

    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            prepare_managed_revision_file_capture(
                adapter=_MemoryAdapter(corrupt_read=True),
                file_name="source.pdf",
                content_type="application/pdf",
                content=content,
                **scope,
            )
        )
    assert exc_info.value.code == "object_verification_failed"


def test_direct_capture_requires_an_exact_version_and_matching_readback():
    from app.schemas.storage import SourceLocatorV1, StorageLocatorV1
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.revision_files import prepare_direct_revision_file_capture

    content = b"source"
    adapter = _MemoryAdapter()
    asyncio.run(adapter.put("external/source", content, "application/octet-stream"))
    source = SourceLocatorV1(
        kind="external_object",
        provider="minio",
        endpoint_ref="primary",
        bucket="documents",
        object_key="external/source",
        object_version="version-1",
        etag="etag-1",
    )
    locator = StorageLocatorV1(
        provider="minio",
        endpoint_ref="primary",
        bucket="documents",
        object_key="external/source",
        object_version="version-1",
        etag="etag-1",
        immutability_mode="version_id",
    )
    prepared = asyncio.run(
        prepare_direct_revision_file_capture(
            adapter=adapter,
            library_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_revision_id=uuid.uuid4(),
            file_name="source.bin",
            content_type="application/octet-stream",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            expected_size_bytes=len(content),
            locator=locator,
            source_locator=source,
        )
    )
    assert prepared.managed_snapshot is False

    no_version = locator.model_copy(
        update={"object_version": None, "immutability_mode": "content_hash"}
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            prepare_direct_revision_file_capture(
                adapter=adapter,
                library_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_revision_id=uuid.uuid4(),
                file_name="source.bin",
                content_type=None,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size_bytes=len(content),
                locator=no_version,
                source_locator=source,
            )
        )
    assert exc_info.value.code == "immutable_version_required"

    mismatched_locator = locator.model_copy(update={"endpoint_ref": "secondary"})
    mismatched_source = source.model_copy(update={"endpoint_ref": "secondary"})
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            prepare_direct_revision_file_capture(
                adapter=adapter,
                library_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_revision_id=uuid.uuid4(),
                file_name="source.bin",
                content_type=None,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size_bytes=len(content),
                locator=mismatched_locator,
                source_locator=mismatched_source,
            )
        )
    assert exc_info.value.code == "storage_identity_mismatch"

    from app.services.object_storage_contracts import StorageObjectStat

    wrong_version = _MemoryAdapter()
    asyncio.run(wrong_version.put("external/source", content, None))
    wrong_version.stat = AsyncMock(
        return_value=StorageObjectStat(
            size_bytes=len(content),
            object_version="version-2",
            etag="etag-1",
        )
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            prepare_direct_revision_file_capture(
                adapter=wrong_version,
                library_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_revision_id=uuid.uuid4(),
                file_name="source.bin",
                content_type=None,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size_bytes=len(content),
                locator=locator,
                source_locator=source,
            )
        )
    assert exc_info.value.code == "object_verification_failed"


def test_remote_adapter_errors_are_bounded_and_do_not_leak_client_secrets():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_remote import MinioObjectStorageAdapter

    secret = "DO-NOT-LEAK"

    class FailingClient:
        def get_object(self, *args, **kwargs):
            raise RuntimeError(f"provider failure {secret}")

    adapter = MinioObjectStorageAdapter(
        client=FailingClient(),
        endpoint_ref="primary",
        bucket="documents",
        max_read_bytes=1024,
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.read("object", "v1"))
    assert exc_info.value.code == "provider_read_failed"
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert not hasattr(adapter, "access_key")


def test_remote_adapter_rejects_oversized_version_metadata():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_remote import MinioObjectStorageAdapter

    class Client:
        def put_object(self, *args, **kwargs):
            return SimpleNamespace(version_id="v" * 513, etag="etag")

    adapter = MinioObjectStorageAdapter(
        client=Client(),
        endpoint_ref="primary",
        bucket="documents",
        max_read_bytes=1024,
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.put("object", b"content", "text/plain"))
    assert exc_info.value.code == "provider_metadata_invalid"

    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.put("../unsafe", b"content", "text/plain"))
    assert exc_info.value.code == "unsafe_object_key"


def test_remote_adapter_maps_missing_objects_without_leaking_provider_errors():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_remote import MinioObjectStorageAdapter

    class MissingError(RuntimeError):
        code = "NoSuchKey"

    class Client:
        def get_object(self, *args, **kwargs):
            raise MissingError("secret provider diagnostic")

    adapter = MinioObjectStorageAdapter(
        client=Client(),
        endpoint_ref="primary",
        bucket="documents",
        max_read_bytes=1024,
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.read("object", "version-1"))
    assert exc_info.value.code == "object_not_found"
    assert "secret provider diagnostic" not in str(exc_info.value)


def test_minio_and_oss_adapters_bind_bucket_key_and_exact_version():
    from app.services.object_storage_remote import (
        MinioObjectStorageAdapter,
        OssObjectStorageAdapter,
    )

    class Response:
        def read(self, limit):
            assert limit == 1025
            return b"x"

        def close(self):
            return None

        def release_conn(self):
            return None

    class MinioClient:
        def __init__(self):
            self.calls = []

        def put_object(self, bucket, key, stream, size, content_type):
            self.calls.append(("put", bucket, key, size, content_type, stream.read()))
            return SimpleNamespace(version_id="v1", etag="e1")

        def get_object(self, bucket, key, version_id):
            self.calls.append(("read", bucket, key, version_id))
            return Response()

        def stat_object(self, bucket, key, version_id):
            self.calls.append(("stat", bucket, key, version_id))
            return SimpleNamespace(size=1, version_id="v1", etag="e1")

        def remove_object(self, bucket, key, version_id):
            self.calls.append(("delete", bucket, key, version_id))

        def presigned_get_object(self, bucket, key, expires, version_id):
            self.calls.append(("sign", bucket, key, expires, version_id))
            return "https://signed.invalid/minio"

    minio_client = MinioClient()
    minio = MinioObjectStorageAdapter(
        client=minio_client,
        endpoint_ref="primary",
        bucket="documents",
        max_read_bytes=1024,
    )
    version = asyncio.run(minio.put("objects/file", b"x", "text/plain"))
    assert version.object_version == "v1"
    assert asyncio.run(minio.read("objects/file", "v1")) == b"x"
    assert asyncio.run(minio.stat("objects/file", "v1")).object_version == "v1"
    asyncio.run(minio.delete("objects/file", "v1"))
    assert "signed.invalid" in asyncio.run(
        minio.download_url("objects/file", "v1", 60)
    )
    assert all(call[1:3] == ("documents", "objects/file") for call in minio_client.calls)
    assert all(call[3] == "v1" for call in minio_client.calls[1:4])

    class OssClient:
        def __init__(self):
            self.calls = []

        def put_object(self, key, content, headers):
            self.calls.append(("put", key, content, headers))
            return SimpleNamespace(versionid="v1", etag="e1")

        def get_object(self, key, params):
            self.calls.append(("read", key, params))
            return Response()

        def head_object(self, key, params):
            self.calls.append(("stat", key, params))
            return SimpleNamespace(content_length=1, versionid="v1", etag="e1")

        def delete_object(self, key, params):
            self.calls.append(("delete", key, params))

        def sign_url(self, method, key, expires, params):
            self.calls.append(("sign", method, key, expires, params))
            return "https://signed.invalid/oss"

    oss_client = OssClient()
    oss = OssObjectStorageAdapter(
        bucket_client=oss_client,
        endpoint_ref="primary",
        bucket="documents",
        max_read_bytes=1024,
    )
    version = asyncio.run(oss.put("objects/file", b"x", "text/plain"))
    assert version.object_version == "v1"
    assert asyncio.run(oss.read("objects/file", "v1")) == b"x"
    assert asyncio.run(oss.stat("objects/file", "v1")).object_version == "v1"
    asyncio.run(oss.delete("objects/file", "v1"))
    assert "signed.invalid" in asyncio.run(oss.download_url("objects/file", "v1", 60))
    assert [call[0] for call in oss_client.calls] == [
        "put",
        "read",
        "stat",
        "delete",
        "sign",
    ]
    for call in oss_client.calls[1:4]:
        assert call[2] == {"versionId": "v1"}


def test_prepared_capture_identity_projection_contains_no_runtime_access_values():
    from app.services.revision_files import prepared_capture_values

    prepared = SimpleNamespace(
        locator=SimpleNamespace(
            provider="local",
            endpoint_ref="primary",
            bucket=None,
            object_key="libraries/l/objects/hash.txt",
            object_version=None,
            etag=None,
            immutability_mode="content_hash",
        ),
        file_name="source.txt",
        content_type="text/plain",
        size_bytes=3,
        sha256="a" * 64,
        managed_snapshot=True,
        source_locator=None,
        verified_at=SimpleNamespace(),
    )
    values = prepared_capture_values(prepared)
    text = repr(values).lower()
    for forbidden in ("signed_url", "endpoint_url", "access_key", "secret_key"):
        assert forbidden not in text


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _DbResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return _Rows(self.rows)


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RevisionFileDb:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    def begin_nested(self):
        return _Nested()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


def test_revision_file_persistence_is_scope_fenced_idempotent_and_document_first():
    from app.services.revision_files import persist_revision_file_capture

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        latest_revision_id=revision_id,
        current_revision_id=None,
        deleted_at=None,
    )
    revision = SimpleNamespace(
        id=revision_id,
        library_id=library_id,
        document_id=document_id,
        status="pending",
    )
    prepared = asyncio.run(
        prepare_managed_capture_for_test(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        )
    )
    db = _RevisionFileDb(
        [_DbResult([document]), _DbResult([revision]), _DbResult([])]
    )
    row = asyncio.run(persist_revision_file_capture(db, prepared=prepared))
    assert row in db.added
    sql = [str(statement).lower() for statement in db.statements]
    assert "from documents" in sql[0]
    assert "from document_revisions" in sql[1]
    assert "from document_revision_files" in sql[2]

    duplicate_db = _RevisionFileDb(
        [_DbResult([document]), _DbResult([revision]), _DbResult([row])]
    )
    duplicate = asyncio.run(
        persist_revision_file_capture(duplicate_db, prepared=prepared)
    )
    assert duplicate is row
    assert duplicate_db.added == []

    retry_prepared = replace(
        prepared,
        locator=prepared.locator.model_copy(
            update={"object_version": "version-2", "etag": "etag-2"}
        ),
    )
    retry_db = _RevisionFileDb(
        [_DbResult([document]), _DbResult([revision]), _DbResult([row])]
    )
    assert (
        asyncio.run(
            persist_revision_file_capture(retry_db, prepared=retry_prepared)
        )
        is row
    )

    row.sha256 = "b" * 64
    conflict_db = _RevisionFileDb(
        [_DbResult([document]), _DbResult([revision]), _DbResult([row])]
    )
    from app.services.object_storage_contracts import ObjectStorageError

    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(persist_revision_file_capture(conflict_db, prepared=prepared))
    assert exc_info.value.code == "revision_file_identity_conflict"

    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(
            persist_revision_file_capture(
                conflict_db,
                prepared=SimpleNamespace(sha256="a" * 64, size_bytes=3),
            )
        )
    assert exc_info.value.code == "invalid_revision_file_capture"


async def prepare_managed_capture_for_test(
    *, library_id: uuid.UUID, document_id: uuid.UUID, revision_id: uuid.UUID
):
    from app.services.revision_files import prepare_managed_revision_file_capture

    return await prepare_managed_revision_file_capture(
        adapter=_MemoryAdapter(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        file_name="source.txt",
        content_type="text/plain",
        content=b"source",
    )


def test_import_storage_io_is_explicitly_before_the_library_write_lock():
    from app.api.documents import import_file

    source = inspect.getsource(import_file)
    rollback = source.index("await db.rollback()")
    prepare = source.index("await prepare_managed_file_object")
    lock = source.index("await _lock_writable(db, lib)", prepare)
    assert rollback < prepare < lock


def test_legacy_file_projection_uses_the_immutable_revision_file_winner(monkeypatch):
    from app.api import documents
    from app.config import settings

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    prepared = asyncio.run(
        prepare_managed_capture_for_test(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        )
    )
    winner = SimpleNamespace(
        file_name="original.txt",
        content_type="text/plain",
        object_key="libraries/lib/objects/original.txt",
        size_bytes=6,
        sha256="a" * 64,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=None), add=MagicMock())
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    with patch.object(
        documents,
        "persist_revision_file_capture",
        new=AsyncMock(return_value=winner),
    ):
        asyncio.run(
            documents._store_original_file_for_result(
                db,
                lib=SimpleNamespace(id=library_id),
                result={
                    "document_id": str(document_id),
                    "_revision": 1,
                    "_document_revision_id": str(revision_id),
                },
                filename="new-name.txt",
                content_type="application/octet-stream",
                content=b"source",
                prepared_file=prepared,
            )
        )

    projected = db.add.call_args.args[0]
    assert projected.file_name == "original.txt"
    assert projected.storage_path == winner.object_key
    assert projected.sha256 == winner.sha256


def test_structured_batch_import_rolls_back_only_the_failed_storage_item(
    tmp_path, monkeypatch
):
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    from app.api import documents
    from app.config import settings
    from app.models.library import Library
    from app.services.object_storage_contracts import ObjectStorageError

    library = SimpleNamespace(
        id=uuid.uuid4(),
        lifecycle_mode="managed",
        index_state="ready",
        deleted_at=None,
        chunk_size=1000,
        chunk_overlap=120,
    )
    user_id = uuid.uuid4()

    class ExpiringUser:
        expired = False

        @property
        def id(self):
            if self.expired:
                raise RuntimeError("expired ORM user was accessed after rollback")
            return user_id

    user = ExpiringUser()
    savepoint_exits = []

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            savepoint_exits.append(exc_type)
            return False

    async def rollback():
        user.expired = True

    db = SimpleNamespace(
        add=MagicMock(),
        begin_nested=MagicMock(side_effect=Savepoint),
        commit=AsyncMock(),
        rollback=AsyncMock(side_effect=rollback),
    )

    async def get(model, ident):
        del ident
        return library if model is Library else None

    async def execute(statement):
        del statement
        return _DbResult([library])

    db.get = get
    db.execute = execute
    docs = []
    jobs = []
    for title in ("Doc 1", "Doc 2"):
        docs.append(
            SimpleNamespace(
                id=uuid.uuid4(),
                status="pending",
                title=title,
                external_id=None,
                current_revision=1,
            )
        )
        jobs.append(
            SimpleNamespace(
                id=uuid.uuid4(),
                document_revision_id=uuid.uuid4(),
            )
        )
    upload = UploadFile(
        io.BytesIO(
            b'[{"title":"Doc 1","text":"one"},'
            b'{"title":"Doc 2","text":"two"}]'
        ),
        filename="documents.json",
        headers=Headers({"content-type": "application/json"}),
    )
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    monkeypatch.setattr(settings, "document_storage_provider", "local")
    monkeypatch.setattr(settings, "document_storage_endpoint_ref", "primary")
    monkeypatch.setattr(settings, "document_files_dir", str(tmp_path))
    monkeypatch.setattr(settings, "document_storage_max_read_bytes", 1024 * 1024)
    ingest = AsyncMock(
        side_effect=[
            (docs[0], jobs[0], 1, False),
            (docs[1], jobs[1], 1, False),
        ]
    )
    persist = AsyncMock(
        side_effect=[
            ObjectStorageError("provider_write_failed", "sanitized"),
            SimpleNamespace(
                file_name="documents.json",
                content_type="application/json",
                object_key="libraries/lib/objects/documents.json",
                size_bytes=upload.size or len(upload.file.getvalue()),
                sha256="a" * 64,
            ),
        ]
    )
    with (
        patch.object(documents.ingest_service, "ingest_text", new=ingest),
        patch.object(documents, "persist_revision_file_capture", new=persist),
    ):
        result = asyncio.run(
            documents.import_file(
                file=upload,
                external_id=None,
                replace_document_id=None,
                lib=library,
                user=user,
                db=db,
            )
        )

    assert result["status"] == "partial"
    assert result["imported_count"] == 1
    assert result["failed_count"] == 1
    assert result["documents"][0]["title"] == "Doc 2"
    assert savepoint_exits == [ObjectStorageError, None]
    assert db.begin_nested.call_count == 2
    db.commit.assert_awaited_once()
    assert all(call.kwargs["created_by"] == user_id for call in ingest.await_args_list)


def test_remote_sdk_imports_are_lazy_and_packaged_as_one_optional_extra():
    source = Path("app/services/object_storage.py").read_text(encoding="utf-8")
    assert source.index("def build_object_storage_adapter") < source.index(
        "from minio import Minio"
    )
    assert source.index("def build_object_storage_adapter") < source.index(
        "import oss2"
    )
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "object-storage = [" in pyproject
    assert '"minio>=7.2"' in pyproject
    assert '"oss2>=2.18"' in pyproject


def test_api_lifespan_calls_revision_file_storage_startup_validator(monkeypatch):
    import app.main as main

    calls = []
    monkeypatch.setattr(
        main,
        "validate_revision_file_storage_startup",
        lambda config: calls.append(config),
    )
    main.assert_revision_file_storage_startup_security()
    assert calls == [main.settings]
    assert "assert_revision_file_storage_startup_security" in inspect.getsource(
        main.lifespan
    )


def test_enabled_local_storage_is_built_during_startup(monkeypatch):
    import app.main as main

    build = MagicMock()
    monkeypatch.setattr(main.settings, "revision_file_storage_enabled", True)
    monkeypatch.setattr(main.settings, "document_storage_provider", "local")
    monkeypatch.setattr(main, "validate_revision_file_storage_startup", lambda config: None)
    with patch("app.services.object_storage.build_object_storage_adapter", new=build):
        main.assert_revision_file_storage_startup_security()
    build.assert_called_once_with(main.settings)


def test_structured_local_download_uses_the_current_revision_file(tmp_path, monkeypatch):
    from app.api.documents import download_document_file
    from app.config import settings
    from app.services.object_storage_local import LocalObjectStorageAdapter

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    content = b"current revision bytes"
    adapter = LocalObjectStorageAdapter(
        root=tmp_path,
        endpoint_ref="primary",
        max_read_bytes=1024,
    )
    object_key = "libraries/lib/objects/hash.txt"
    asyncio.run(adapter.put(object_key, content, "text/plain"))
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        current_revision_id=revision_id,
        deleted_at=None,
    )
    row = SimpleNamespace(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        file_name="source.txt",
        content_type="text/plain",
        storage_provider="local",
        endpoint_ref="primary",
        bucket=None,
        object_key=object_key,
        object_version=None,
        etag=None,
        immutability_mode="content_hash",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=document)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    with (
        patch(
            "app.api.documents.current_revision_file",
            new=AsyncMock(return_value=row),
        ),
        patch(
            "app.api.documents.build_object_storage_adapter",
            return_value=adapter,
        ),
    ):
        response = asyncio.run(
            download_document_file(
                document_id,
                lib=SimpleNamespace(id=library_id),
                db=db,
            )
        )
    assert response.body == content
    db.rollback.assert_awaited_once()


def test_structured_download_hash_mismatch_returns_generic_502(monkeypatch):
    from fastapi import HTTPException

    from app.api.documents import download_document_file
    from app.config import settings

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    content = b"expected bytes"
    adapter = _MemoryAdapter(corrupt_read=True)
    object_key = "libraries/lib/objects/hash.txt"
    asyncio.run(adapter.put(object_key, content, "text/plain"))
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        current_revision_id=revision_id,
        deleted_at=None,
    )
    row = SimpleNamespace(
        file_name="source.txt",
        content_type="text/plain",
        storage_provider="minio",
        endpoint_ref="primary",
        bucket="documents",
        object_key=object_key,
        object_version="version-1",
        etag="etag-1",
        immutability_mode="version_id",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=document)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    with (
        patch(
            "app.api.documents.current_revision_file",
            new=AsyncMock(return_value=row),
        ),
        patch(
            "app.api.documents.build_object_storage_adapter",
            return_value=adapter,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(
            download_document_file(
                document_id,
                lib=SimpleNamespace(id=library_id),
                db=db,
            )
        )
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "stored document file is unavailable"


def test_revision_file_access_survives_session_expiration():
    from app.services.revision_files import revision_file_access_from_row

    row = SimpleNamespace(
        file_name="source.txt",
        content_type="text/plain",
        storage_provider="local",
        endpoint_ref="primary",
        bucket=None,
        object_key="libraries/lib/objects/hash.txt",
        object_version=None,
        etag=None,
        immutability_mode="content_hash",
        size_bytes=3,
        sha256="a" * 64,
    )
    access = revision_file_access_from_row(row)
    row.file_name = None
    row.object_key = None
    row.sha256 = None
    assert access.file_name == "source.txt"
    assert access.locator.object_key == "libraries/lib/objects/hash.txt"
    assert access.sha256 == "a" * 64
