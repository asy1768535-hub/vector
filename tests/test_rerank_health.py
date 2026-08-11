from __future__ import annotations

from app.api import health as health_api
from app.config import settings
from app.services import rerank as rerank_svc
from app.services import selfcheck


def _async_value(value):
    async def _probe():
        return value

    return _probe


async def test_health_rerank_is_off_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(selfcheck, "any_library_rerank_enabled", _async_value(False))
    monkeypatch.setattr(rerank_svc, "is_configured", lambda: False)

    assert await health_api._check_rerank() == "off"


async def test_health_rerank_library_override_without_config_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(selfcheck, "any_library_rerank_enabled", _async_value(True))
    monkeypatch.setattr(rerank_svc, "is_configured", lambda: False)

    assert await health_api._check_rerank() == "not_configured"


async def test_health_rerank_probe_failure_is_fail(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(rerank_svc, "is_configured", lambda: True)

    async def fail_probe(*args, **kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(selfcheck, "probe_rerank", fail_probe)
    assert await health_api._check_rerank() == "fail"


def test_health_payload_does_not_treat_not_configured_as_ok():
    assert health_api._payload({"rerank": "off"})["status"] == "ready"
    payload = health_api._payload({"rerank": "not_configured"})
    assert payload["status"] == "not_ready"
    assert payload["components"]["rerank"] == "not_configured"
    assert payload["failure_codes"] == ["rerank_unavailable"]
