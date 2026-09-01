from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.services.token_budget import estimate_chat_request_tokens


log = logging.getLogger(__name__)


ProviderErrorCategory = Literal["timeout", "network_error", "http_error"]
DEEPSEEK_PROVIDER_NAME = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL_NAME = "deepseek-v4-pro"
DEEPSEEK_MODEL_NAMES = frozenset({DEEPSEEK_MODEL_NAME, "deepseek-v4-flash"})
LOCAL_PROVIDER_NAME = "openai-compatible"
LOCAL_BASE_URL = "http://10.0.10.2:8113/v1"
LOCAL_MODEL_NAME = "qwen3.5-9b"
GEMMA_PROVIDER_NAME = "gemma4"
GEMMA_BASE_URL = "https://model.rhzy.ai/v1"
GEMMA_DIRECT_BASE_URL = "http://10.0.10.2:8114/v1"
GEMMA_MODEL_NAME = "gemma4-31b-uncensored-bf16-256k-seq4"
QWEN38_PROVIDER_NAME = "qwen3.8"
QWEN38_BASE_URL = "https://model.rhzy.ai/v1"
QWEN38_MODEL_NAME = "qwen3.8-27b-uncensored-fp8"
NUEXTRACT_PROVIDER_NAME = "nuextract3"
NUEXTRACT_BASE_URL = "http://nuextract3-gpu0:8000/v1"
NUEXTRACT_MODEL_NAME = "nuextract3"
MINSTRAL_PROVIDER_NAME = "minstral3b"
MINSTRAL_BASE_URL = "http://graph-minstral-3b:8000/v1"
MINSTRAL_MODEL_NAME = "graph-minstral-3b"
QWEN3_DRAFT_PROVIDER_NAME = "qwen3-draft-4b"
QWEN3_DRAFT_BASE_URL = "http://graph-qwen3-4b:8000/v1"
QWEN3_DRAFT_MODEL_NAME = "graph-qwen3-4b"
GRAPH_DRAFT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "graph_draft",
        "schema": {
            "type": "object",
            "properties": {
                "entities": {"type": "array", "items": {"type": "object"}},
                "relations": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["entities", "relations"],
            "additionalProperties": False,
        },
    },
}


def graph_extraction_provider_name(*, base_url: str, model: str) -> str:
    contract = (base_url, model)
    if base_url == DEEPSEEK_BASE_URL and model in DEEPSEEK_MODEL_NAMES:
        return DEEPSEEK_PROVIDER_NAME
    if contract == (LOCAL_BASE_URL, LOCAL_MODEL_NAME):
        return LOCAL_PROVIDER_NAME
    if model == GEMMA_MODEL_NAME and base_url in {GEMMA_BASE_URL, GEMMA_DIRECT_BASE_URL}:
        return GEMMA_PROVIDER_NAME
    if contract == (QWEN38_BASE_URL, QWEN38_MODEL_NAME):
        return QWEN38_PROVIDER_NAME
    if contract == (NUEXTRACT_BASE_URL, NUEXTRACT_MODEL_NAME):
        return NUEXTRACT_PROVIDER_NAME
    if contract == (MINSTRAL_BASE_URL, MINSTRAL_MODEL_NAME):
        return MINSTRAL_PROVIDER_NAME
    if contract == (QWEN3_DRAFT_BASE_URL, QWEN3_DRAFT_MODEL_NAME):
        return QWEN3_DRAFT_PROVIDER_NAME
    raise ValueError("unsupported graph extraction provider contract")


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


def request_preview(
    messages: list[dict[str, Any]],
    *,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    """Return a development-safe request preview without document contents."""

    return {
        "messages": [
            {
                "role": message.get("role"),
                "content_chars": len(
                    message["content"]
                    if isinstance(message.get("content"), str)
                    else _canonical_json(message.get("content", ""))
                ),
                "content_sha256": hashlib.sha256(
                    (
                        message["content"]
                        if isinstance(message.get("content"), str)
                        else _canonical_json(message.get("content", ""))
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for message in messages
        ],
        "max_output_tokens": max_output_tokens,
        "estimated_request_tokens": estimate_chat_request_tokens(
            messages,
            response_format={"type": "json_object"},
            reserved_output_tokens=max_output_tokens or 0,
        ),
    }


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


class OpenAICompatibleGraphExtractor:
    supports_concept_inventory = True

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        max_output_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        chat_template_kwargs: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_output_tokens is not None and (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer or None")
        self._endpoint = _endpoint(base_url)
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._response_format = response_format or {"type": "json_object"}
        self._chat_template_kwargs = chat_template_kwargs
        self._transport = transport

    def with_output_budget(self, max_output_tokens: int) -> "OpenAICompatibleGraphExtractor":
        clone = object.__new__(type(self))
        clone._endpoint = self._endpoint
        clone._model = self._model
        clone._api_key = self._api_key
        clone._timeout_seconds = self._timeout_seconds
        clone._max_output_tokens = max_output_tokens
        clone._response_format = self._response_format
        clone._chat_template_kwargs = self._chat_template_kwargs
        clone._transport = self._transport
        return clone

    async def _extract_once(
        self,
        messages: list[dict[str, str]],
        *,
        include_response_format: bool,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        if include_response_format:
            payload["response_format"] = self._response_format
        if any(marker in self._model.lower() for marker in ("qwen", "gemma")):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self._chat_template_kwargs is not None:
            payload["chat_template_kwargs"] = self._chat_template_kwargs
        if self._model.lower() == "deepseek-v4-flash":
            payload["thinking"] = {"type": "disabled"}
        if self._max_output_tokens is not None:
            payload["max_tokens"] = self._max_output_tokens
        request_hash = _payload_hash(payload)
        started = time.perf_counter()
        log.info(
            "graph provider request endpoint=%s model=%s input_tokens=%s output_budget=%s response_format=%s",
            self._endpoint,
            self._model,
            request_preview(messages, max_output_tokens=0)["estimated_request_tokens"],
            self._max_output_tokens,
            include_response_format,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "graph provider redacted request preview=%s",
                request_preview(messages, max_output_tokens=self._max_output_tokens),
            )
        try:
            headers = (
                {"Authorization": f"Bearer {self._api_key}"}
                if self._api_key
                else {}
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            log.warning(
                "graph provider timeout endpoint=%s model=%s latency_ms=%s",
                self._endpoint,
                self._model,
                latency_ms,
            )
            raise GraphExtractionProviderError(
                "timeout",
                "graph extraction provider timed out",
                latency_ms=latency_ms,
            ) from exc
        except httpx.RequestError as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            log.warning(
                "graph provider network error endpoint=%s model=%s latency_ms=%s",
                self._endpoint,
                self._model,
                latency_ms,
            )
            raise GraphExtractionProviderError(
                "network_error",
                "graph extraction provider network request failed",
                latency_ms=latency_ms,
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000)
        if not response.is_success:
            body = _sanitize_text(response.text, secret=self._api_key, limit=900)
            log.warning(
                "graph provider response endpoint=%s model=%s status=%s latency_ms=%s",
                self._endpoint,
                self._model,
                response.status_code,
                latency_ms,
            )
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

        log.info(
            "graph provider response endpoint=%s model=%s status=%s input_tokens=%s output_tokens=%s latency_ms=%s",
            self._endpoint,
            self._model,
            response.status_code,
            _optional_int(usage.get("prompt_tokens")),
            _optional_int(usage.get("completion_tokens")),
            latency_ms,
        )
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

    async def extract(self, messages: list[dict[str, str]]) -> ProviderResponse:
        try:
            return await self._extract_once(messages, include_response_format=True)
        except GraphExtractionProviderError as exc:
            message = str(exc).casefold()
            if exc.status_code not in {400, 404, 422} or not any(
                marker in message
                for marker in ("response_format", "json_object", "structured output")
            ):
                raise
            log.warning(
                "graph provider response_format unsupported; retrying without it endpoint=%s model=%s",
                self._endpoint,
                self._model,
            )
            return await self._extract_once(messages, include_response_format=False)


class NuExtractGraphDraftExtractor(OpenAICompatibleGraphExtractor):
    """NuExtract's official vLLM template contract for structured draft extraction."""

    def __init__(
        self,
        *,
        template: dict[str, Any],
        base_url: str = NUEXTRACT_BASE_URL,
        model: str = NUEXTRACT_MODEL_NAME,
        api_key: str = "",
        timeout_seconds: float = 120.0,
        max_output_tokens: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(template, dict) or not template:
            raise ValueError("NuExtract template must be a non-empty object")
        super().__init__(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            chat_template_kwargs={
                "template": _canonical_json(template),
                "enable_thinking": False,
            },
            transport=transport,
        )

    async def extract_context(self, context_text: str) -> ProviderResponse:
        if not isinstance(context_text, str) or not context_text.strip():
            raise ValueError("NuExtract context must be non-empty text")
        return await self.extract(
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": context_text}],
                }
            ]
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
