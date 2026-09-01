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


class _ModelOutlineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[OutlineItemV1] = Field(min_length=1, max_length=256)


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


def parse_model_outline(content: str) -> OutlinePayloadV1:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise KnowledgeArtifactGenerationError(
            "invalid_provider_json", "outline provider returned invalid JSON"
        ) from None
    try:
        parsed = _ModelOutlineResponse.model_validate(raw)
        return OutlinePayloadV1(generation_mode="model", items=parsed.items)
    except ValidationError:
        raise KnowledgeArtifactGenerationError(
            "invalid_provider_payload", "outline provider output violates the contract"
        ) from None


def summary_messages(source_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是中文文档摘要助手。只返回一个 JSON 对象，且只能有 summary 一个字段。"
                "请提炼文档的目的、范围、核心内容和结论，通常控制在 300 到 500 个汉字；"
                "短文可相应缩短。不要逐段照抄原文，不要添加原文没有的事实。"
            ),
        },
        {"role": "user", "content": source_text},
    ]


def outline_messages(source_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是中文文档大纲助手。只返回一个 JSON 对象，且只能有 items 一个字段。"
                "items 是章节数组，每项只能包含 level、title、path；level 为 1 到 6，"
                "path 是从一级章节到当前章节的标题数组，最后一项必须等于 title。"
                "请根据正文提炼真实章节结构，合并重复标题，不要把文件名当作章节，"
                "不要添加正文没有的内容，通常不超过 30 项。"
            ),
        },
        {"role": "user", "content": source_text},
    ]
