from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.knowledge_artifact import (
    OutlineItemV1,
    OutlinePayloadV1,
    SummaryPayloadV1,
)


_WHITESPACE = re.compile(r"\s+")


class KnowledgeArtifactGenerationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ModelSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=16_000)


def _normalized_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def deterministic_summary(source_text: str) -> SummaryPayloadV1:
    if not isinstance(source_text, str):
        raise TypeError("source text must be a string")
    normalized = _normalized_text(source_text)
    if not normalized:
        raise ValueError("source text must not be blank")
    summary = normalized[:16_000]
    return SummaryPayloadV1(
        summary=summary,
        generation_mode="deterministic",
        source_character_count=len(source_text),
        truncated=len(normalized) > len(summary),
    )


def _clean_path(raw_path: Any) -> list[str]:
    if not isinstance(raw_path, list):
        return []
    cleaned: list[str] = []
    for raw_segment in raw_path[:6]:
        if not isinstance(raw_segment, str):
            return []
        segment = _normalized_text(raw_segment)[:512]
        if not segment:
            return []
        cleaned.append(segment)
    return cleaned


def deterministic_outline(
    title_paths: list[Any], *, document_title: str | None
) -> OutlinePayloadV1:
    seen: set[tuple[str, ...]] = set()
    items: list[OutlineItemV1] = []
    for raw_path in title_paths:
        path = _clean_path(raw_path)
        for depth in range(1, len(path) + 1):
            prefix = tuple(path[:depth])
            if prefix in seen:
                continue
            seen.add(prefix)
            items.append(
                OutlineItemV1(
                    level=depth,
                    title=prefix[-1],
                    path=list(prefix),
                )
            )
            if len(items) == 256:
                break
        if len(items) == 256:
            break
    if not items:
        fallback = _normalized_text(document_title or "")[:512]
        if not fallback:
            raise ValueError("document title is required when no outline path exists")
        items.append(OutlineItemV1(level=1, title=fallback, path=[fallback]))
    return OutlinePayloadV1(generation_mode="deterministic", items=items)


def parse_model_summary(
    content: str,
    *,
    source_character_count: int,
    source_truncated: bool,
) -> SummaryPayloadV1:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise KnowledgeArtifactGenerationError(
            "invalid_provider_json", "summary provider returned invalid JSON"
        ) from None
    try:
        parsed = _ModelSummaryResponse.model_validate(raw)
        return SummaryPayloadV1(
            summary=parsed.summary,
            generation_mode="model",
            source_character_count=source_character_count,
            truncated=source_truncated,
        )
    except ValidationError:
        raise KnowledgeArtifactGenerationError(
            "invalid_provider_payload", "summary provider output violates the contract"
        ) from None


def summary_messages(source_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Return one JSON object with exactly one field named summary. "
                "Summarize only the supplied source and do not add unsupported facts."
            ),
        },
        {"role": "user", "content": source_text},
    ]
