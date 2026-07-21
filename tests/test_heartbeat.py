"""运行状态心跳单元测试（docs/26 / 批次 C2，测试清单 §8）。

纯单元：不连真实 DB / 模型服务。`derive_service_status` 是纯函数；`beat` 的失败兜底
用 patch 模拟独立 session 抛错，断言不向上传播。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import CheckConstraint

from app.config import settings
from app.models.service_heartbeat import ServiceHeartbeat
from app.services import heartbeat

NOW = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc)
OFFLINE = 60


def _row(service_type="api", *, age_seconds=0, status="online", metadata=None,
         instance_id="host-1-aaaa", pid=1):
    """构造一个未持久化的心跳行（last_seen_at = NOW - age）。"""
    return ServiceHeartbeat(
        service_type=service_type,
        instance_id=instance_id,
        hostname="host",
        pid=pid,
        started_at=NOW - timedelta(hours=1),
        last_seen_at=NOW - timedelta(seconds=age_seconds),
        status=status,
        heartbeat_metadata=metadata,
    )


# ── §8.2 在线/离线边界 ────────────────────────────────────────────────────
def test_online_offline_boundary():
    online = heartbeat.derive_service_status("api", [_row(age_seconds=59)], now=NOW, offline_seconds=OFFLINE)
    assert online["status"] == "online" and online["online_instances"] == 1
    offline = heartbeat.derive_service_status("api", [_row(age_seconds=61)], now=NOW, offline_seconds=OFFLINE)
    assert offline["status"] == "offline" and offline["online_instances"] == 0
    # 离线实例仍计入 known（未过期），但不在线
    assert offline["known_instances"] == 1


# ── §8.12 多实例部分离线 → degraded ──────────────────────────────────────
def test_partial_offline_degraded():
    rows = [
        _row(instance_id="host-1-a", age_seconds=5),
        _row(instance_id="host-1-b", age_seconds=8),
        _row(instance_id="host-1-c", age_seconds=12),
        _row(instance_id="host-1-d", age_seconds=90),   # stale 但未过期
    ]
    out = heartbeat.derive_service_status("api", rows, now=NOW, offline_seconds=OFFLINE)
    assert out["status"] == "degraded"
    assert out["online_instances"] == 3 and out["known_instances"] == 4


def test_all_fresh_online_all_stale_offline():
    fresh = [_row(instance_id="a", age_seconds=5), _row(instance_id="b", age_seconds=9)]
    assert heartbeat.derive_service_status("api", fresh, now=NOW, offline_seconds=OFFLINE)["status"] == "online"
    stale = [_row(instance_id="a", age_seconds=90), _row(instance_id="b", age_seconds=120)]
    assert heartbeat.derive_service_status("api", stale, now=NOW, offline_seconds=OFFLINE)["status"] == "offline"


def test_stopping_excluded_from_online_and_known():
    rows = [
        _row(instance_id="a", age_seconds=5),
        _row(instance_id="b", age_seconds=5, status="stopping"),
    ]
    out = heartbeat.derive_service_status("api", rows, now=NOW, offline_seconds=OFFLINE)
    # stopping 不计入 online/known
    assert out["online_instances"] == 1 and out["known_instances"] == 1
    assert out["status"] == "online"


def test_degraded_via_metadata_flag():
    rows = [_row(age_seconds=5, metadata={"degraded": True})]
    out = heartbeat.derive_service_status("embedding_worker", rows, now=NOW, offline_seconds=OFFLINE)
    assert out["status"] == "degraded"   # 单实例在线但自报降级


# ── §8.11 PID 复用不覆盖旧实例（两行并存）──────────────────────────────────
def test_pid_reuse_two_rows_degraded():
    rows = [
        _row(instance_id="host-100-aaaa", age_seconds=90, pid=100),   # 旧实例离线未过期
        _row(instance_id="host-100-bbbb", age_seconds=5, pid=100),    # 复用 PID 的新实例在线
    ]
    out = heartbeat.derive_service_status("cleanup_worker", rows, now=NOW, offline_seconds=OFFLINE)
    assert out["known_instances"] == 2 and out["online_instances"] == 1
    assert out["status"] == "degraded"


# ── §8.7 空：无任何行 → offline / latest=None ─────────────────────────────
def test_empty_rows_offline():
    out = heartbeat.derive_service_status("cleanup_worker", [], now=NOW, offline_seconds=OFFLINE)
    assert out["status"] == "offline"
    assert out["online_instances"] == 0 and out["known_instances"] == 0
    assert out["latest"] is None


def test_latest_is_freshest_instance():
    rows = [_row(instance_id="old", age_seconds=40), _row(instance_id="new", age_seconds=3)]
    out = heartbeat.derive_service_status("api", rows, now=NOW, offline_seconds=OFFLINE)
    assert out["latest"]["instance_id"] == "new"
    assert out["latest"]["seconds_since_last_seen"] == 3


# ── §8.10 metadata ORM 保留名回归 ────────────────────────────────────────
def test_metadata_reserved_name_mapping():
    # 导入/映射不抛；ORM 属性是 heartbeat_metadata 且绑定 DB 列名 metadata
    assert ServiceHeartbeat.__table__.c.metadata.name == "metadata"
    row = _row(metadata={"watch": True})
    assert row.heartbeat_metadata == {"watch": True}
    row.heartbeat_metadata = {"degraded": True}      # 可写
    assert row.heartbeat_metadata["degraded"] is True


def test_service_heartbeat_model_allows_graph_extractor():
    check = next(
        constraint
        for constraint in ServiceHeartbeat.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_heartbeat_service_type"
    )
    assert "graph_extractor" in str(check.sqltext)
    assert "knowledge_artifact_worker" in str(check.sqltext)


# ── §8.4 心跳写失败不向上抛、不终止调用方 ──────────────────────────────────
def test_beat_swallows_db_error():
    def _boom(*a, **k):
        raise RuntimeError("db down")

    async def run():
        with patch.object(heartbeat, "async_session_factory", _boom):
            ok = await heartbeat.beat(
                "api", "host-1-aaaa", hostname="host", pid=1, started_at=NOW,
            )
        return ok

    # 不抛异常；返回 False
    assert asyncio.run(run()) is False


def test_prune_swallows_db_error():
    def _boom(*a, **k):
        raise RuntimeError("db down")

    async def run():
        with patch.object(heartbeat, "async_session_factory", _boom):
            return await heartbeat.prune(cutoff_seconds=3600)

    assert asyncio.run(run()) == 0


def test_make_instance_id_unique_suffix():
    a, b = heartbeat.make_instance_id(), heartbeat.make_instance_id()
    assert a != b                       # 随机后缀防 PID 复用覆盖
    assert a.startswith(f"{heartbeat.HOSTNAME}-{heartbeat.PID}-")


# ── §8.9 长任务期间 heartbeat_loop 仍独立打卡（与主循环解耦）─────────────────
def test_heartbeat_loop_keeps_beating_during_long_task(monkeypatch):
    """主循环阻塞在长任务时，独立 heartbeat_loop 照常每 interval 打卡（用极短 interval，不真睡 60s）。"""
    monkeypatch.setattr(settings, "heartbeat_interval_seconds", 0.01)
    calls: list[int] = []

    async def _fake_beat(*a, **k):
        calls.append(1)
        return True

    monkeypatch.setattr(heartbeat, "beat", _fake_beat)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat.heartbeat_loop(
            "embedding_worker", "host-1-aaaa",
            hostname="h", pid=1, started_at=NOW, stop_event=stop,
        ))
        # 模拟主循环阻塞在一个长任务期间——heartbeat_loop 是另一个并发任务，应持续打卡
        await asyncio.sleep(0.06)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert len(calls) >= 2              # 长任务期间多次打卡 → 实例保持在线


# ── shutdown：stop_event 触发后立即结束循环、不等满 interval、无遗留 task ──────
def test_heartbeat_loop_stops_promptly_no_leftover_task(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_interval_seconds", 100)   # 故意很长

    async def _fake_beat(*a, **k):
        return True

    monkeypatch.setattr(heartbeat, "beat", _fake_beat)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat.heartbeat_loop(
            "api", "host-1-aaaa", hostname="h", pid=1, started_at=NOW, stop_event=stop,
        ))
        await asyncio.sleep(0.02)       # 让它进入可被唤醒的 sleep
        stop.set()
        # 即便 interval=100s，置位后应立即唤醒返回——否则这里会超时
        await asyncio.wait_for(task, timeout=1.0)
        return task

    task = asyncio.run(run())
    assert task.done() and not task.cancelled()   # 优雅结束，非取消、无悬挂
