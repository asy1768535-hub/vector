from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.auth.backend import current_superuser
from app.db import get_db
from app.main import app
from app.models.user import User


def test_create_library_duplicate_slug_returns_clear_chinese_409():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate slug")))
    db.rollback = AsyncMock()

    async def _ov_su():
        return su

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        resp = TestClient(app).post(
            "/admin/libraries",
            json={"slug": "dup_lib", "name": "重复库"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "库唯一ID已存在，请更换后重试"
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
