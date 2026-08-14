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
            assert "attempt_count" in str(db.execute.call_args.args[0])
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


def test_retry_route_does_not_accept_unknown_import_or_graph_ids():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        client = TestClient(app)
        resp = client.post(f"/admin/jobs/{uuid.uuid4()}/retry")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
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


def test_monitor_maps_graph_waiting_schema_to_pending_for_closed_stats():
    _, _, graph_job = _monitor_pipeline_jobs("waiting_schema")
    row = _graph_monitor_row(graph_job, title="sample.docx")
    assert row.status == "pending"
    assert row.raw_status == "waiting_schema"


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
    assert embedding_row.retry_capability == "supported"
    assert graph_row.retry_capability == "stale"
    assert graph_row.retry_reason == "旧版本或非 production 图谱任务不可重试"


def test_monitor_marks_exhausted_embedding_attempts_non_retryable():
    _, embedding_job, _ = _monitor_pipeline_jobs("failed")
    embedding_job.status = "failed"
    embedding_job.attempt_count = settings.embed_worker_max_attempts
    row = _embedding_monitor_row(embedding_job, title="sample.docx")
    assert row.retryable is False
    assert row.retry_capability == "exhausted"
    assert row.retry_reason == "尝试次数耗尽"
    assert row.raw_error is None


def test_monitor_import_and_graph_failed_errors_keep_raw_error_and_capability():
    import_job, embedding_job, graph_job = _monitor_pipeline_jobs("failed")
    import_job.status = "failed"
    import_job.last_error = None
    import_row = _import_monitor_row(import_job, embedding_job=embedding_job, graph_job=graph_job)
    graph_row = _graph_monitor_row(graph_job, title="sample.docx")
    assert import_row.retryable is True
    assert import_row.retry_capability == "supported"
    assert import_row.raw_error is None
    assert graph_row.retryable is False
    assert graph_row.raw_error is None


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


def test_graph_monitor_only_exposes_provider_in_flight_for_processing_jobs():
    _, _, graph_job = _monitor_pipeline_jobs("processing")
    graph_job.statistics = {"provider_gate": {"in_flight": 6}}

    processing = _graph_monitor_row(graph_job, title="sample.docx")
    assert processing.metrics["in_flight"] == 6

    graph_job.status = "succeeded"
    graph_job.finished_at = datetime.now(timezone.utc)
    finished = _graph_monitor_row(graph_job, title="sample.docx")
    assert finished.metrics["in_flight"] is None


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


def test_graph_monitor_marks_latest_failed_job_with_retryable_units():
    _, _, graph_job = _monitor_pipeline_jobs("failed")
    graph_job.execution_mode = "production"
    row = _graph_monitor_row(
        graph_job,
        title="sample.docx",
        latest_production=True,
        has_retryable_units=True,
    )
    assert row.retryable is True
    assert row.retry_target_type == "graph"
    assert row.retry_target_id == graph_job.id
    assert row.retry_generation == graph_job.retry_generation


def test_import_downstream_failure_targets_real_embedding_job():
    import_job, embedding_job, graph_job = _monitor_pipeline_jobs("processing")
    embedding_job.status = "failed"
    import_job.current_stage = "embedding"
    row = _import_monitor_row(
        import_job,
        embedding_job=embedding_job,
        graph_job=graph_job,
    )
    assert row.retryable is True
    assert row.retry_target_type == "embedding"
    assert row.retry_target_id == embedding_job.id


def test_import_own_failure_does_not_route_to_stale_downstream_job():
    import_job, embedding_job, graph_job = _monitor_pipeline_jobs("failed")
    import_job.status = "failed"
    import_job.current_stage = "parsing"
    embedding_job.status = "failed"
    graph_job.status = "failed"
    row = _import_monitor_row(
        import_job,
        embedding_job=embedding_job,
        graph_job=graph_job,
    )
    assert row.retry_target_type == "import"
    assert row.retry_target_id == import_job.id
    assert row.retryable is True


def test_unified_retry_deduplicates_embedding_targets_and_audits_each_result():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    job_id = uuid.uuid4()
    job = EmbeddingJob(
        id=job_id,
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        status="failed",
        attempt_count=1,
        created_at=datetime.now(timezone.utc),
    )

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(return_value=job)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.api.admin_jobs.audit_log.record", new_callable=AsyncMock) as audit:
            client = TestClient(app)
            response = client.post(
                "/admin/jobs/monitor/retry",
                json={
                    "items": [
                        {"task_type": "embedding", "job_id": str(job_id), "observed_generation": 1},
                        {"task_type": "embedding", "job_id": str(job_id), "observed_generation": 1},
                    ]
                },
            )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()["results"]
        assert [item["status"] for item in payload] == ["succeeded", "rejected"]
        assert payload[1]["reason"] == "duplicate_target"
        assert job.status == "pending"
        assert audit.await_count == 2
    finally:
        app.dependency_overrides.clear()


def test_unified_retry_rejects_stale_embedding_generation_without_mutation():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    job = EmbeddingJob(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        status="failed",
        attempt_count=3,
        created_at=datetime.now(timezone.utc),
    )

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(return_value=job)
    db.commit = AsyncMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.api.admin_jobs.audit_log.record", new_callable=AsyncMock):
            client = TestClient(app)
            response = client.post(
                "/admin/jobs/monitor/retry",
                json={
                    "items": [
                        {"task_type": "embedding", "job_id": str(job.id), "observed_generation": 2}
                    ]
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"][0]["reason"] == "stale_generation"
        assert job.status == "failed"
    finally:
        app.dependency_overrides.clear()


def test_unified_retry_routes_graph_without_embedding_retry_api():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    _, _, graph_job = _monitor_pipeline_jobs("failed")
    graph_job.execution_mode = "production"
    library = MagicMock(id=graph_job.library_id)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = graph_job.id

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[graph_job, library])
    db.execute = AsyncMock(return_value=execute_result)
    db.commit = AsyncMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.api.admin_jobs.retry_graph_extraction_job", new_callable=AsyncMock) as graph_retry, \
             patch("app.api.admin_jobs.retry_job", new_callable=AsyncMock) as embedding_retry, \
             patch("app.api.admin_jobs.audit_log.record", new_callable=AsyncMock):
            graph_retry.return_value = graph_job
            client = TestClient(app)
            response = client.post(
                "/admin/jobs/monitor/retry",
                json={
                    "items": [
                        {"task_type": "graph", "job_id": str(graph_job.id), "observed_generation": 0}
                    ]
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"][0]["status"] == "succeeded"
        graph_retry.assert_awaited_once()
        embedding_retry.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_unified_retry_routes_import_to_import_service_only():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    import_job, _, _ = _monitor_pipeline_jobs("failed")
    import_job.status = "failed"
    import_job.current_stage = "parsing"

    async def _ov_su():
        return su

    db = AsyncMock()
    db.get = AsyncMock(return_value=import_job)
    db.commit = AsyncMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.api.admin_jobs.import_uploads.retry_job", new_callable=AsyncMock) as import_retry, \
             patch("app.api.admin_jobs.retry_graph_extraction_job", new_callable=AsyncMock) as graph_retry, \
             patch("app.api.admin_jobs.audit_log.record", new_callable=AsyncMock):
            client = TestClient(app)
            response = client.post(
                "/admin/jobs/monitor/retry",
                json={
                    "items": [
                        {"task_type": "import", "job_id": str(import_job.id), "observed_generation": 1}
                    ]
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"][0]["status"] == "succeeded"
        import_retry.assert_awaited_once()
        graph_retry.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
