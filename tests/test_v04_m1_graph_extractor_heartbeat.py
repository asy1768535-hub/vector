from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from app.api.admin_operations import _SERVICE_TYPES


def test_operations_status_declares_graph_extractor():
    assert _SERVICE_TYPES == (
        "api",
        "embedding_worker",
        "cleanup_worker",
        "graph_extractor",
    )


def test_graph_extractor_one_shot_emits_only_online_and_stopping_heartbeats(monkeypatch):
    from app.services import heartbeat
    from app.workers import graph_extractor

    monkeypatch.setattr(graph_extractor.settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(graph_extractor, "validate_graph_extraction_startup", lambda _: None)
    monkeypatch.setattr(heartbeat, "make_instance_id", lambda: "host-1-test")
    beat = AsyncMock(return_value=True)
    monkeypatch.setattr(heartbeat, "beat", beat)

    asyncio.run(graph_extractor.run(watch=False))

    assert beat.await_count == 2
    first, second = beat.await_args_list
    assert first.args[:2] == ("graph_extractor", "host-1-test")
    assert first.kwargs["status"] == "online"
    assert first.kwargs["heartbeat_metadata"] == {
        "watch": False,
        "mode": "heartbeat_only",
        "milestone": "M1",
    }
    assert second.args[:2] == ("graph_extractor", "host-1-test")
    assert second.kwargs["status"] == "stopping"


def test_graph_extractor_shell_has_no_unit_consumption_or_business_db_imports():
    text = Path("app/workers/graph_extractor.py").read_text(encoding="utf-8")
    for forbidden in (
        "GraphExtractionJob",
        "GraphExtractionUnit",
        "async_session_factory",
        "FOR UPDATE",
        "SKIP LOCKED",
        "claim_token",
        "lease_expires_at",
        "httpx",
        "chat/completions",
    ):
        assert forbidden not in text


def test_local_scripts_manage_graph_extractor_without_printing_secrets():
    start = Path("scripts/start_local.ps1").read_text(encoding="utf-8")
    stop = Path("scripts/stop_local.ps1").read_text(encoding="utf-8")
    status = Path("scripts/status_local.ps1").read_text(encoding="utf-8")

    assert "GRAPH_EXTRACTION_ENABLED" in start
    assert "app.workers.graph_extractor" in start
    assert "graph_extractor.pid" in start
    assert "graph_extractor.pid" in stop
    assert "graph_extractor.pid" in status
    for text in (start, stop, status):
        assert "GRAPH_EXTRACTION_API_KEY" not in text
        assert "CHAT_API_KEY" not in text
