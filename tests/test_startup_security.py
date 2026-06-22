"""JWT 启动加固（拒用默认密钥）的单测。"""
from __future__ import annotations

import pytest

from app.main import assert_startup_security, _DEFAULT_JWT_SECRET


def test_default_secret_in_prod_raises(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", _DEFAULT_JWT_SECRET)
    monkeypatch.setattr(settings, "app_debug", False)
    with pytest.raises(RuntimeError, match="拒绝启动"):
        assert_startup_security()


def test_default_secret_in_debug_warns_not_raises(monkeypatch, caplog):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", _DEFAULT_JWT_SECRET)
    monkeypatch.setattr(settings, "app_debug", True)
    assert_startup_security()  # 不抛


def test_custom_secret_passes(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", "a-strong-random-secret-value-xyz")
    monkeypatch.setattr(settings, "app_debug", False)
    assert_startup_security()  # 不抛
