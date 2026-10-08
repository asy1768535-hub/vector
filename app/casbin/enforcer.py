"""Casbin 操作级策略快照 + 每进程复用的 SQLAlchemy adapter。

策略持久化在 casbin_rule 表（adapter 自动建表）。
每次操作从数据库读取已提交策略，避免不同 API 进程长期使用旧权限。
"""
from __future__ import annotations

import threading
from pathlib import Path

import casbin
from casbin_sqlalchemy_adapter import Adapter

from app.config import settings

_MODEL_PATH = Path(__file__).resolve().parent / "model.conf"

_adapter: Adapter | None = None
_lock = threading.Lock()


def get_enforcer() -> casbin.Enforcer:
    """返回独立的最新策略快照；仅 adapter 和连接池跨操作复用。"""
    global _adapter
    if _adapter is None:
        with _lock:
            if _adapter is None:
                _adapter = Adapter(settings.db_dsn_sync)
    # Enforcer 构造时已经 load_policy。快照不共享，避免刷新修改在途模型。
    return casbin.Enforcer(str(_MODEL_PATH), _adapter)


def reload_policy() -> None:
    """保留显式刷新入口；后续操作总会自行读取最新策略。"""
    if _adapter is not None:
        get_enforcer()


def has_permission(user_id: str, library_slug: str, action: str) -> bool:
    """Convenience wrapper: r=(sub, library:<slug>, action)."""
    return get_enforcer().enforce(user_id, f"library:{library_slug}", action)
