"""GET /admin/operations/status 接口测试（docs/26 / 批次 C2，测试清单 §8）。

鉴权：超管之外被拒。聚合 shape：空库 → 三类服务恒出现且 offline、统计全零。
用依赖覆盖 + mock session（execute 按调用顺序返回），不连真实 DB。
DB 侧聚合的端到端正确性由 PG 集成测试覆盖（无 DSN 时 SKIP）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.auth.backend import current_superuser
from app.db import get_db
from app.main import app
from app.models.user import User

NOW = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc)


def _result(*, scalar_one=None, scalars_all=None, all_rows=None):
    """构造一个能按需响应 scalar_one() / scalars().all() / all() 的结果 mock。"""
    r = MagicMock()
    if scalar_one is not None:
        r.scalar_one.return_value = scalar_one
    r.scalars.return_value.all.return_value = scalars_all or []
    r.all.return_value = all_rows or []
    return r


def test_status_requires_superuser():
    """未认证（无 cookie）访问被拒——端点非公开（401/403）。"""
    client = TestClient(app)
    resp = client.get("/admin/operations/status")
    assert resp.status_code in (401, 403)


def test_status_empty_db_shape():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)

    async def _ov_su():
        return su

    # 端点内 execute 调用顺序：now() → heartbeats → jobs → outbox → libraries → rebuild ops
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(scalar_one=NOW),     # select(func.now())
        _result(scalars_all=[]),     # select(ServiceHeartbeat)
        _result(all_rows=[]),        # embedding_jobs group by
        _result(all_rows=[]),        # cleanup_outbox group by
        _result(all_rows=[]),        # libraries group by
        _result(all_rows=[]),        # graph publications group by
        _result(all_rows=[]),        # rebuild operations
    ])

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        client = TestClient(app)
        resp = client.get("/admin/operations/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 四类进程恒定出现且 offline
        types = {s["service_type"]: s for s in body["services"]}
        assert set(types) == {
            "api",
            "embedding_worker",
            "cleanup_worker",
            "graph_extractor",
        }
        for s in body["services"]:
            assert s["status"] == "offline"
            assert s["online_instances"] == 0 and s["known_instances"] == 0
            assert s["latest"] is None
        # 统计全零
        assert body["embedding_jobs"]["total"] == 0
        assert body["cleanup_outbox"]["dead_letter"] == 0
        assert body["libraries"] == {"rebuilding": 0, "failed": 0}
        assert body["graph_publications"] == {"active": 0, "degraded": 0}
        assert body["rebuild_operations"] == []
        assert body["offline_threshold_seconds"] == 60
    finally:
        app.dependency_overrides.clear()


def test_status_aggregates_counts_and_progress():
    """有数据：服务在线 + 任务/Outbox/重建进度按聚合行正确映射。"""
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)

    async def _ov_su():
        return su

    from app.models.service_heartbeat import ServiceHeartbeat

    def _hb(service_type, instance):
        # last_seen_at = NOW，DB now 也用 NOW → 龄 0s → 在线
        return ServiceHeartbeat(
            service_type=service_type, instance_id=instance, hostname="h", pid=1,
            started_at=NOW, last_seen_at=NOW, status="online", heartbeat_metadata=None,
        )

    hbs = [
        _hb("api", "h-1-a"),
        _hb("embedding_worker", "h-2-b"),
        _hb("cleanup_worker", "h-3-c"),
        _hb("graph_extractor", "h-4-d"),
    ]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(scalar_one=NOW),
        _result(scalars_all=hbs),
        _result(all_rows=[("pending", 3), ("processing", 1), ("done", 120), ("superseded", 4)]),
        _result(all_rows=[("pending", 2), ("done", 88), ("failed", 1)]),
        _result(all_rows=[("rebuilding", 1), ("ready", 5)]),
        _result(all_rows=[("active", 4), ("degraded", 2)]),
        _result(all_rows=[("demo", "running", 50, None, 37)]),
    ])

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        client = TestClient(app)
        resp = client.get("/admin/operations/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for s in body["services"]:
            assert s["status"] == "online" and s["online_instances"] == 1
        # superseded 计入 total 不单列
        assert body["embedding_jobs"]["total"] == 128
        assert body["embedding_jobs"]["done"] == 120
        # dead_letter = failed
        assert body["cleanup_outbox"]["dead_letter"] == 1
        assert body["cleanup_outbox"]["total"] == 91
        assert body["libraries"]["rebuilding"] == 1
        assert body["graph_publications"] == {"active": 4, "degraded": 2}
        op = body["rebuild_operations"][0]
        assert op["library_slug"] == "demo"
        assert op["done_job_count"] == 37 and op["expected_job_count"] == 50
        assert op["progress_pct"] == 74.0
    finally:
        app.dependency_overrides.clear()


def test_requeue_failed_cleanup_requires_superuser():
    client = TestClient(app)
    resp = client.post("/admin/operations/cleanup-outbox/requeue-failed")
    assert resp.status_code in (401, 403)


def test_requeue_failed_cleanup_accepts_library_filter():
    su = User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)
    lib_id = uuid.uuid4()

    async def _ov_su():
        return su

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=4))
    db.commit = AsyncMock()
    db.add = MagicMock()

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        client = TestClient(app)
        resp = client.post(f"/admin/operations/cleanup-outbox/requeue-failed?library_id={lib_id}")
        assert resp.status_code == 200
        assert resp.json() == {"requeued_count": 4}
        sql = str(db.execute.await_args.args[0]).lower()
        assert "qdrant_cleanup_outbox" in sql
        assert "status='failed'" in sql or "status = 'failed'" in sql
        assert "library_id" in sql
        params = db.execute.await_args.args[1]
        assert params["library_id"] == str(lib_id)
        db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
