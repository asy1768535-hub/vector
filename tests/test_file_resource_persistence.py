from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import UniqueConstraint

from app.models.file_resource import FileResource
from app.services import import_uploads
from app.services.file_resources import (
    build_file_resource,
    build_storing_file_resource,
    delete_file_resource_object,
    file_resource_download_url,
    materialize_file_resource,
    prepare_file_resource,
    resource_object_key,
    store_file_resource_object,
)
from app.services.object_storage_contracts import (
    ObjectStorageError,
    StorageObjectStat,
    StorageObjectVersion,
)


class _MemoryAdapter:
    provider = "local"
    endpoint_ref = "primary"
    bucket = None

    def __init__(self, *, corrupt: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.corrupt = corrupt

    async def put(self, object_key: str, content: bytes, content_type: str | None):
        self.objects[object_key] = content
        return StorageObjectVersion(object_version=None, etag=None)

    async def put_file(self, object_key: str, source_path: Path, content_type: str | None):
        content = source_path.read_bytes()
        if self.corrupt:
            content = b"x" * len(content)
        self.objects[object_key] = content
        return StorageObjectVersion(object_version=None, etag=None)

    async def read(self, object_key: str, object_version: str | None) -> bytes:
        return self.objects[object_key]

    async def stat(self, object_key: str, object_version: str | None):
        content = self.objects[object_key]
        return StorageObjectStat(
            size_bytes=len(content), object_version=None, etag=None
        )

    async def delete(self, object_key: str, object_version: str | None) -> None:
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)

    async def download_url(
        self, object_key: str, object_version: str | None, expires_seconds: int
    ) -> str:
        return f"https://minio.example.test/{object_key}?expires={expires_seconds}"

    async def materialize(
        self, object_key: str, object_version: str | None, destination_path: Path
    ) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(self.objects[object_key])



def test_resource_object_key_is_scoped_to_upload_context() -> None:
    library_id = uuid.uuid4()
    first = resource_object_key(library_id, uuid.uuid4(), "合同.pdf")
    second = resource_object_key(library_id, uuid.uuid4(), "合同.pdf")

    assert first != second
    assert first.startswith(f"libraries/{library_id}/file-resources/")
    assert first.endswith("/original.pdf")
    assert "/objects/" not in first


def test_file_resource_model_has_independent_storage_contract() -> None:
    from app.models.document_import_job import DocumentImportJob

    columns = FileResource.__table__.columns
    assert {
        "file_name",
        "relative_path",
        "library_id",
        "uploaded_by_user_id",
        "size_bytes",
        "sha256",
        "storage_path",
        "storage_status",
        "storage_verified_at",
    } <= set(columns.keys())
    assert "document_id" not in columns
    assert "document_revision_id" not in columns
    assert columns.storage_status.default.arg == "storing"
    assert "sha256" not in {
        column.name
        for constraint in FileResource.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        for column in constraint.columns
    }
    assert "uq_document_import_jobs_file_resource" in {
        constraint.name
        for constraint in DocumentImportJob.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    migration = Path("alembic/versions/0076_file_resource_persistence.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0076"' in migration
    assert 'down_revision = "0075"' in migration
    assert 'op.create_table(\n        "file_resources"' in migration
    assert 'op.add_column(\n        "document_import_jobs"' in migration
    assert "uq_document_import_jobs_file_resource" in migration


def test_prepare_file_resource_verifies_size_and_sha(tmp_path: Path) -> None:
    content = "原始文件".encode()
    source = tmp_path / "合同.pdf"
    source.write_bytes(content)
    library_id = uuid.uuid4()
    upload_context_id = uuid.uuid4()
    adapter = _MemoryAdapter()

    prepared = asyncio.run(
        prepare_file_resource(
            adapter=adapter,
            library_id=library_id,
            upload_context_id=upload_context_id,
            file_name="合同.pdf",
            content_type="application/pdf",
            relative_path="资料/合同.pdf",
            source_path=source,
            expected_size_bytes=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    )

    assert prepared.size_bytes == len(content)
    assert prepared.sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.locator.object_key in adapter.objects
    resource = build_file_resource(
        prepared,
        library_id=library_id,
        uploaded_by_user_id=uuid.uuid4(),
        file_name="合同.pdf",
        relative_path="资料/合同.pdf",
    )
    assert isinstance(resource, FileResource)
    assert resource.storage_status == "available"
    assert resource.object_key == prepared.locator.object_key


@pytest.mark.parametrize("bad_field", ["size", "sha"])
def test_prepare_file_resource_rejects_verification_mismatch(
    tmp_path: Path, bad_field: str
) -> None:
    content = b"resource-content"
    source = tmp_path / "sample.txt"
    source.write_bytes(content)
    adapter = _MemoryAdapter()
    expected_size = len(content) + 1 if bad_field == "size" else len(content)
    expected_sha = "0" * 64 if bad_field == "sha" else hashlib.sha256(content).hexdigest()

    with pytest.raises(ObjectStorageError):
        asyncio.run(
            prepare_file_resource(
                adapter=adapter,
                library_id=uuid.uuid4(),
                upload_context_id=uuid.uuid4(),
                file_name="sample.txt",
                content_type="text/plain",
                source_path=source,
                expected_size_bytes=expected_size,
                expected_sha256=expected_sha,
            )
        )


def test_prepare_file_resource_does_not_mark_corrupt_object_available(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"resource-content")
    adapter = _MemoryAdapter(corrupt=True)

    with pytest.raises(ObjectStorageError):
        asyncio.run(
            prepare_file_resource(
                adapter=adapter,
                library_id=uuid.uuid4(),
                upload_context_id=uuid.uuid4(),
                file_name="sample.txt",
                content_type="text/plain",
                source_path=source,
                expected_size_bytes=source.stat().st_size,
                expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        )

    assert adapter.deleted


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _CommitFailureDb:
    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value) -> None:
        self.added.append(value)

    async def execute(self, _statement):
        return _Result(self.job_id)

    async def commit(self):
        self.commits += 1
        if self.commits == 2:
            raise ConnectionError("database commit result unknown")
    async def rollback(self):
        self.rollbacks += 1
        self.added.clear()


class _NoopLease:
    thread_stop_event = None
    _lost_error = None

    def ensure_current(self) -> None:
        return None

    async def stop_renewal(self) -> None:
        return None


@asynccontextmanager
async def _noop_claim_lease(*_args, **_kwargs):
    yield _NoopLease()


def test_database_failure_keeps_written_resource_object_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"resource-content"
    job_id = uuid.uuid4()
    claim = import_uploads.UploadOperationClaim(
        job_id=job_id,
        owner_token="upload:owner:complete:token",
        operation="complete",
        staging_key=f"{job_id.hex}.upload",
        file_name="sample.txt",
        size_bytes=len(content),
        upload_offset=len(content),
        library_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        relative_path=None,
        content_type="text/plain",
    )
    staging = tmp_path / claim.staging_key
    staging.write_bytes(content)
    adapter = _MemoryAdapter()
    config = SimpleNamespace(
        import_upload_staging_dir=str(tmp_path),
        import_staging_dir=str(tmp_path),
        import_upload_retry_after_seconds=2,
        import_upload_claim_heartbeat_seconds=30,
        import_upload_claim_stale_seconds=300,
        document_storage_provider="local",
        document_files_dir=str(tmp_path / "objects"),
        document_storage_endpoint_ref="primary",
        document_storage_max_read_bytes=1024 * 1024,
    )
    db = _CommitFailureDb(job_id)

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(
        import_uploads, "build_object_storage_adapter", lambda _config: adapter
    )

    with pytest.raises(ConnectionError, match="commit result unknown"):
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db, claim=claim, config=config
            )
        )

    assert adapter.deleted == []
    assert len(adapter.objects) == 1
    assert db.rollbacks >= 1

def test_materialize_available_file_resource_to_worker_input(tmp_path: Path) -> None:
    content = b"worker-input"
    adapter = _MemoryAdapter()
    library_id = uuid.uuid4()
    upload_context_id = uuid.uuid4()
    resource = build_storing_file_resource(
        library_id=library_id,
        uploaded_by_user_id=uuid.uuid4(),
        upload_context_id=upload_context_id,
        file_name="sample.txt",
        content_type="text/plain",
        relative_path=None,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        adapter=adapter,
    )
    resource.storage_status = "available"
    adapter.objects[resource.object_key] = content
    destination = tmp_path / "worker" / "sample.txt"

    asyncio.run(
        materialize_file_resource(
            adapter=adapter,
            resource=resource,
            destination_path=destination,
        )
    )

    assert destination.read_bytes() == content


def test_download_url_requires_a_verified_resource_and_preserves_its_object_key() -> None:
    content = b"download-me"
    adapter = _MemoryAdapter()
    resource = build_storing_file_resource(
        library_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        upload_context_id=uuid.uuid4(),
        file_name="sample.txt",
        content_type="text/plain",
        relative_path=None,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        adapter=adapter,
    )
    resource.storage_status = "available"

    url = asyncio.run(
        file_resource_download_url(
            adapter=adapter,
            resource=resource,
            expires_seconds=300,
        )
    )

    assert resource.object_key in url
    assert "expires=300" in url

    resource.storage_status = "deleting"
    with pytest.raises(ObjectStorageError, match="not available"):
        asyncio.run(
            file_resource_download_url(
                adapter=adapter,
                resource=resource,
                expires_seconds=300,
            )
        )

def test_existing_resource_object_is_not_overwritten_on_retry(tmp_path: Path) -> None:
    content = b"new-content"
    source = tmp_path / "sample.txt"
    source.write_bytes(content)
    adapter = _MemoryAdapter()
    resource = build_storing_file_resource(
        library_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        upload_context_id=uuid.uuid4(),
        file_name="sample.txt",
        content_type="text/plain",
        relative_path=None,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        adapter=adapter,
    )
    original = b"different-content"
    adapter.objects[resource.object_key] = original

    with pytest.raises(ObjectStorageError, match="verification failed"):
        asyncio.run(
            store_file_resource_object(
                adapter=adapter,
                resource=resource,
                source_path=source,
            )
        )

    assert adapter.objects[resource.object_key] == original
    assert adapter.deleted == []


def test_explicit_resource_delete_removes_object_and_is_idempotent() -> None:
    content = b"delete-me"
    adapter = _MemoryAdapter()
    resource = build_storing_file_resource(
        library_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        upload_context_id=uuid.uuid4(),
        file_name="sample.txt",
        content_type="text/plain",
        relative_path=None,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        adapter=adapter,
    )
    resource.storage_status = "available"
    adapter.objects[resource.object_key] = content

    asyncio.run(delete_file_resource_object(adapter=adapter, resource=resource))
    asyncio.run(delete_file_resource_object(adapter=adapter, resource=resource))

    assert resource.storage_status == "deleted"
    assert resource.object_key not in adapter.objects
    assert adapter.deleted == [resource.object_key]


def test_importer_reuses_file_resource_object_for_revision_without_second_upload(
    monkeypatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.workers import importer

    resource = SimpleNamespace(
        library_id=uuid.uuid4(),
        file_name="源文件.txt",
        content_type="text/plain",
        size_bytes=6,
        sha256=hashlib.sha256(b"source").hexdigest(),
        storage_provider="minio",
        endpoint_ref="primary",
        bucket="vector-database-raw",
        object_key="libraries/lib/file-resources/upload/original.txt",
        object_version=None,
        etag="etag",
        immutability_mode="content_hash",
        storage_verified_at=SimpleNamespace(),
    )
    snapshot = SimpleNamespace(
        library_id=resource.library_id,
        file_resource=resource,
        file_name=resource.file_name,
        content_type=resource.content_type,
        sha256=resource.sha256,
    )
    upload_again = AsyncMock()
    monkeypatch.setattr(importer.settings, "revision_file_storage_enabled", True)
    monkeypatch.setattr(importer, "prepare_managed_file_path", upload_again)

    prepared = asyncio.run(
        importer._prepare_revision_file(snapshot, tmp_path / "unused.txt")
    )

    assert prepared.locator.object_key == resource.object_key
    assert prepared.sha256 == resource.sha256
    upload_again.assert_not_awaited()
