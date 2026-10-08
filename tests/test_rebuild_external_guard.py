"""#6 R6：external 库 rebuild → 409（API）+ Service 层防护，且绝不调用任何 Qdrant 删除/重建。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.auth.backend import current_superuser
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.services import rebuild as rebuild_svc

_su = User(id=uuid.uuid4(), email="r@x", is_superuser=True, is_active=True)


def _ext_lib():
    return Library(
        id=uuid.uuid4(), slug="extlib", name="E", qdrant_collection="case_chunks_000",
        embedding_model="m", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
        vector_distance="cosine", lifecycle_mode="external", index_state="ready",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_rebuild_external_returns_409_without_touching_qdrant():
    lib = _ext_lib()
    db = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = lib
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()

    app.dependency_overrides[current_superuser] = lambda: _su
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as dc, \
             patch("app.services.qdrant.ensure_collection", new_callable=AsyncMock) as ec, \
             patch("app.services.rebuild.run_rebuild", new_callable=AsyncMock) as rr:
            resp = TestClient(app).post("/admin/libraries/extlib/rebuild-collection")
            assert resp.status_code == status.HTTP_409_CONFLICT
            dc.assert_not_called()
            ec.assert_not_called()
            rr.assert_not_called()        # 连重建编排都不进入
    finally:
        app.dependency_overrides.clear()


def test_service_run_rebuild_rejects_external_without_qdrant():
    """Service 层 run_rebuild 对 external 库直接抛 ExternalLibraryError，绝不调 Qdrant。"""
    ext = _ext_lib()
    res = MagicMock()
    res.scalar_one.return_value = ext
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)

    with patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as dc, \
         patch("app.services.qdrant.ensure_collection", new_callable=AsyncMock) as ec:
        with pytest.raises(rebuild_svc.ExternalLibraryError):
            asyncio.run(rebuild_svc.run_rebuild(db, ext.id))
        dc.assert_not_called()
        ec.assert_not_called()


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


def test_finalize_fails_operation_when_rebuild_job_was_superseded():
    """A terminal superseded rebuild job must not leave the library rebuilding forever."""
    library_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    library = SimpleNamespace(id=library_id, index_state="rebuilding",
                              active_rebuild_operation_id=operation_id,
                              qdrant_collection="test_col", deleted_at=None, lifecycle_mode="managed")
    operation = SimpleNamespace(
        id=operation_id,
        library_id=library_id,
        collection_name="test_col",
        status="running",
        expected_job_count=1,
        last_error=None,
        finished_at=None,
    )
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(library_id),
            _scalar_result(library),
            _scalar_result(operation),
            _scalar_result(0),
            _scalar_result(1),
        ]
    )
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    advanced = asyncio.run(rebuild_svc.try_finalize(db, operation_id))

    assert advanced is True
    assert operation.status == "failed"
    assert operation.last_error == "rebuild jobs did not complete: failed=0, superseded=1"
    assert operation.finished_at is not None
    assert library.index_state == "failed"
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_activate_binds_document_revision_id_and_int_revision():
    library_id = uuid.uuid4()
    op_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()
    operation = SimpleNamespace(
        id=op_id,
        library_id=library_id,
        status="preparing",
        expected_job_count=0,
        collection_name="test_col",
    )

    db = MagicMock()
    lib_res = MagicMock()
    lib_res.scalar_one.return_value = SimpleNamespace(
        active_rebuild_operation_id=op_id, qdrant_collection="test_col",
        lifecycle_mode="managed", deleted_at=None,
    )
    op_res = MagicMock()
    op_res.scalar_one.return_value = operation
    from app.models.embedding_job import EmbeddingJob
    job = EmbeddingJob(library_id=library_id, document_id=doc_id, status="pending",
                       document_revision=8, document_revision_id=rev_id,
                       document_revision_no=2, rebuild_operation_id=op_id)
    jobs_res = MagicMock()
    jobs_res.scalars.return_value.all.return_value = [job]
    db.execute = AsyncMock(side_effect=[lib_res, op_res, jobs_res])
    added_jobs = []
    db.add = MagicMock(side_effect=lambda job: added_jobs.append(job))
    db.commit = AsyncMock()

    doc_revs = [(doc_id, 8, rev_id, 2)]
    asyncio.run(rebuild_svc._activate(db, library_id, op_id, doc_revs))

    assert added_jobs == []
    assert job.library_id == library_id
    assert job.document_id == doc_id
    assert job.document_revision == 8
    assert isinstance(job.document_revision, int)
    assert job.document_revision_id == rev_id
    assert isinstance(job.document_revision_id, uuid.UUID)
    assert job.document_revision_no == 2
    assert job.rebuild_operation_id == op_id
    assert operation.expected_job_count == 1
    assert operation.status == "running"
    db.commit.assert_awaited_once()


def test_activate_backward_compatibility_with_2_tuple(monkeypatch):
    monkeypatch.setattr(rebuild_svc.settings, "enable_revision_id_worker", False)
    library_id = uuid.uuid4()
    op_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    operation = SimpleNamespace(
        id=op_id,
        library_id=library_id,
        status="preparing",
        expected_job_count=0,
        collection_name="test_col",
    )

    db = MagicMock()
    lib_res = MagicMock()
    lib_res.scalar_one.return_value = SimpleNamespace(
        active_rebuild_operation_id=op_id, qdrant_collection="test_col",
        lifecycle_mode="managed", deleted_at=None,
    )
    op_res = MagicMock()
    op_res.scalar_one.return_value = operation
    jobs_res = MagicMock()
    jobs_res.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[lib_res, op_res, jobs_res])
    db.get = AsyncMock(return_value=SimpleNamespace(
        library_id=library_id, deleted_at=None, current_revision=3, current_revision_id=None,
    ))
    added_jobs = []
    db.add = MagicMock(side_effect=lambda job: added_jobs.append(job))
    db.commit = AsyncMock()

    doc_revs = [(doc_id, 3)]
    asyncio.run(rebuild_svc._activate(db, library_id, op_id, doc_revs))

    assert len(added_jobs) == 1
    job = added_jobs[0]
    assert job.document_id == doc_id
    assert job.document_revision == 3
    assert job.document_revision_id is None
    assert job.document_revision_no is None
    assert job.rebuild_operation_id == op_id
