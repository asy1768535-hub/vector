from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.workers import cleanup


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


class _Session:
    def begin(self):
        return _Transaction()


class _SessionContext:
    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *_args):
        return None


def test_operations_maintenance_runs_once_in_one_shot_and_at_interval_in_watch():
    result = SimpleNamespace(
        request_records_deleted=3,
        rate_windows_deleted=2,
        answer_leases_deleted=1,
    )
    maintenance = AsyncMock(return_value=result)
    with (
        patch.object(settings, "public_api_operations_enabled", True),
        patch.object(settings, "public_api_cleanup_interval_seconds", 60),
        patch.object(cleanup, "async_session_factory", return_value=_SessionContext()),
        patch.object(
            cleanup.public_api_operations_service,
            "run_public_api_operations_maintenance",
            new=maintenance,
        ),
    ):
        one_shot = asyncio.run(
            cleanup._run_public_api_operations_maintenance_if_due(
                watch=False,
                last_run_monotonic=None,
                current_monotonic=100,
            )
        )
        assert one_shot == 100
        assert (
            asyncio.run(
                cleanup._run_public_api_operations_maintenance_if_due(
                    watch=False,
                    last_run_monotonic=one_shot,
                    current_monotonic=1_000,
                )
            )
            == one_shot
        )
        assert (
            asyncio.run(
                cleanup._run_public_api_operations_maintenance_if_due(
                    watch=True,
                    last_run_monotonic=one_shot,
                    current_monotonic=159,
                )
            )
            == one_shot
        )
        watch_run = asyncio.run(
            cleanup._run_public_api_operations_maintenance_if_due(
                watch=True,
                last_run_monotonic=one_shot,
                current_monotonic=160,
            )
        )
    assert watch_run == 160
    assert maintenance.await_count == 2


def test_operations_maintenance_disabled_is_inert_and_failures_are_swallowed(caplog):
    maintenance = AsyncMock(side_effect=RuntimeError("private request content"))
    with patch.object(settings, "public_api_operations_enabled", False):
        assert (
            asyncio.run(
                cleanup._run_public_api_operations_maintenance_if_due(
                    watch=True,
                    last_run_monotonic=None,
                    current_monotonic=100,
                )
            )
            is None
        )
    maintenance.assert_not_awaited()

    with (
        patch.object(settings, "public_api_operations_enabled", True),
        patch.object(cleanup, "async_session_factory", return_value=_SessionContext()),
        patch.object(
            cleanup.public_api_operations_service,
            "run_public_api_operations_maintenance",
            new=maintenance,
        ),
    ):
        assert (
            asyncio.run(
                cleanup._run_public_api_operations_maintenance_if_due(
                    watch=True,
                    last_run_monotonic=None,
                    current_monotonic=200,
                )
            )
            == 200
        )
    assert "private request content" not in caplog.text
    assert "maintenance_failed" in caplog.text
