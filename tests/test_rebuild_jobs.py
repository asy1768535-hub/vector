"""#5 重建 collection：保留历史 job、每篇活动文档恰好新建一条 pending job。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.auth.backend import current_superuser
from app.db import get_db
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.user import User

_superuser = User(
    id="00000000-0000-0000-0000-000000000099",
    email="root@example.com", is_superuser=True, is_active=True,
)
_lib = Library(
    id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
    slug="testlib", name="T", qdrant_collection="testlib_col",
    embedding_model="bge-m3", embedding_dim=1024,
    chunk_size=1000, chunk_overlap=120, vector_distance="cosine",
    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
)


def test_rebuild_creates_one_job_per_active_doc():
    doc_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    lib_res = MagicMock(); lib_res.scalar_one_or_none.return_value = _lib
    upd_res = MagicMock()
    ids_res = MagicMock(); ids_res.scalars.return_value.all.return_value = doc_ids

    db = MagicMock()
    # 调用序：select Library → update Document → select Document.id
    db.execute = AsyncMock(side_effect=[lib_res, upd_res, ids_res])
    added = []
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = lambda: _superuser
    app.dependency_overrides[get_db] = _ov_db
    try:
        with patch("app.api.admin_libraries.qdrant.delete_collection", new_callable=AsyncMock), \
             patch("app.api.admin_libraries.qdrant.ensure_collection", new_callable=AsyncMock), \
             patch("app.api.admin_libraries.audit_log.record", new_callable=AsyncMock):
            resp = TestClient(app).post("/admin/libraries/testlib/rebuild-collection")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == status.HTTP_200_OK
    jobs = [o for o in added if isinstance(o, EmbeddingJob)]
    # 关键：恰好每篇活动文档一条新 job（不多不少），且都是 pending
    assert len(jobs) == len(doc_ids)
    assert {j.document_id for j in jobs} == set(doc_ids)
    assert all(j.status == "pending" for j in jobs)
