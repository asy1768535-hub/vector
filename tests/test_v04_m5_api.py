from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.auth.backend import current_active_user
from app.db import get_db
from app.main import app
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.user import User
from app.services.graph_extraction_jobs import GraphExtractionJobError


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")


def _library() -> Library:
    return Library(
        id=LIB_ID,
        slug="m5-api",
        name="M5 API",
        qdrant_collection="m5_api",
        embedding_model="bge-m3",
        embedding_dim=1024,
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
    )


def _user(*, admin=True) -> User:
    return User(
        id=USER_ID,
        email="m5@example.com",
        is_superuser=admin,
        is_active=True,
    )


def _job(**changes):
    values = {
        "id": JOB_ID,
        "library_id": LIB_ID,
        "document_id": DOC_ID,
        "document_revision_id": REV_ID,
        "ontology_version_id": ONTOLOGY_ID,
        "trigger_type": "manual",
        "execution_mode": "production",
        "status": "queued",
        "current_stage": "preparing",
        "input_fingerprint": "a" * 64,
        "rerun_of_job_id": None,
        "retry_generation": 0,
        "model_provider": "dashscope",
        "model_name": "qwen-plus",
        "prompt_version": "v1",
        "extractor_version": "v1",
        "output_parser_version": "v1",
        "context_policy_version": "v1",
        "extraction_policy_version": "v1",
        "normalization_rule_version": "normalization_v1",
        "confidence_policy_version": "v1",
        "document_parser_version": "parser-v1",
        "chunking_strategy_version": "chunk-v1",
        "counts": {"total": 1, "queued": 1},
        "statistics": {},
        "error_code": None,
        "sensitive_payload_purged_at": None,
        "created_at": None,
        "updated_at": None,
        "started_at": None,
        "finished_at": None,
        "model_config_snapshot": {"secret": "must-not-leak"},
        "ontology_snapshot": {"sensitive": "must-not-leak"},
        "error_message": "must-not-leak",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _document_scope():
    document = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        content_hash="document-hash",
        current_revision_id=REV_ID,
        status="ready",
        deleted_at=None,
    )
    revision = DocumentRevision(
        id=REV_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        revision_no=1,
        content_hash="revision-hash",
        parser_name="plain",
        parser_version="parser-v1",
        chunking_strategy="fixed",
        chunking_strategy_version="chunk-v1",
        security_level="internal",
        status="ready",
    )
    return document, revision


def _client(db, *, admin=True):
    async def override_db():
        return db

    async def override_user():
        return _user(admin=admin)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def test_v04_graph_extraction_routes_are_mounted():
    paths = set(app.openapi()["paths"])
    prefix = "/libraries/{slug}/v04/graph-extractions"
    assert f"{prefix}/" in paths
    assert f"{prefix}/{{job_id}}" in paths
    assert f"{prefix}/{{job_id}}/units" in paths
    assert f"{prefix}/{{job_id}}/candidates" in paths
    assert f"{prefix}/{{job_id}}/retry" in paths
    assert f"{prefix}/{{job_id}}/rerun" in paths
    assert f"{prefix}/{{job_id}}/cancel" in paths


def test_create_returns_sanitized_job_without_frozen_or_raw_payloads():
    db = AsyncMock()
    document, revision = _document_scope()

    async def get_row(model, object_id):
        return {
            (Document, DOC_ID): document,
            (DocumentRevision, REV_ID): revision,
        }.get((model, object_id))

    db.get = AsyncMock(side_effect=get_row)
    db.commit = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v04_graph_extraction.graph_extraction_jobs.create_graph_extraction_job",
                new=AsyncMock(return_value=_job()),
            ) as create,
        ):
            response = _client(db).post(
                "/libraries/m5-api/v04/graph-extractions/",
                json={"document_id": str(DOC_ID), "idempotency_key": "manual-key"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"] == str(JOB_ID)
    for forbidden in (
        "idempotency_key",
        "model_config_snapshot",
        "policy_config_snapshot",
        "ontology_snapshot",
        "raw_response",
        "parsed_response",
        "error_message",
    ):
        assert forbidden not in payload
    create.assert_awaited_once()
    db.commit.assert_awaited_once()


def test_retry_returns_exact_no_retryable_units_conflict():
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v04_graph_extraction.graph_extraction_jobs.retry_graph_extraction_job",
                new=AsyncMock(
                    side_effect=GraphExtractionJobError(
                        "no_retryable_units",
                        "nothing to retry",
                    )
                ),
            ),
        ):
            response = _client(db).post(
                f"/libraries/m5-api/v04/graph-extractions/{JOB_ID}/retry"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"] == "no_retryable_units"
    db.commit.assert_not_awaited()


def test_cancel_refreshes_server_updated_fields_before_response():
    db = AsyncMock()
    job = _job(status="cancelled")
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v04_graph_extraction.graph_extraction_jobs.cancel_graph_extraction_job",
                new=AsyncMock(return_value=job),
            ),
        ):
            response = _client(db).post(
                f"/libraries/m5-api/v04/graph-extractions/{JOB_ID}/cancel"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(job)


def test_full_rerun_requires_admin_even_with_library_insert_permission():
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", return_value=True),
            patch(
                "app.api.v04_graph_extraction.graph_extraction_jobs.create_graph_extraction_job",
                new=AsyncMock(),
            ) as create,
        ):
            response = _client(db, admin=False).post(
                f"/libraries/m5-api/v04/graph-extractions/{JOB_ID}/rerun",
                json={"client_idempotency_key": "rerun-key-001"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "admin_required"
    create.assert_not_awaited()
