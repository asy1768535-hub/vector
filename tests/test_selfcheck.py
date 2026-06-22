"""selfcheck 可消费判定单测：维度不符对 worker 算不可消费（不连真实服务）。"""
from __future__ import annotations

from app.services import selfcheck


def _aprobe(value):
    async def _f():
        return value
    return _f


async def test_check_consumable_all_ok(monkeypatch):
    monkeypatch.setattr(selfcheck, "probe_embedding", _aprobe((True, "ok", 1024)))
    monkeypatch.setattr(selfcheck, "probe_qdrant", _aprobe((True, "ok")))
    ok, _reason = await selfcheck.check_consumable()
    assert ok is True


async def test_check_consumable_dim_mismatch_not_consumable(monkeypatch):
    # embedding 可达但维度不符 → 写 Qdrant 会失败 → worker 不可消费
    monkeypatch.setattr(selfcheck, "probe_embedding", _aprobe((True, "dim mismatch: 实测 768 维", 768)))
    monkeypatch.setattr(selfcheck, "probe_qdrant", _aprobe((True, "ok")))
    ok, reason = await selfcheck.check_consumable()
    assert ok is False
    assert "embedding" in reason and "mismatch" in reason


async def test_check_consumable_qdrant_down(monkeypatch):
    monkeypatch.setattr(selfcheck, "probe_embedding", _aprobe((True, "ok", 1024)))
    monkeypatch.setattr(selfcheck, "probe_qdrant", _aprobe((False, "connect refused")))
    ok, reason = await selfcheck.check_consumable()
    assert ok is False and "qdrant" in reason


async def test_run_startup_check_dim_mismatch_returns_false(monkeypatch):
    # API 仍会照常启动(调用方不依赖返回值)，但返回 False 让 worker 进入 degraded
    monkeypatch.setattr(selfcheck, "probe_embedding", _aprobe((True, "dim mismatch: 实测 768 维", 768)))
    monkeypatch.setattr(selfcheck, "probe_qdrant", _aprobe((True, "ok")))
    monkeypatch.setattr(selfcheck, "probe_rerank", _aprobe(("off", "disabled")))
    assert await selfcheck.run_startup_check("test") is False
