"""Casbin enforcer 单例 + SQLAlchemy adapter。

策略持久化在 casbin_rule 表（adapter 自动建表）。
启动时 load_policy 把全部策略读入内存，enforce() 是纯内存匹配。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import casbin
from casbin_sqlalchemy_adapter import Adapter

from app.config import settings

_MODEL_PATH = Path(__file__).resolve().parent / "model.conf"

_enforcer: Optional[casbin.Enforcer] = None
_lock = threading.Lock()


def get_enforcer() -> casbin.Enforcer:
    """惰性初始化的全局 enforcer。线程安全。"""
    global _enforcer
    if _enforcer is None:
        with _lock:
            if _enforcer is None:
                adapter = Adapter(settings.db_dsn_sync)
                _enforcer = casbin.Enforcer(str(_MODEL_PATH), adapter)
                _enforcer.load_policy()
    return _enforcer


def reload_policy() -> None:
    """多副本场景下，外部触发刷新策略缓存。"""
    if _enforcer is not None:
        _enforcer.load_policy()


def has_permission(user_id: str, library_slug: str, action: str) -> bool:
    """Convenience wrapper: r=(sub, library:<slug>, action)."""
    return get_enforcer().enforce(user_id, f"library:{library_slug}", action)
