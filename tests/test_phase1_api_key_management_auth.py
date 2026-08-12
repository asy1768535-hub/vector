from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.auth.backend import current_cookie_user, get_jwt_strategy
from app.models.api_key import ApiKey
from app.models.user import User
from app.schemas.dify import DifyRetrievalResponse


def _user(*, active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        email="phase1@example.com",
        is_active=active,
        is_superuser=False,
        is_verified=True,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
    )


def _empty_db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


async def _cookie_user():
    return _user()


def test_api_key_cannot_manage_api_keys_with_bearer_auth():
    """API Key is for business APIs only; key management must require cookie login."""
    app.dependency_overrides[get_db] = lambda: _empty_db()
    try:
        with patch("app.auth.api_key.APIKeyStrategy.read_token", new=AsyncMock(return_value=_user())):
            resp = TestClient(app).get(
                "/me/api-keys",
                headers={"Authorization": "Bearer vk_phase1_key"},
            )
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_inactive_api_key_user_is_rejected_by_protected_endpoint():
    """A key belonging to a disabled user must not authenticate as active."""
    app.dependency_overrides[get_db] = lambda: _empty_db()
    try:
        with patch("app.auth.api_key.APIKeyStrategy.read_token", new=AsyncMock(return_value=_user(active=False))):
            resp = TestClient(app).get(
                "/me/permissions",
                headers={"Authorization": "Bearer disabled-key"},
            )
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_api_key_list_never_returns_plaintext_or_hash():
    user = _user()
    row = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        name="dify",
        key_prefix="vk_test123",
        key_hash="$2b$12$secret-hash-must-not-leak",
        created_at=datetime.now(timezone.utc),
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [row]
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/me/api-keys")
        assert resp.status_code == 200
        body = resp.json()[0]
        assert "plaintext_key" not in body
        assert "key_hash" not in body
        assert body["key_prefix"] == "vk_test123"
    finally:
        app.dependency_overrides.clear()


def test_superuser_api_key_cannot_access_read_or_write_admin_routes():
    superuser = _user()
    superuser.is_superuser = True
    app.dependency_overrides[get_db] = lambda: _empty_db()
    try:
        with patch("app.auth.api_key.APIKeyStrategy.read_token", new=AsyncMock(return_value=superuser)):
            client = TestClient(app)
            read_response = client.get(
                "/admin/audit-log",
                headers={"Authorization": "Bearer vk_superuser_key"},
            )
            write_response = client.post(
                "/admin/operations/cleanup-outbox/requeue-failed",
                headers={"Authorization": "Bearer vk_superuser_key"},
            )
        assert read_response.status_code == 401
        assert write_response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_superuser_cookie_can_access_admin_route():
    superuser = _user()
    superuser.is_superuser = True
    db = _empty_db()
    db.get = AsyncMock(return_value=superuser)
    app.dependency_overrides[get_db] = lambda: db
    try:
        token = asyncio.run(get_jwt_strategy().write_token(superuser))
        client = TestClient(app)
        client.cookies.set("vk_session", token)
        response = client.get(
            "/admin/audit-log",
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_api_key_business_retrieval_behavior_is_unchanged():
    user = _user()
    library = SimpleNamespace(
        slug="business",
        index_state="ready",
        qdrant_collection="business_collection",
        embedding_model="bge-m3",
        embedding_base_url=None,
        rerank_enabled=False,
        retrieval_mode="dense",
        source_config={},
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch("app.auth.api_key.APIKeyStrategy.read_token", new=AsyncMock(return_value=user)),
            patch("app.api.retrieval.load_active_library", new=AsyncMock(return_value=library)),
            patch("app.api.retrieval.has_permission", return_value=True),
            patch(
                "app.api.retrieval.run_retrieval",
                new=AsyncMock(return_value=DifyRetrievalResponse(records=[])),
            ),
        ):
            response = TestClient(app).post(
                "/retrieval",
                json={"knowledge_id": "business", "query": "hello"},
                headers={"Authorization": "Bearer vk_business_key"},
            )
        assert response.status_code == 200
        assert response.json() == {"records": []}
    finally:
        app.dependency_overrides.clear()
