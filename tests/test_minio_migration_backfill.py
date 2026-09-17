from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


def _migration_module():
    path = Path(__file__).parents[1] / "scripts" / "migrate_local_file_resources_to_minio.py"
    spec = importlib.util.spec_from_file_location("minio_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_minio_resource_builds_revision_capture_without_copying_object():
    module = _migration_module()
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    verified_at = datetime.now(timezone.utc)
    resource = SimpleNamespace(
        library_id=library_id,
        file_name="source.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=42,
        sha256="a" * 64,
        storage_provider="minio",
        endpoint_ref="primary",
        bucket="vector-database-raw",
        object_key=f"libraries/{library_id}/objects/{'a' * 64}.docx",
        object_version=None,
        etag="etag",
        immutability_mode="content_hash",
        storage_verified_at=verified_at,
    )
    job = SimpleNamespace(document_id=document_id, document_revision_id=revision_id)

    capture = module.revision_capture_from_resource(job, resource)

    assert capture.document_id == document_id
    assert capture.document_revision_id == revision_id
    assert capture.locator.object_key == resource.object_key
    assert capture.locator.provider == "minio"
    assert capture.verified_at == verified_at
