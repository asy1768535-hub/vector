from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import ValidationError

from app.schemas.graph_extraction import GraphExtractionPayload


_OUTER_JSON_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)


class GraphExtractionParseError(ValueError):
    def __init__(
        self,
        parse_status: Literal["invalid_json", "invalid_schema"],
        message: str,
    ) -> None:
        self.parse_status = parse_status
        super().__init__(message[:512])


def _unwrap_outer_fence(raw_content: str) -> str:
    value = (raw_content or "").strip()
    match = _OUTER_JSON_FENCE.fullmatch(value)
    if match is not None:
        return match.group("body").strip()
    return value


def _validation_summary(exc: ValidationError) -> str:
    errors = exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    parts: list[str] = []
    for error in errors[:5]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "payload"
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{location}: {message}")
    return "invalid extraction schema: " + "; ".join(parts)


def parse_graph_extraction_output(raw_content: str) -> GraphExtractionPayload:
    value = _unwrap_outer_fence(raw_content)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise GraphExtractionParseError(
            "invalid_json",
            "model response is not valid JSON",
        ) from exc

    try:
        return GraphExtractionPayload.model_validate(decoded)
    except ValidationError as exc:
        raise GraphExtractionParseError(
            "invalid_schema",
            _validation_summary(exc),
        ) from exc
