"""W2：账号安全 / 密码管理 / 权限展示 的接口与逻辑测试。

走真实 router + 依赖覆盖；DB/UserManager 用进程内 mock，无需真实 PG。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api import me as me_api
from app.api import admin_users as admin_users_api
from app.auth.backend import current_active_user, current_superuser
from app.auth.user_manager import UserManager, get_user_manager
from app.db import get_db
from app.main import app
from app.models.user import User

NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


def _user(*, is_superuser=False, email="u@example.com") -> User:
    return User(
        id=uuid.uuid4(), email=email, username="u", display_name="U",
        is_active=True, is_superuser=is_superuser, is_verified=True, created_at=NOW,
    )


def _db_get(mapping: dict) -> AsyncMock:
    """构造支持 await db.get(Model, pk) / commit / refresh / add / flush 的 fake db。"""
    db = AsyncMock()
    async def _get(_model, pk):
        return mapping.get(pk)
    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()
    return db


# ── W2-1：reset token 不进日志 ───────────────────────────────────────────────
def test_reset_token_not_logged(caplog):
    um = UserManager(MagicMock())
    user = _user(email="reset@example.com")
    token = "SUPER-SECRET-RESET-TOKEN-zzz-999"
    with caplog.at_level(logging.INFO):
        asyncio.run(um.on_after_forgot_password(user, token))
    assert token not in caplog.text                 # token 绝不出现
    assert "reset" in caplog.text.lower()           # 记录了"已请求重置"
    assert "reset@example.com" in caplog.text       # 记录了 email


# ── W2-2：超管不能自我降权 / 停用 ───────────────────────────────────────────
def test_superuser_cannot_self_demote_or_deactivate():
    actor = _user(is_superuser=True, email="boss@example.com")
    db = _db_get({actor.id: actor})
    app.dependency_overrides[current_superuser] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        r1 = c.patch(f"/admin/users/{actor.id}", json={"is_superuser": False})
        assert r1.status_code == 400 and "超级管理员" in r1.json()["detail"]
        r2 = c.patch(f"/admin/users/{actor.id}", json={"is_active": False})
        assert r2.status_code == 400 and "停用自己" in r2.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_superuser_can_demote_another_user():
    actor = _user(is_superuser=True, email="boss@example.com")
    other = _user(is_superuser=True, email="other@example.com")
    db = _db_get({actor.id: actor, other.id: other})
    app.dependency_overrides[current_superuser] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(admin_users_api.audit_log, "record", new=AsyncMock()):
            r = TestClient(app).patch(f"/admin/users/{other.id}", json={"is_superuser": False})
        assert r.status_code == 200 and r.json()["is_superuser"] is False
    finally:
        app.dependency_overrides.clear()


def test_delete_cannot_disable_self():
    actor = _user(is_superuser=True)
    db = _db_get({actor.id: actor})
    app.dependency_overrides[current_superuser] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        r = TestClient(app).delete(f"/admin/users/{actor.id}")
        assert r.status_code == 400 and "yourself" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ── W2-4：管理员重置密码 ─────────────────────────────────────────────────────
def test_admin_reset_password_atomic_commit_once():
    actor = _user(is_superuser=True)
    target = _user(email="target@example.com")
    target.hashed_password = "OLD-HASH-PLACEHOLDER"
    db = _db_get({target.id: target})
    um = UserManager(MagicMock())   # 真实 password_helper

    audit_calls: list = []
    async def _audit(_db, _actor, action, target_dict=None):
        audit_calls.append((action, target_dict))

    app.dependency_overrides[current_superuser] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_manager] = lambda: um
    try:
        with patch.object(admin_users_api.audit_log, "record", new=_audit):
            r = TestClient(app).post(
                f"/admin/users/{target.id}/reset-password", json={"password": "brandnew123"})
        assert r.status_code == 204
        # 成功路径：密码 + 审计一次提交
        assert db.commit.await_count == 1
    finally:
        app.dependency_overrides.clear()

    # 新密码可验证、旧密码失效；存的是哈希而非明文
    assert target.hashed_password != "brandnew123"
    ok_new, _ = um.password_helper.verify_and_update("brandnew123", target.hashed_password)
    ok_old, _ = um.password_helper.verify_and_update("oldpassword0", target.hashed_password)
    assert ok_new is True and ok_old is False
    # 审计：仅 user_id + user.password_reset，不含明文/哈希
    assert ("user.password_reset", {"user_id": str(target.id)}) in audit_calls
    assert all(
        "password" not in (t or {}) and "hashed_password" not in (t or {})
        for _, t in audit_calls
    )


def test_admin_reset_password_audit_failure_no_commit():
    actor = _user(is_superuser=True)
    target = _user(email="target2@example.com")
    db = _db_get({target.id: target})
    um = UserManager(MagicMock())

    async def _boom(*_a, **_k):
        raise RuntimeError("audit backend down")

    app.dependency_overrides[current_superuser] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_manager] = lambda: um
    try:
        with patch.object(admin_users_api.audit_log, "record", new=_boom):
            r = TestClient(app, raise_server_exceptions=False).post(
                f"/admin/users/{target.id}/reset-password", json={"password": "brandnew123"})
        assert r.status_code == 500
        # 审计失败 → 绝不提交（密码改动随请求回滚）
        assert db.commit.await_count == 0
    finally:
        app.dependency_overrides.clear()


def test_reset_password_requires_superuser():
    # 不注入用户 → 真实鉴权：普通/未认证一律被拒（superuser-only 端点）
    app.dependency_overrides.clear()
    r = TestClient(app).post(
        f"/admin/users/{uuid.uuid4()}/reset-password", json={"password": "whatever123"})
    assert r.status_code in (401, 403)


def test_admin_user_update_schema_drops_password():
    # 普通 PATCH 不能混入 password：上线口径要求额外字段直接拒绝，不能静默丢弃。
    from app.schemas.admin import AdminUserUpdate
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        AdminUserUpdate(username="x", password="should-be-ignored")  # type: ignore[call-arg]


# ── W2-5：/me/permissions 带 library_name 且过滤已删除库 ─────────────────────
def test_me_permissions_attaches_name_and_filters_deleted():
    user = _user()
    result = MagicMock()
    result.all.return_value = [("alpha", "Alpha 知识库")]   # ghost 已删除 → 不在结果里
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(
            me_api.casbin_service, "list_user_permissions",
            return_value={"alpha": ["read"], "ghost": ["read", "insert"]},
        ):
            r = TestClient(app).get("/me/permissions")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["library_slug"] == "alpha"
        assert rows[0]["library_name"] == "Alpha 知识库"
        assert rows[0]["actions"] == ["read"]
        assert "organization_id" not in rows[0]
        assert all(x["library_slug"] != "ghost" for x in rows)   # 已删除库被过滤
    finally:
        app.dependency_overrides.clear()


def test_me_permissions_superuser_empty():
    su = _user(is_superuser=True)
    app.dependency_overrides[current_active_user] = lambda: su
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        r = TestClient(app).get("/me/permissions")
        assert r.status_code == 200 and r.json() == []
    finally:
        app.dependency_overrides.clear()
