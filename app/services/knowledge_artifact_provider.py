from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx


ProviderErrorCategory = Literal[
    "timeout", "network_error", "http_error", "invalid_envelope"
]
DEEPSEEK_PROVIDER_NAME = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL_NAME = "deepseek-v4-pro"


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class SummaryProviderResponse:
    content: str = field(repr=False)
    request_payload_hash: str
    provider_request_id: str | None
    latency_ms: int
    input_token_count: int | None
    output_token_count: int | None


class KnowledgeArtifactProviderError(RuntimeError):
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
        super().__init__(message[:255])


class OpenAICompatibleSummaryProvider:
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

    async def generate(
        self, messages: list[dict[str, str]]
    ) -> SummaryProviderResponse:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        payload_hash = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
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
        except httpx.TimeoutException:
            raise KnowledgeArtifactProviderError(
                "timeout",
                "knowledge artifact provider timed out",
                latency_ms=round((time.perf_counter() - started) * 1000),
            ) from None
        except httpx.RequestError:
            raise KnowledgeArtifactProviderError(
                "network_error",
                "knowledge artifact provider network request failed",
                latency_ms=round((time.perf_counter() - started) * 1000),
            ) from None

        latency_ms = round((time.perf_counter() - started) * 1000)
        if not response.is_success:
            raise KnowledgeArtifactProviderError(
                "http_error",
                f"knowledge artifact provider returned HTTP {response.status_code}",
                latency_ms=latency_ms,
                status_code=response.status_code,
            )
        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
            request_id = envelope.get("id") or response.headers.get("x-request-id")
            usage = envelope.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise KnowledgeArtifactProviderError(
                "invalid_envelope",
                "knowledge artifact provider returned an invalid response envelope",
                latency_ms=latency_ms,
                status_code=response.status_code,
            ) from None
        return SummaryProviderResponse(
            content=content[:100_000],
            request_payload_hash=payload_hash,
            provider_request_id=str(request_id)[:255] if request_id else None,
            latency_ms=latency_ms,
            input_token_count=_optional_int(usage.get("prompt_tokens")),
            output_token_count=_optional_int(usage.get("completion_tokens")),
        )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) and value >= 0 else None
