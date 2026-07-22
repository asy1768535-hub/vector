from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import knowledge_catalog as api
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.document_processing import DocumentProcessingRead
from app.services.document_processing_contracts import DocumentProcessingError
from app.services.organization_authorization import OrganizationAuthorizationError


NOW = datetime(2026, 7, 22, 11, 30, tzinfo=timezone.utc)


def _user(*, superuser: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=superuser,
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


def _processing(library: Library, document_id: uuid.UUID) -> DocumentProcessingRead:
    return DocumentProcessingRead(
        library_id=library.id,
        document_id=document_id,
        document_revision_id=uuid.uuid4(),
        stages=[
            {
                "stage": stage,
                "availability": "enabled",
                "status": "not_started",
                "job_id": None,
                "retry_generation": 0,
                "attempt_count": None,
                "safe_error_code": None,
                "retryable": False,
                "created_at": None,
                "started_at": None,
                "updated_at": None,
                "finished_at": None,
                "graph_counts": None,
            }
            for stage in ("summary", "outline", "classification", "graph")
        ],
    )


def _overrides(user: User, db) -> None:
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


def test_processing_diagnostics_are_default_off_before_library_lookup():
    with patch.object(api.deps_module, "load_active_library", new=AsyncMock()) as load:
        response = TestClient(app).get(
            f"/libraries/secret/catalog/documents/{uuid.uuid4()}/processing"
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    load.assert_not_awaited()


def test_management_read_uses_exact_authorization_and_bounded_schema():
    user, library, db = _user(), _library(), AsyncMock()
    document_id = uuid.uuid4()
    _overrides(user, db)
    try:
        with (
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch.object(api.deps_module, "load_active_library", new=AsyncMock(return_value=library)),
            patch.object(api, "authorize_library_management", new=AsyncMock()) as authorize,
            patch.object(
                api,
                "get_document_processing_diagnostics",
                new=AsyncMock(return_value=_processing(library, document_id)),
            ) as read,
        ):
            response = TestClient(app).get(
                f"/libraries/catalog/catalog/documents/{document_id}/processing"
            )
        assert response.status_code == 200, response.text
        assert response.json()["contract_version"] == "document-processing-v1"
        authorize.assert_awaited_once()
        read.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_platform_superuser_cannot_bypass_customer_management_authorization():
    user, library, db = _user(superuser=True), _library(), AsyncMock()
    _overrides(user, db)
    try:
        with (
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch.object(settings, "organization_authorization_enabled", True),
            patch.object(api.deps_module, "load_active_library", new=AsyncMock(return_value=library)),
            patch.object(
                api,
                "authorize_library_management",
                new=AsyncMock(side_effect=OrganizationAuthorizationError("organization_forbidden")),
            ),
            patch.object(api, "get_document_processing_diagnostics", new=AsyncMock()) as read,
        ):
            response = TestClient(app).get(
                f"/libraries/catalog/catalog/documents/{uuid.uuid4()}/processing"
            )
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
        read.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_retry_commits_exact_fenced_request_and_hides_native_failure():
    user, library, db = _user(), _library(), AsyncMock()
    document_id, source_job_id = uuid.uuid4(), uuid.uuid4()
    _overrides(user, db)
    try:
        with (
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch.object(api.deps_module, "load_active_library", new=AsyncMock(return_value=library)),
            patch.object(api, "authorize_library_management", new=AsyncMock()),
            patch.object(
                api,
                "retry_document_processing_stage",
                new=AsyncMock(return_value=_processing(library, document_id)),
            ) as retry,
        ):
            response = TestClient(app).post(
                f"/libraries/catalog/catalog/documents/{document_id}/processing/summary/retry",
                json={"source_job_id": str(source_job_id), "retry_generation": 2},
            )
        assert response.status_code == 200, response.text
        assert retry.await_args.kwargs["source_job_id"] == source_job_id
        assert retry.await_args.kwargs["observed_retry_generation"] == 2
        db.commit.assert_awaited_once()

        db.reset_mock()
        with (
            patch.object(settings, "knowledge_catalog_enabled", True),
            patch.object(api.deps_module, "load_active_library", new=AsyncMock(return_value=library)),
            patch.object(api, "authorize_library_management", new=AsyncMock()),
            patch.object(
                api,
                "retry_document_processing_stage",
                new=AsyncMock(
                    side_effect=DocumentProcessingError(
                        "processing_retry_rejected",
                        "raw provider response with credential",
                    )
                ),
            ),
        ):
            failed = TestClient(app).post(
                f"/libraries/catalog/catalog/documents/{document_id}/processing/summary/retry",
                json={"source_job_id": str(source_job_id), "retry_generation": 2},
            )
        assert failed.status_code == 409
        assert failed.json() == {"detail": "processing_retry_rejected"}
        assert "credential" not in failed.text
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
