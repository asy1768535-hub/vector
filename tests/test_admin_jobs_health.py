"""#5 /health 的 ocr 状态、#6 reset-failed 按库过滤的接口测试。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.api.admin_jobs import (
    _embedding_monitor_row,
    _graph_monitor_row,
    _graph_publication_status,
    _import_monitor_row,
    router as admin_jobs_router,
)
from app.api.health import _check_ocr
from app.auth.backend import current_superuser
from app.config import settings
from app.db import get_db
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.user import User


# ── #5：/health 的 ocr 状态 ───────────────────────────────────────────────


def test_check_ocr_ok_when_engine_available():
    with patch("app.services.ocr.is_available", return_value=True):
        assert _check_ocr() == "ok"


def test_check_ocr_missing_when_enabled_but_not_installed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    with patch("app.services.ocr.is_available", return_value=False):
        assert _check_ocr() == "missing"  # 开了却没装 → 大声报


def test_check_ocr_off_when_disabled_and_not_installed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    with patch("app.services.ocr.is_available", return_value=False):
        assert _check_ocr() == "off"


# ── #6：reset-failed 按库过滤 ─────────────────────────────────────────────


def test_reset_failed_accepts_library_filter():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)

    async def _ov_su():
        return su

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=3))

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.services.audit_log.record", new_callable=AsyncMock) as rec:
            client = TestClient(app)
            lib_id = uuid.uuid4()
            resp = client.post(f"/admin/jobs/reset-failed?library_id={lib_id}")
            assert resp.status_code == status.HTTP_200_OK
            assert resp.json()["reset_count"] == 3
            # 审计里记录了 library_id（便于追溯按库重置）
            target = rec.call_args.args[3]
            assert target["library_id"] == str(lib_id)
            assert target["reset_count"] == 3
    finally:
        app.dependency_overrides.clear()


def test_reset_failed_global_when_no_library():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)

    async def _ov_su():
        return su

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=9))

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.services.audit_log.record", new_callable=AsyncMock) as rec:
            client = TestClient(app)
            resp = client.post("/admin/jobs/reset-failed")
            assert resp.status_code == status.HTTP_200_OK
            assert resp.json()["reset_count"] == 9
            assert rec.call_args.args[3]["library_id"] is None
    finally:
        app.dependency_overrides.clear()


def test_retry_processing_job_rejected_to_avoid_duplicate_workers():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    job = EmbeddingJob(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision=1,
        status="processing",
        worker_id="worker-1",
        attempt_count=1,
        created_at=datetime.now(timezone.utc),
    )

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(return_value=job)

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.services.audit_log.record", new_callable=AsyncMock):
            client = TestClient(app)
            resp = client.post(f"/admin/jobs/{job.id}/retry")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "processing" in resp.json()["detail"]
        db.execute.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def _monitor_pipeline_jobs(graph_status: str = "queued"):
    now = datetime.now(timezone.utc)
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    embedding_id = uuid.uuid4()
    import_job = DocumentImportJob(
        id=uuid.uuid4(),
        library_id=library_id,
        batch_id=uuid.uuid4(),
        file_name="sample.docx",
        size_bytes=100,
        upload_offset=100,
        staging_key=f"monitor/{uuid.uuid4()}",
        graph_extraction_requested=True,
        status="processing",
        current_stage="embedding",
        attempt_count=1,
        embedding_job_id=embedding_id,
        document_id=document_id,
        document_revision_id=revision_id,
        created_at=now,
    )
    embedding_job = EmbeddingJob(
        id=embedding_id,
        library_id=library_id,
        document_id=document_id,
        document_revision=2,
        document_revision_id=revision_id,
        status="done",
        attempt_count=1,
        created_at=now,
        finished_at=now,
    )
    graph_job = GraphExtractionJob(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        status=graph_status,
        current_stage="extracting" if graph_status == "processing" else "preparing",
        retry_generation=0,
        model_config_snapshot={"build_mode": "standard"},
        counts={"total": 4, "queued": 1, "processing": 1, "succeeded": 2},
        created_at=now,
        finished_at=now if graph_status not in {"queued", "processing"} else None,
    )
    return import_job, embedding_job, graph_job


def test_monitor_import_waits_for_graph_job_after_embedding_finishes():
    import_job, embedding_job, _ = _monitor_pipeline_jobs()

    row = _import_monitor_row(
        import_job,
        embedding_job=embedding_job,
        graph_job=None,
        revision_no=2,
    )

    assert row.status == "processing"
    assert row.raw_status == "awaiting_graph"
    assert row.stage == "awaiting_graph"


def test_monitor_import_tracks_graph_extraction_until_it_finishes():
    import_job, embedding_job, graph_job = _monitor_pipeline_jobs("processing")
    processing = _import_monitor_row(
        import_job,
        embedding_job=embedding_job,
        graph_job=graph_job,
        revision_no=2,
    )
    assert processing.status == "processing"
    assert processing.stage == "extracting"

    graph_job.status = "succeeded"
    graph_job.current_stage = "finalizing"
    finished = _import_monitor_row(
        import_job,
        embedding_job=embedding_job,
        graph_job=graph_job,
        revision_no=2,
    )
    assert finished.status == "done"
    assert finished.raw_status == "succeeded"


def test_monitor_only_marks_embedding_jobs_retryable():
    _, embedding_job, graph_job = _monitor_pipeline_jobs("failed")
    embedding_job.status = "failed"

    embedding_row = _embedding_monitor_row(embedding_job, title="sample.docx")
    graph_row = _graph_monitor_row(graph_job, title="sample.docx", revision_no=2)

    assert embedding_row.retryable is True
    assert graph_row.status == "failed"
    assert graph_row.retryable is False


def test_monitor_routes_are_registered_before_dynamic_retry_route():
    paths = [route.path for route in admin_jobs_router.routes]
    assert "/admin/jobs/monitor" in paths
    assert "/admin/jobs/monitor/stats" in paths
    assert paths.index("/admin/jobs/monitor") < paths.index("/admin/jobs/{job_id}/retry")


def test_graph_monitor_exposes_frozen_mode_and_unit_progress():
    _, _, graph_job = _monitor_pipeline_jobs("processing")
    row = _graph_monitor_row(graph_job, title="sample.docx", revision_no=2)
    assert row.build_mode == "standard"
    assert row.progress == {
        "completed": 2,
        "total": 4,
        "percent": 50.0,
        "queued": 1,
        "processing": 1,
        "completed_batches": 2,
        "planned_batches": 4,
    }


def test_graph_monitor_exposes_entity_only_quality_statistics():
    _, _, graph_job = _monitor_pipeline_jobs("succeeded")
    graph_job.statistics = {
        **(graph_job.statistics or {}),
        "candidate_pipeline": {
            "entity_candidate_count": 3,
            "relation_candidate_count": 2,
            "routing": {
                "entities": {"validated": 0, "pending_review": 3},
                "relations": {"validated": 0, "rejected": 2},
            },
            "failure_reasons": {
                "entities": {"concept_requires_valid_relation": 1},
                "relations": {"relation_evidence_invalid": 2},
            },
        },
        "materialization": {
            "outcome": "entities_only",
            "publishable_entity_candidate_count": 0,
            "publishable_relation_count": 0,
            "pending_entity_candidate_count": 3,
            "failure_reasons": {"no_valid_relation": 1},
        },
        "publication": {
            "outcome": "skipped",
            "entity_count": 0,
            "relation_count": 0,
            "failure_reason": "no_valid_relation",
            "current_graph_unchanged": True,
        },
    }

    row = _graph_monitor_row(graph_job, title="sample.docx", revision_no=2)

    assert _graph_publication_status(graph_job, None) == "entities_only"
    assert row.metrics["stage_counts"] == {
        "extraction": {"entities": 3, "relations": 2},
        "validation": {"entities": 0, "relations": 0},
        "materialization": {"entities": 0, "relations": 0},
        "publication": {"entities": 0, "relations": 0},
    }
    assert row.metrics["failure_reasons"]["publication"] == "no_valid_relation"
    assert row.metrics["current_graph_unchanged"] is True
