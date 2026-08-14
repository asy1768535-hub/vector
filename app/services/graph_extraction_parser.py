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


def _dedupe_entity_local_ids(decoded):
    if not isinstance(decoded, dict) or not isinstance(decoded.get("entities"), list):
        return decoded
    entities = decoded["entities"]
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    changed = False
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        local_id = entity.get("local_id")
        if not isinstance(local_id, str) or not local_id:
            continue
        if local_id not in seen:
            seen.add(local_id)
            continue
        duplicate_ids.add(local_id)
        suffix = 2
        while True:
            candidate = f"{local_id[:120]}_{suffix}"
            if candidate not in seen:
                break
            suffix += 1
        entity["local_id"] = candidate
        seen.add(candidate)
        changed = True
    if not changed:
        return decoded
    relations = decoded.get("relations")
    if isinstance(relations, list):
        decoded["relations"] = [
            relation
            for relation in relations
            if not (
                isinstance(relation, dict)
                and (
                    relation.get("source_local_id") in duplicate_ids
                    or relation.get("target_local_id") in duplicate_ids
                )
            )
        ]
    return decoded


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
        return GraphExtractionPayload.model_validate(
            _dedupe_entity_local_ids(decoded)
        )
    except ValidationError as exc:
        raise GraphExtractionParseError(
            "invalid_schema",
            _validation_summary(exc),
        ) from exc
