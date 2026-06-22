"""#5 /health 的 ocr 状态、#6 reset-failed 按库过滤的接口测试。"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.api.health import _check_ocr
from app.auth.backend import current_superuser
from app.config import settings
from app.db import get_db
from app.models.user import User


# ── #5：/health 的 ocr 状态 ───────────────────────────────────────────────

def test_check_ocr_ok_when_engine_available():
    with patch("app.services.ocr.is_available", return_value=True):
        assert _check_ocr() == "ok"


def test_check_ocr_missing_when_enabled_but_not_installed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    with patch("app.services.ocr.is_available", return_value=False):
        assert _check_ocr() == "missing"      # 开了却没装 → 大声报


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
