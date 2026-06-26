"""知识库「常用问题」(FAQ) 后台接口测试。

走真实 router + 权限依赖(require_lib) + schema 校验；DB 层用进程内 fake（替换
app.services.library_faq.*），从而无需真实 PG 即可验证排序/过滤/CRUD/权限语义。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth.backend import current_active_user
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.library_faq import LibraryFAQQuestion
from app.models.user import User

LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
SLUG = "medical"

mock_user = User(
    id="00000000-0000-0000-0000-000000000001",
    email="u@example.com", is_superuser=True, is_active=True,
)
mock_library = Library(
    id=LIB_ID, slug=SLUG, name="med", qdrant_collection="lib_medical",
    embedding_model="bge-m3", embedding_dim=1024, chunk_size=1000, chunk_overlap=120,
    lifecycle_mode="managed", index_state="ready",
)

# 进程内假存储（每个测试由 fixture 清空）
STORE: list[LibraryFAQQuestion] = []


def _make_faq(question, sort_order=0, *, is_active=True, created_at=None):
    now = created_at or datetime.now(timezone.utc)
    return LibraryFAQQuestion(
        id=uuid.uuid4(), library_id=LIB_ID, question=question,
        sort_order=sort_order, is_active=is_active, created_at=now, updated_at=now,
    )


# ── fake service 实现（与真实 library_faq 同语义）──────────────────────────
async def fake_list(db, library_id, *, include_inactive=False):
    items = [f for f in STORE if f.library_id == library_id and (include_inactive or f.is_active)]
    return sorted(items, key=lambda f: (f.sort_order, f.created_at))


async def fake_get(db, library_id, faq_id):
    return next((f for f in STORE if f.id == faq_id and f.library_id == library_id), None)


async def fake_create(db, library_id, payload):
    f = _make_faq(payload.question, payload.sort_order, is_active=payload.is_active)
    f.library_id = library_id
    STORE.append(f)
    return f


async def fake_update(db, faq, payload):
    if payload.question is not None:
        faq.question = payload.question
    if payload.sort_order is not None:
        faq.sort_order = payload.sort_order
    if payload.is_active is not None:
        faq.is_active = payload.is_active
    return faq


async def fake_delete(db, faq):
    STORE.remove(faq)


def _only(*actions):
    """构造 has_permission side_effect：仅这些 action 返回 True。"""
    allowed = set(actions)
    return lambda uid, slug, action: action in allowed


def _perm(side_effect):
    """同时 patch require_lib(app.deps) 与 list 处 include_inactive 判定(admin_libraries) 的 has_permission。"""
    return (
        patch("app.deps.has_permission", side_effect=side_effect),
        patch("app.api.admin_libraries.has_permission", side_effect=side_effect),
    )


@pytest.fixture
def client():
    STORE.clear()
    mock_user.is_superuser = True

    async def ov_user():
        return mock_user

    async def ov_db():
        db = AsyncMock()
        db.add = MagicMock()         # add 是同步方法（audit_log 不 await 它）
        return db

    app.dependency_overrides[current_active_user] = ov_user
    app.dependency_overrides[get_db] = ov_db
    patchers = [
        patch("app.deps.load_active_library", new=AsyncMock(return_value=mock_library)),
        patch("app.services.library_faq.list_faqs", new=fake_list),
        patch("app.services.library_faq.get_faq", new=fake_get),
        patch("app.services.library_faq.create_faq", new=fake_create),
        patch("app.services.library_faq.update_faq", new=fake_update),
        patch("app.services.library_faq.delete_faq", new=fake_delete),
    ]
    for p in patchers:
        p.start()
    try:
        yield TestClient(app)
    finally:
        for p in reversed(patchers):
            p.stop()
        app.dependency_overrides.clear()


# 1. read 权限用户可读 active FAQ
def test_read_user_can_read_active(client):
    STORE.extend([_make_faq("q1", 0), _make_faq("q2", 1)])
    mock_user.is_superuser = False
    p1, p2 = _perm(_only("read"))
    with p1, p2:
        r = client.get(f"/admin/libraries/{SLUG}/faqs")
    assert r.status_code == 200
    assert [f["question"] for f in r.json()] == ["q1", "q2"]


# 2. 无 read 权限用户不能读
def test_no_read_user_forbidden(client):
    mock_user.is_superuser = False
    p1, p2 = _perm(_only())          # 什么权限都没有
    with p1, p2:
        r = client.get(f"/admin/libraries/{SLUG}/faqs")
    assert r.status_code == 403


# 3. admin 权限用户可新增（并 strip question）
def test_admin_can_create(client):
    mock_user.is_superuser = False
    p1, p2 = _perm(_only("admin", "read"))
    with p1, p2:
        r = client.post(f"/admin/libraries/{SLUG}/faqs",
                        json={"question": "  八大员包括哪些岗位  ", "sort_order": 2})
    assert r.status_code == 201
    body = r.json()
    assert body["question"] == "八大员包括哪些岗位"     # 首尾空格被 strip
    assert body["sort_order"] == 2 and body["is_active"] is True
    assert len(STORE) == 1


# 4. 非 admin 用户不能新增
def test_non_admin_cannot_create(client):
    mock_user.is_superuser = False
    p1, p2 = _perm(_only("read"))     # 只有 read
    with p1, p2:
        r = client.post(f"/admin/libraries/{SLUG}/faqs", json={"question": "x"})
    assert r.status_code == 403
    assert not STORE


# 5. 更新会 strip question；空问题 → 422
def test_update_strips_question(client):
    faq = _make_faq("orig", 0)
    STORE.append(faq)
    r = client.patch(f"/admin/libraries/{SLUG}/faqs/{faq.id}", json={"question": "  新的问题  "})
    assert r.status_code == 200
    assert r.json()["question"] == "新的问题"


def test_update_blank_question_returns_422(client):
    faq = _make_faq("orig", 0)
    STORE.append(faq)
    r = client.patch(f"/admin/libraries/{SLUG}/faqs/{faq.id}", json={"question": "   "})
    assert r.status_code == 422


def test_create_blank_question_returns_422(client):
    r = client.post(f"/admin/libraries/{SLUG}/faqs", json={"question": "   "})
    assert r.status_code == 422
    assert not STORE


# 6. 删除后列表不再返回
def test_delete_removes_from_list(client):
    faq = _make_faq("to-delete", 0)
    STORE.append(faq)
    r = client.delete(f"/admin/libraries/{SLUG}/faqs/{faq.id}")
    assert r.status_code == 204
    r2 = client.get(f"/admin/libraries/{SLUG}/faqs")
    assert r2.json() == []


def test_delete_missing_faq_404(client):
    r = client.delete(f"/admin/libraries/{SLUG}/faqs/{uuid.uuid4()}")
    assert r.status_code == 404


# 7. inactive 默认不返回
def test_inactive_hidden_by_default(client):
    STORE.append(_make_faq("active", 0, is_active=True))
    STORE.append(_make_faq("hidden", 1, is_active=False))
    r = client.get(f"/admin/libraries/{SLUG}/faqs")     # superuser，不带 include_inactive
    assert [f["question"] for f in r.json()] == ["active"]


# 8. include_inactive=true 仅 admin/superuser 生效
def test_include_inactive_admin_sees_all(client):
    STORE.append(_make_faq("active", 0, is_active=True))
    STORE.append(_make_faq("hidden", 1, is_active=False))
    r = client.get(f"/admin/libraries/{SLUG}/faqs?include_inactive=true")   # superuser
    assert [f["question"] for f in r.json()] == ["active", "hidden"]


def test_include_inactive_ignored_for_read_only_user(client):
    STORE.append(_make_faq("active", 0, is_active=True))
    STORE.append(_make_faq("hidden", 1, is_active=False))
    mock_user.is_superuser = False
    p1, p2 = _perm(_only("read"))     # read 但非 admin
    with p1, p2:
        r = client.get(f"/admin/libraries/{SLUG}/faqs?include_inactive=true")
    assert r.status_code == 200
    assert [f["question"] for f in r.json()] == ["active"]    # 被降级，仍只 active


# 9. 排序：sort_order asc, created_at asc
def test_ordering_by_sort_then_created(client):
    t0 = datetime.now(timezone.utc)
    STORE.append(_make_faq("b_sort1", 1, created_at=t0))
    STORE.append(_make_faq("a_sort0_late", 0, created_at=t0 + timedelta(seconds=5)))
    STORE.append(_make_faq("c_sort0_early", 0, created_at=t0))
    r = client.get(f"/admin/libraries/{SLUG}/faqs")
    assert [f["question"] for f in r.json()] == ["c_sort0_early", "a_sort0_late", "b_sort1"]


# 10. 删除知识库后 FAQ 不可见（库软删 → load_active_library 返回 None → 403）
def test_deleted_library_faqs_not_visible(client):
    with patch("app.deps.load_active_library", new=AsyncMock(return_value=None)):
        r = client.get(f"/admin/libraries/{SLUG}/faqs")
    assert r.status_code == 403
