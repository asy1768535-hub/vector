from __future__ import annotations

MINIMAX_OPENAI_BASE_URLS = frozenset(
    {
        "https://api.minimax.io/v1",
        "https://api.minimaxi.com/v1",
        # Verified MiniMax-M2.7 OpenAI-compatible gateway used by production.
        "https://model.rhzy.ai/v1",
    }
)
MINIMAX_OPENAI_MODELS = frozenset(
    {
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
    }
)
RHY_QWEN38_BASE_URL = "https://model.rhzy.ai/v1"
RHY_QWEN38_MODEL = "qwen3.8-27b-uncensored-fp8"


def _normalized_base_url(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/chat/completions")


def is_minimax_openai_contract(*, base_url: str, model: str) -> bool:
    return (
        _normalized_base_url(base_url) in MINIMAX_OPENAI_BASE_URLS
        and model in MINIMAX_OPENAI_MODELS
    )


def json_request_options(*, base_url: str, model: str) -> dict[str, object]:
    """Return the strict-JSON request profile for an approved provider."""

    if is_minimax_openai_contract(base_url=base_url, model=model):
        # MiniMax rejects temperature=0 and M2 structured output is prompt-driven.
        return {"temperature": 0.01, "reasoning_split": True}
    if (
        _normalized_base_url(base_url) == RHY_QWEN38_BASE_URL
        and model == RHY_QWEN38_MODEL
    ):
        # This proxy duplicates JSON prefixes when response_format is supplied.
        return {"temperature": 0}
    return {"temperature": 0, "response_format": {"type": "json_object"}}
