"""#6 §6/§7.1 写前置守卫：external→409、rebuilding/failed→503。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.auth.backend import current_active_user
from app.db import get_db
from tests.test_query_import_api import mock_library, override_user, make_db_mock


@pytest.fixture
def client():
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = lambda: make_db_mock()
    with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml:
        ml.return_value = mock_library
        yield TestClient(app)
    app.dependency_overrides.clear()


def _ingest(client):
    return client.post("/libraries/testlib/documents", json={"text": "hello world"})


def test_external_library_write_409(client, monkeypatch):
    monkeypatch.setattr(mock_library, "lifecycle_mode", "external")
    monkeypatch.setattr(mock_library, "index_state", "ready")
    assert _ingest(client).status_code == status.HTTP_409_CONFLICT


def test_rebuilding_library_write_503(client, monkeypatch):
    monkeypatch.setattr(mock_library, "lifecycle_mode", "managed")
    monkeypatch.setattr(mock_library, "index_state", "rebuilding")
    resp = _ingest(client)
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert resp.headers.get("Retry-After") == "5"


def test_ready_managed_library_write_ok(client, monkeypatch):
    monkeypatch.setattr(mock_library, "lifecycle_mode", "managed")
    monkeypatch.setattr(mock_library, "index_state", "ready")
    with patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as it:
        from unittest.mock import MagicMock
        import uuid
        doc = MagicMock(); doc.id = uuid.uuid4(); doc.status = "pending"
        job = MagicMock(); job.id = uuid.uuid4()
        it.return_value = (doc, job, 1, False)
        assert _ingest(client).status_code == status.HTTP_201_CREATED


# ── 阻断3：重建期间禁止改/删库配置（503）─────────────────────────────
def _admin_client(monkeypatch, index_state):
    from app.auth.backend import current_superuser
    from app.models.user import User
    import uuid
    su = User(id=uuid.uuid4(), email="su@x", is_superuser=True, is_active=True)
    monkeypatch.setattr(mock_library, "lifecycle_mode", "managed")
    monkeypatch.setattr(mock_library, "index_state", index_state)
    app.dependency_overrides[current_superuser] = lambda: su
    app.dependency_overrides[get_db] = lambda: make_db_mock()
    return TestClient(app)


def test_update_library_blocked_while_rebuilding(monkeypatch):
    try:
        resp = _admin_client(monkeypatch, "rebuilding").patch("/admin/libraries/testlib", json={"name": "x"})
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    finally:
        app.dependency_overrides.clear()


def test_delete_library_blocked_while_rebuilding(monkeypatch):
    try:
        resp = _admin_client(monkeypatch, "rebuilding").delete("/admin/libraries/testlib")
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    finally:
        app.dependency_overrides.clear()
