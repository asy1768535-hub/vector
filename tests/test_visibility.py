"""#6 §7.2 检索可见性过滤 compute_visible_mask 的单测。"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.services import visibility

LIB_ID = uuid.uuid4()
D1 = uuid.uuid4()
D2 = uuid.uuid4()


def _db_with_docs(rows):
    """rows: list of (id, library_id, current_revision)."""
    db = MagicMock()
    res = MagicMock()
    res.all.return_value = rows
    db.execute = AsyncMock(return_value=res)
    return db


def _lib(mode="managed"):
    return NS(id=LIB_ID, lifecycle_mode=mode)


def test_external_library_bypasses(monkeypatch):
    payloads = [{"document_id": str(D1)}, {}]   # 即便缺 document_id 也放行
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs([]), _lib("external"), payloads))
    assert mask == [True, True]


def test_filter_off_bypasses(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_consistency_filter", False)
    payloads = [{"document_id": str(D1)}]
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs([]), _lib("managed"), payloads))
    assert mask == [True]


def test_managed_revision_match_and_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    rows = [(D1, LIB_ID, 3, None), (D2, LIB_ID, 5, None)]   # (id, library_id, current_revision, deleted_at)
    payloads = [
        {"document_id": str(D1), "document_revision": 3},   # 匹配 → 可见
        {"document_id": str(D2), "document_revision": 4},   # 旧 revision → 不可见
        {"document_id": str(uuid.uuid4())},                  # 不存在 → 不可见
        {"text": "no id"},                                   # 缺 document_id → 不可见
    ]
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs(rows), _lib("managed"), payloads))
    assert mask == [True, False, False, False]


def test_managed_deleted_not_visible(monkeypatch):
    """#7：已 tombstone（deleted_at 非空）→ 立即不可见，即便 revision 匹配。"""
    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    import datetime as _dt
    rows = [(D1, LIB_ID, 3, _dt.datetime(2026, 1, 1))]      # deleted_at 非空
    payloads = [{"document_id": str(D1), "document_revision": 3}]
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs(rows), _lib("managed"), payloads))
    assert mask == [False]


def test_managed_missing_revision_treated_as_1(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    rows = [(D1, LIB_ID, 1, None)]
    payloads = [{"document_id": str(D1)}]   # payload 无 document_revision → 视作 1 → 匹配
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs(rows), _lib("managed"), payloads))
    assert mask == [True]


def test_managed_library_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    rows = [(D1, uuid.uuid4(), 1, None)]    # 文档属于别的库
    payloads = [{"document_id": str(D1), "document_revision": 1}]
    mask = asyncio.run(visibility.compute_visible_mask(_db_with_docs(rows), _lib("managed"), payloads))
    assert mask == [False]
