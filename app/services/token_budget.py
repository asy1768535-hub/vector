"""Conservative request token accounting for local extraction models."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _fallback_text_tokens(value: str) -> int:
    """Estimate mixed-language text without the unsafe ``len(text)/3`` rule.

    Chinese characters are counted as one token.  Latin/digit runs are counted
    at four characters per token and punctuation/whitespace gets one token.
    This intentionally overestimates short JSON fragments rather than hiding
    a context overflow behind a low output cap.
    """

    cjk = len(_CJK_RE.findall(value))
    rest = _CJK_RE.sub("", value)
    runs = re.findall(r"[A-Za-z0-9_]+|[^A-Za-z0-9_\s]", rest)
    non_cjk = sum(max(1, (len(run) + 3) // 4) for run in runs)
    whitespace = len(re.findall(r"\s+", rest))
    return max(1, cjk + non_cjk + whitespace)


@lru_cache(maxsize=4)
def _qwen_tokenizer(model_name: str):
    if not model_name.lower().startswith("qwen"):
        return None
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    except Exception:  # noqa: BLE001 - optional local dependency/cache
        return None


def estimate_text_tokens(value: str, *, model_name: str | None = None) -> int:
    if not isinstance(value, str):
        value = str(value)
    tokenizer = _qwen_tokenizer(model_name or "") if model_name else None
    if tokenizer is not None:
        try:
            return max(1, len(tokenizer.encode(value, add_special_tokens=False)))
        except Exception:  # noqa: BLE001 - fallback must remain available
            pass
    return _fallback_text_tokens(value)


def estimate_serialized_tokens(value: Any, *, model_name: str | None = None) -> int:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return estimate_text_tokens(encoded, model_name=model_name)


def estimate_chat_request_tokens(
    messages: list[dict[str, str]],
    *,
    response_format: Any | None = None,
    reserved_output_tokens: int = 0,
    model_name: str | None = None,
) -> int:
    """Count system/user text, JSON wrappers, response format and output reserve."""

    total = estimate_serialized_tokens(messages, model_name=model_name)
    if response_format is not None:
        total += estimate_serialized_tokens(response_format, model_name=model_name)
    # Chat templates add role markers, separators and an assistant priming
    # token.  Keep a fixed conservative envelope when the tokenizer cannot
    # render the provider's exact template locally.
    total += 32
    return total + max(0, reserved_output_tokens)


def split_text_to_token_budget(
    text: str,
    max_tokens: int,
    *,
    model_name: str | None = None,
) -> list[str]:
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if estimate_text_tokens(text, model_name=model_name) <= max_tokens:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        low, high = 1, len(text) - start
        best = 1
        while low <= high:
            mid = (low + high) // 2
            if estimate_text_tokens(text[start : start + mid], model_name=model_name) <= max_tokens:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        parts.append(text[start : start + best])
        start += best
    return parts
