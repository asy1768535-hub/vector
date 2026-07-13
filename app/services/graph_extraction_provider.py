from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx


ProviderErrorCategory = Literal["timeout", "network_error", "http_error"]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sanitize_text(value: Any, *, secret: str, limit: int) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return text[:limit]


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    provider_request_id: str | None
    raw_response: str
    request_payload_hash: str
    input_token_count: int | None
    output_token_count: int | None
    latency_ms: int
    finish_reason: str | None


class GraphExtractionProviderError(RuntimeError):
    def __init__(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        latency_ms: int,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.latency_ms = latency_ms
        self.status_code = status_code
        super().__init__(message[:1024])


class DashScopeGraphExtractor:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = _endpoint(base_url)
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def extract(self, messages: list[dict[str, str]]) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        request_hash = _payload_hash(payload)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise GraphExtractionProviderError(
                "timeout",
                "graph extraction provider timed out",
                latency_ms=latency_ms,
            ) from exc
        except httpx.RequestError as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise GraphExtractionProviderError(
                "network_error",
                "graph extraction provider network request failed",
                latency_ms=latency_ms,
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000)
        if not response.is_success:
            body = _sanitize_text(response.text, secret=self._api_key, limit=900)
            raise GraphExtractionProviderError(
                "http_error",
                f"graph extraction provider returned HTTP {response.status_code}: {body}",
                latency_ms=latency_ms,
                status_code=response.status_code,
            )

        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not text")
            usage = envelope.get("usage") or {}
            request_id = response.headers.get("x-request-id") or envelope.get("id")
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GraphExtractionProviderError(
                "http_error",
                "graph extraction provider returned an invalid response envelope",
                latency_ms=latency_ms,
                status_code=response.status_code,
            ) from exc

        return ProviderResponse(
            content=_sanitize_text(content, secret=self._api_key, limit=1_000_000),
            provider_request_id=_sanitize_text(
                request_id, secret=self._api_key, limit=255
            )
            or None,
            raw_response=_sanitize_text(
                response.text, secret=self._api_key, limit=1_000_000
            ),
            request_payload_hash=request_hash,
            input_token_count=_optional_int(usage.get("prompt_tokens")),
            output_token_count=_optional_int(usage.get("completion_tokens")),
            latency_ms=latency_ms,
            finish_reason=_sanitize_text(
                finish_reason, secret=self._api_key, limit=64
            )
            or None,
        )


class MockGraphExtractor:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._content = _canonical_json(payload)

    async def extract(self, messages: list[dict[str, str]]) -> ProviderResponse:
        request_hash = _payload_hash(
            {"messages": messages, "mock_response": self._content}
        )
        return ProviderResponse(
            content=self._content,
            provider_request_id="mock-deterministic",
            raw_response=self._content,
            request_payload_hash=request_hash,
            input_token_count=0,
            output_token_count=0,
            latency_ms=0,
            finish_reason="stop",
        )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) and value >= 0 else None
