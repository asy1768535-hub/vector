from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.services.graph_extraction_provider import GraphExtractionProviderError
from app.services.graph_extraction_rate_limit import (
    call_graph_extraction_provider,
    graph_extraction_provider_gate_snapshot,
)


def test_provider_gate_bounds_concurrency() -> None:
    async def scenario() -> tuple[int, int]:
        in_flight = 0
        maximum = 0

        async def call() -> str:
            nonlocal in_flight, maximum
            in_flight += 1
            maximum = max(maximum, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return "ok"

        results = await asyncio.gather(
            *(
                call_graph_extraction_provider(
                    call,
                    concurrency=2,
                    max_retries=0,
                    backoff_base_seconds=0.001,
                )
                for _ in range(6)
            )
        )
        return maximum, len(results)

    maximum, count = asyncio.run(scenario())
    assert maximum == 2
    assert count == 6


def test_provider_gates_are_isolated_by_role() -> None:
    async def scenario() -> tuple[int, int, int, int]:
        in_flight = {"draft": 0, "review": 0}
        maximum = {"draft": 0, "review": 0}

        async def call(role: str) -> str:
            in_flight[role] += 1
            maximum[role] = max(maximum[role], in_flight[role])
            await asyncio.sleep(0.01)
            in_flight[role] -= 1
            return role

        await asyncio.gather(
            *(
                call_graph_extraction_provider(
                    lambda role=role: call(role),
                    concurrency=2 if role == "draft" else 4,
                    max_retries=0,
                    backoff_base_seconds=0.001,
                    gate_key=role,
                )
                for role in ("draft", "review")
                for _ in range(8)
            )
        )
        draft = graph_extraction_provider_gate_snapshot(gate_key="draft")
        review = graph_extraction_provider_gate_snapshot(gate_key="review")
        assert draft is not None
        assert review is not None
        return (
            maximum["draft"],
            maximum["review"],
            draft.configured_concurrency,
            review.configured_concurrency,
        )

    assert asyncio.run(scenario()) == (2, 4, 2, 4)


def test_429_retries_and_reduces_effective_concurrency() -> None:
    async def scenario() -> tuple[int, int]:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise GraphExtractionProviderError(
                    "http_error",
                    "limited",
                    latency_ms=1,
                    status_code=429,
                )
            return "ok"

        with patch(
            "app.services.graph_extraction_rate_limit.random.uniform",
            return_value=0.0,
        ):
            result = await call_graph_extraction_provider(
                call,
                concurrency=4,
                max_retries=2,
                backoff_base_seconds=0.001,
            )
        snapshot = graph_extraction_provider_gate_snapshot()
        assert snapshot is not None
        assert result == "ok"
        return attempts, snapshot.effective_concurrency

    attempts, concurrency = asyncio.run(scenario())
    assert attempts == 2
    assert concurrency == 2


def test_non_retryable_4xx_fails_once() -> None:
    async def scenario() -> int:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            raise GraphExtractionProviderError(
                "http_error",
                "bad request",
                latency_ms=1,
                status_code=400,
            )

        with pytest.raises(GraphExtractionProviderError):
            await call_graph_extraction_provider(
                call,
                concurrency=2,
                max_retries=2,
                backoff_base_seconds=0.001,
            )
        return attempts

    assert asyncio.run(scenario()) == 1
