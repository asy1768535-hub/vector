from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import knowledge_catalog as api
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.knowledge_catalog import CatalogDocumentPageRead
from app.schemas.storage import StorageLocatorV1
from app.services.knowledge_catalog import PreparedCatalogFileAccess
from app.services.knowledge_catalog_contracts import KnowledgeCatalogError
from app.services.revision_files import RevisionFileAccess


NOW = datetime(2026, 7, 22, 23, 45, tzinfo=timezone.utc)
HASH = "a" * 64


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=NOW,
    )


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="catalog",
        name="Catalog",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="catalog",
        index_state="ready",
        created_at=NOW,
    )


def _override_library(user: User, library: Library, db):
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return patch("app.deps.authorize_library", new=AsyncMock(return_value=library))


def test_catalog_routes_default_off_before_library_authorization():
    with patch("app.deps.authorize_library", new=AsyncMock()) as authorize:
        response = TestClient(app).get("/libraries/secret/catalog/documents")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    authorize.assert_not_awaited()


def test_catalog_list_uses_existing_library_read_boundary():
    user, library, db = _user(), _library(), AsyncMock()
    page = CatalogDocumentPageRead(items=[], total=0, next_cursor=None)
    _override_library(user, library, db)
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch("app.deps.authorize_library", new=AsyncMock(return_value=library)) as authorize,
            patch.object(api, "list_catalog_documents", new=AsyncMock(return_value=page)) as read,
        ):
            response = TestClient(app).get("/libraries/catalog/catalog/documents?limit=10")
        assert response.status_code == 200, response.text
        assert response.json()["contract_version"] == "catalog-documents-v1"
        authorize.assert_awaited_once()
        read.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_catalog_errors_are_bounded_and_cross_scope_is_generic():
    user, library, db = _user(), _library(), AsyncMock()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch("app.deps.authorize_library", new=AsyncMock(return_value=library)),
            patch.object(
                api,
                "get_catalog_evidence_detail",
                new=AsyncMock(side_effect=KnowledgeCatalogError("catalog_not_found")),
            ),
        ):
            response = TestClient(app).get(
                f"/libraries/catalog/catalog/evidence/{uuid.uuid4()}"
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
    finally:
        app.dependency_overrides.clear()


def test_file_access_releases_db_before_signing_and_hides_locator():
    user, library, db = _user(), _library(), AsyncMock()
    file_id, document_id, revision_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    prepared = PreparedCatalogFileAccess(
        revision_file_id=file_id,
        document_id=document_id,
        document_revision_id=revision_id,
        access=RevisionFileAccess(
            file_name="contract.pdf",
            content_type="application/pdf",
            size_bytes=12,
            sha256=HASH,
            locator=StorageLocatorV1(
                provider="minio",
                endpoint_ref="primary",
                bucket="private",
                object_key="tenant/secret-object",
                object_version="v1",
                etag="etag",
                immutability_mode="version_id",
            ),
            lifecycle_status="available",
        ),
    )
    events = []
    db.rollback = AsyncMock(side_effect=lambda: events.append("rollback"))

    class Adapter:
        provider = "minio"
        endpoint_ref = "primary"
        bucket = "private"

        async def download_url(self, object_key, object_version, expires_seconds):
            assert events == ["rollback"]
            assert (object_key, object_version, expires_seconds) == (
                "tenant/secret-object",
                "v1",
                300,
            )
            events.append("sign")
            return "https://signed.invalid/access"

    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch.object(settings, "revision_file_storage_enabled", True),
            patch("app.deps.authorize_library", new=AsyncMock(return_value=library)),
            patch.object(
                api,
                "prepare_catalog_file_access",
                new=AsyncMock(return_value=prepared),
            ),
            patch.object(api, "build_object_storage_adapter", return_value=Adapter()),
        ):
            response = TestClient(app).get(
                f"/libraries/catalog/catalog/files/{file_id}/access"
            )
        assert response.status_code == 200, response.text
        assert events == ["rollback", "sign"]
        payload = response.json()
        assert payload["access_mode"] == "signed_url"
        serialized = str(payload)
        for forbidden in (
            "endpoint_ref",
            "bucket",
            "object_key",
            "object_version",
            "etag",
            "tenant/secret-object",
        ):
            assert forbidden not in serialized
    finally:
        app.dependency_overrides.clear()
