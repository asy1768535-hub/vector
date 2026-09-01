"""Process-local provider admission, retry, and adaptive concurrency."""

from __future__ import annotations

import asyncio
import random
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.services.graph_extraction_provider import GraphExtractionProviderError


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderGateSnapshot:
    configured_concurrency: int
    effective_concurrency: int
    in_flight: int
    throttled_count: int
    retry_count: int


class _ProviderGate:
    def __init__(self, concurrency: int) -> None:
        self._configured = concurrency
        self._effective = concurrency
        self._in_flight = 0
        self._stable_successes = 0
        self._throttled_count = 0
        self._retry_count = 0
        self._not_before = 0.0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            while True:
                delay = self._not_before - time.monotonic()
                if self._in_flight < self._effective and delay <= 0:
                    self._in_flight += 1
                    return
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                else:
                    await self._condition.wait()

    async def release(self) -> None:
        async with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()

    async def record_success(self) -> None:
        async with self._condition:
            self._stable_successes += 1
            if self._effective < self._configured and self._stable_successes >= self._effective * 10:
                self._effective += 1
                self._stable_successes = 0
                self._condition.notify_all()

    async def record_retry(self, delay: float, *, throttled: bool) -> None:
        async with self._condition:
            self._retry_count += 1
            self._not_before = max(self._not_before, time.monotonic() + delay)
            if throttled:
                self._throttled_count += 1
                self._effective = max(1, self._effective // 2)
                self._stable_successes = 0
            self._condition.notify_all()

    def snapshot(self) -> ProviderGateSnapshot:
        return ProviderGateSnapshot(
            configured_concurrency=self._configured,
            effective_concurrency=self._effective,
            in_flight=self._in_flight,
            throttled_count=self._throttled_count,
            retry_count=self._retry_count,
        )


_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, _ProviderGate]
] = weakref.WeakKeyDictionary()


def _gate(concurrency: int, *, gate_key: str = "default") -> _ProviderGate:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("provider concurrency must be a positive integer")
    if not isinstance(gate_key, str) or not gate_key.strip():
        raise ValueError("provider gate key must be non-empty")
    loop = asyncio.get_running_loop()
    gates = _GATES.setdefault(loop, {})
    gate = gates.get(gate_key)
    if gate is None or gate.snapshot().configured_concurrency != concurrency:
        gate = _ProviderGate(concurrency)
        gates[gate_key] = gate
    return gate


def _retryable(exc: GraphExtractionProviderError) -> tuple[bool, bool]:
    throttled = exc.status_code == 429
    server_error = exc.status_code is not None and 500 <= exc.status_code <= 599
    retryable = exc.category in {"timeout", "network_error"} or throttled or server_error
    return retryable, throttled


async def call_graph_extraction_provider(
    call: Callable[[], Awaitable[T]],
    *,
    concurrency: int,
    max_retries: int,
    backoff_base_seconds: float,
    gate_key: str = "default",
) -> T:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("provider max retries must be a non-negative integer")
    if backoff_base_seconds <= 0:
        raise ValueError("provider backoff must be positive")
    gate = _gate(concurrency, gate_key=gate_key)
    attempt = 0
    while True:
        error: GraphExtractionProviderError | None = None
        await gate.acquire()
        try:
            result = await call()
        except GraphExtractionProviderError as exc:
            error = exc
            retryable, throttled = _retryable(exc)
        else:
            await gate.record_success()
            return result
        finally:
            await gate.release()
        assert error is not None
        if not retryable or attempt >= max_retries:
            raise error
        delay = backoff_base_seconds * (2**attempt) * random.uniform(0.8, 1.2)
        attempt += 1
        await gate.record_retry(delay, throttled=throttled)


def graph_extraction_provider_gate_snapshot(
    *, gate_key: str = "default"
) -> ProviderGateSnapshot | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    gate = (_GATES.get(loop) or {}).get(gate_key)
    return gate.snapshot() if gate is not None else None
