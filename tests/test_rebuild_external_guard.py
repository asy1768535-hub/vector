"""#6 R6：external 库 rebuild → 409，且绝不调用任何 Qdrant 删除/重建。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.auth.backend import current_superuser
from app.db import get_db
from app.models.library import Library
from app.models.user import User

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
    res = MagicMock(); res.scalar_one_or_none.return_value = lib
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
