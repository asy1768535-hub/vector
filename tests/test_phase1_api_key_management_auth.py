from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.auth.backend import current_cookie_user
from app.models.api_key import ApiKey
from app.models.user import User


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
