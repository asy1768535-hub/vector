from __future__ import annotations

import asyncio
import ssl
import sys
import types
import uuid
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from pydantic import ValidationError


def test_operator_minio_environment_maps_to_document_storage(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MINIO_ENDPOINT", "minio.gshbzw.com")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "vector-database")
    monkeypatch.setenv("MINIO_SECRET_KEY", "placeholder-secret")
    monkeypatch.setenv("MINIO_BUCKET", "vector-database-raw")
    monkeypatch.setenv("MINIO_SECURE", "true")

    config = Settings(_env_file=None)

    assert config.document_storage_provider == "minio"
    assert config.document_storage_endpoint_url == "https://minio.gshbzw.com"
    assert config.document_storage_bucket == "vector-database-raw"
    assert config.document_storage_access_key.get_secret_value() == "vector-database"
    assert str(config.document_storage_secret_key) == "**********"
    assert "placeholder-secret" not in repr(config)


def test_explicit_local_provider_cannot_silently_ignore_minio_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DOCUMENT_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("MINIO_ENDPOINT", "minio.example.invalid")

    with pytest.raises(ValidationError, match="MINIO_ENDPOINT"):
        Settings(_env_file=None)


def test_host_only_minio_endpoint_is_safe_for_adapter_construction(monkeypatch):
    from app.config import Settings
    from app.services.object_storage import build_object_storage_adapter

    class FakeMinio:
        def __init__(self, endpoint, *, access_key, secret_key, secure, region):
            self.endpoint = endpoint
            self.access_key = access_key
            self.secret_key = secret_key
            self.secure = secure
            self.region = region

    monkeypatch.setitem(sys.modules, "minio", types.SimpleNamespace(Minio=FakeMinio))
    config = Settings(
        _env_file=None,
        document_storage_provider="minio",
        document_storage_endpoint_url="minio.gshbzw.com",
        document_storage_bucket="vector-database-raw",
        document_storage_access_key="vector-database",
        document_storage_secret_key="placeholder-secret",
    )

    adapter = build_object_storage_adapter(config)

    assert adapter._client.endpoint == "minio.gshbzw.com"
    assert adapter._client.secure is True
    assert adapter.bucket == "vector-database-raw"


def test_storage_adapter_can_read_legacy_local_resources_after_minio_cutover(
    tmp_path, monkeypatch
):
    from app.config import Settings
    from app.services.object_storage import build_object_storage_adapter
    from app.services.object_storage_local import LocalObjectStorageAdapter

    config = Settings(
        _env_file=None,
        document_storage_provider="minio",
        document_storage_endpoint_url="https://minio.example.invalid",
        document_storage_bucket="vector-database-raw",
        document_storage_access_key="vector-database",
        document_storage_secret_key="placeholder-secret",
        document_files_dir=str(tmp_path),
    )

    adapter = build_object_storage_adapter(config, provider="local")

    assert isinstance(adapter, LocalObjectStorageAdapter)


def test_startup_storage_probe_runs_for_remote_provider(monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(main.settings, "document_storage_provider", "minio")
    monkeypatch.setattr(main.settings, "revision_file_storage_enabled", False)

    async def fake_prepare(config):
        calls.append(config)

    import app.services.deployment_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "prepare_document_storage", fake_prepare)
    asyncio.run(main.check_object_storage_startup())

    assert calls == [main.settings]


def test_startup_storage_probe_hides_provider_error_details(monkeypatch):
    from app import main
    from app.services.deployment_bootstrap import DeploymentBootstrapError

    monkeypatch.setattr(main.settings, "document_storage_provider", "minio")
    monkeypatch.setattr(main.settings, "revision_file_storage_enabled", False)

    async def fake_prepare(_config):
        raise DeploymentBootstrapError("remote failure secret=should-not-escape")

    import app.services.deployment_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "prepare_document_storage", fake_prepare)
    try:
        asyncio.run(main.check_object_storage_startup())
    except RuntimeError as exc:
        assert str(exc) == (
            "[storage] configured object storage is unavailable: provider_unavailable"
        )
        assert "should-not-escape" not in str(exc)
    else:
        raise AssertionError("startup probe unexpectedly succeeded")


def test_startup_storage_probe_exposes_sanitized_failure_category(monkeypatch):
    from app import main
    from app.services.deployment_bootstrap import DeploymentBootstrapError

    monkeypatch.setattr(main.settings, "document_storage_provider", "minio")
    monkeypatch.setattr(main.settings, "revision_file_storage_enabled", False)

    async def fake_prepare(_config):
        raise DeploymentBootstrapError(
            "remote document storage probe failed: authentication_error"
        )

    import app.services.deployment_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "prepare_document_storage", fake_prepare)
    with pytest.raises(RuntimeError, match="authentication_error"):
        asyncio.run(main.check_object_storage_startup())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("network down"), "network_error"),
        (ssl.SSLError("bad certificate"), "tls_error"),
        (types.SimpleNamespace(code="InvalidAccessKeyId"), "authentication_error"),
        (types.SimpleNamespace(code="AccessDenied"), "permission_denied"),
        (types.SimpleNamespace(code="NoSuchBucket"), "bucket_not_found"),
    ],
)
def test_startup_storage_probe_classifies_operator_action(error, expected):
    from app.services.object_storage_remote import classify_remote_storage_error

    assert classify_remote_storage_error(error) == expected


def test_minio_permission_failure_has_a_stable_sanitized_code():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.object_storage_remote import MinioObjectStorageAdapter

    class AccessDenied(Exception):
        code = "AccessDenied"

    class Client:
        def put_object(self, *args, **kwargs):
            raise AccessDenied("secret provider detail")

    adapter = MinioObjectStorageAdapter(
        client=Client(),
        endpoint_ref="primary",
        bucket="vector-database-raw",
        max_read_bytes=1024,
    )
    with pytest.raises(ObjectStorageError) as exc_info:
        asyncio.run(adapter.put("files/a", b"x", "text/plain"))

    assert exc_info.value.code == "provider_permission_denied"
    assert "secret provider detail" not in str(exc_info.value)


def test_minio_import_file_queues_processing_without_parsing(monkeypatch):
    from app.api import documents

    user = SimpleNamespace(id=uuid.uuid4())
    library = SimpleNamespace(id=uuid.uuid4())
    queued = {"id": uuid.uuid4(), "status": "queued", "file_status": "available"}
    complete_direct_upload = AsyncMock(return_value=SimpleNamespace(id=queued["id"]))
    projection = AsyncMock(return_value=queued)
    monkeypatch.setattr(documents.settings, "document_storage_provider", "minio")
    monkeypatch.setattr(
        documents,
        "import_uploads",
        SimpleNamespace(
            complete_direct_upload=complete_direct_upload,
            job_projection=projection,
            ImportUploadError=RuntimeError,
        ),
        raising=False,
    )
    upload = UploadFile(BytesIO(b"source bytes"), filename="源文件.txt")

    result = asyncio.run(
        documents.import_file(
            file=upload,
            external_id=None,
            replace_document_id=None,
            visibility_scope=None,
            security_level=None,
            graph_extraction_requested=False,
            lib=library,
            user=user,
            db=SimpleNamespace(),
        )
    )

    assert result == queued
    call = complete_direct_upload.await_args
    assert call.kwargs["payload"].file_name == "源文件.txt"
    assert call.kwargs["content"] == b"source bytes"
    projection.assert_awaited_once()


def test_direct_upload_reuses_resumable_flow_in_bounded_chunks(monkeypatch):
    from app.services import import_uploads

    job = SimpleNamespace(id=uuid.uuid4(), upload_offset=0)
    library = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    payload = SimpleNamespace(size_bytes=10)
    config = SimpleNamespace(import_upload_chunk_bytes=4)
    create = AsyncMock(return_value=job)
    claims = [
        SimpleNamespace(already_queued=False),
        SimpleNamespace(already_queued=False),
        SimpleNamespace(already_queued=False),
        SimpleNamespace(already_queued=False),
    ]
    claim = AsyncMock(side_effect=claims)
    seen = []

    async def append(_db, *, body, claim, config):
        del claim, config
        chunk = b"".join([part async for part in body])
        seen.append(chunk)
        return sum(len(part) for part in seen)

    complete = AsyncMock()
    owned = AsyncMock(return_value=job)
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(import_uploads, "create_session", create)
    monkeypatch.setattr(import_uploads, "claim_upload_operation", claim)
    monkeypatch.setattr(import_uploads, "append_claimed_content", append)
    monkeypatch.setattr(import_uploads, "complete_claimed_upload", complete)
    monkeypatch.setattr(import_uploads, "get_owned_job", owned)

    result = asyncio.run(
        import_uploads.complete_direct_upload(
            db,
            library=library,
            user=user,
            payload=payload,
            content=b"0123456789",
            config=config,
        )
    )

    assert result is job
    assert seen == [b"0123", b"4567", b"89"]
    assert [call.kwargs["operation"] for call in claim.await_args_list] == [
        "content",
        "content",
        "content",
        "complete",
    ]
    complete.assert_awaited_once()
