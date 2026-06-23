"""#7 Cleanup 服务/worker 纯逻辑单测：execute_event 派发 + 指数退避。"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.cleanup_outbox import (
    EVENT_DELETE_COLLECTION, EVENT_DELETE_DOCUMENT_ALL, EVENT_DELETE_DOCUMENT_BEFORE_REVISION,
)
from app.services import cleanup as cleanup_svc
from app.workers import cleanup as cleanup_worker


def _row(event_type, **kw):
    base = dict(collection_name="lib_c", document_id=uuid.uuid4(), target_revision=None)
    base.update(kw)
    return NS(event_type=event_type, **base)


def test_execute_event_dispatch_delete_all():
    row = _row(EVENT_DELETE_DOCUMENT_ALL)
    with patch("app.services.qdrant.delete_points_by_document_id", new_callable=AsyncMock) as f, \
         patch("app.services.qdrant.delete_points_before_revision", new_callable=AsyncMock) as g, \
         patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as h:
        asyncio.run(cleanup_svc.execute_event(row))
        f.assert_awaited_once_with("lib_c", str(row.document_id))
        g.assert_not_called(); h.assert_not_called()


def test_execute_event_dispatch_before_revision():
    row = _row(EVENT_DELETE_DOCUMENT_BEFORE_REVISION, target_revision=5)
    with patch("app.services.qdrant.delete_points_before_revision", new_callable=AsyncMock) as g:
        asyncio.run(cleanup_svc.execute_event(row))
        g.assert_awaited_once_with("lib_c", str(row.document_id), 5)


def test_execute_event_dispatch_delete_collection():
    row = _row(EVENT_DELETE_COLLECTION)
    with patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as h:
        asyncio.run(cleanup_svc.execute_event(row))
        h.assert_awaited_once_with("lib_c")


def test_backoff_increases_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "cleanup_backoff_base_seconds", 5.0)
    monkeypatch.setattr(settings, "cleanup_backoff_max_seconds", 60.0)
    # 退避（不含 jitter）随 attempt 递增并封顶：base*2^(n-1)，n 大时 == cap
    lows = [cleanup_worker._backoff_seconds(n) - 0 for n in (1, 2, 3)]
    # 去掉 jitter 的下界：base*2^(n-1)
    assert lows[0] >= 5.0 and lows[1] >= 10.0 and lows[2] >= 20.0
    big = cleanup_worker._backoff_seconds(20)
    assert big <= 60.0 + 5.0          # cap + 最多一个 base 的 jitter
