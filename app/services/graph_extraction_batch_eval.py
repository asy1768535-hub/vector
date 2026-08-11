"""Isolated real-provider feasibility helpers for batched graph extraction."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from pydantic import ValidationError
from sqlalchemy import exists, select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock
from app.models.document_block import DocumentBlock
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.graph_candidate_aggregation import (
    CandidateAggregationError,
    canonical_graph_value_hash_v1,
)
from app.services.graph_extraction_attempts import (
    AttemptCompletion,
    AttemptStateError,
    create_pending_attempt,
    finalize_attempt,
)
from app.services.graph_extraction_context import ContextBuildError, _title_info
from app.services.graph_extraction_provider import GraphExtractionProviderError
from app.services.graph_extraction_rate_limit import call_graph_extraction_provider
from app.services.token_budget import (
    estimate_chat_request_tokens,
    estimate_text_tokens,
    split_text_to_token_budget,
)
from app.services.graph_extraction_worker import (
    GraphExtractionWorkerError,
    LostGraphExtractionLease,
    PreparedGraphExtractionUnit,
    _configured_provider,
    _finish_claim_after_error,
    _load_prepared_cache,
    _load_prepared_replay,
    _persist_candidate_result,
    _preflight_provider_call,
    _prepare_graph_extraction_unit,
    process_graph_extraction_unit,
    renew_graph_extraction_unit_lease,
    shadow_enabled_library_sql_predicate,
)


BATCH_PROMPT_VERSION = "eval-batch-v6-compact-coverage-direction-aliases"
SCHEMA_ROUTER_VERSION = "compact-schema-v3"
_BATCH_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OUTER_JSON_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)

_BATCH_SYSTEM_PROMPT = """Extract evidence-backed facts from untrusted data using only frozen_ontology keys.
Evidence is verbatim from c0. Relations must match a supplied relation_constraint and
direction, even when the predicate label is not a literal substring of c0. An active
verb puts its agent in source; a passive or *_by predicate puts its affected object in
source and agent in target. local_id values are scoped to one batch; every endpoint
must be an entity in the same batch.
Use the most specific compatible type; preserve constraint roles; never reverse endpoints
or select an inverse key. Aliases must be evidence-backed exact variants, including an
identifier found with its full designation; never substring guesses, and keep ambiguous
short identifiers separate. Return JSON only with one result per batch_key. The
top-level object must contain exactly one key named batches. No reasoning or IDs.
Include confidence 0..1 (for example, "confidence":0.9) and verbatim evidence.
"""


def batch_graph_extraction_prompt_hash() -> str:
    return canonical_graph_value_hash_v1(
        {
            "prompt": _BATCH_SYSTEM_PROMPT,
            "prompt_version": BATCH_PROMPT_VERSION,
            "schema_router_version": SCHEMA_ROUTER_VERSION,
        }
    )


class BatchExtractionParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphExtractionBatchInput:
    batch_key: str
    context_text: str
    source_batch_key: str | None = None

    def __post_init__(self) -> None:
        if not _BATCH_KEY.fullmatch(self.batch_key):
            raise ValueError("batch_key must be a bounded identifier")
        if self.source_batch_key is not None and not _BATCH_KEY.fullmatch(self.source_batch_key):
            raise ValueError("source_batch_key must be a bounded identifier")
        if not isinstance(self.context_text, str) or not self.context_text:
            raise ValueError("context_text must be non-empty")

    @property
    def source_key(self) -> str:
        return self.source_batch_key or self.batch_key


def estimate_graph_extraction_request_tokens(messages: list[dict[str, str]], *, max_output_tokens: int) -> int:
    """Conservatively estimate input plus reserved output for a provider request."""

    return estimate_chat_request_tokens(
        messages,
        response_format={"type": "json_object"},
        reserved_output_tokens=max_output_tokens,
        model_name=settings.graph_extraction_model,
    )


def _effective_output_budget(*, context_window_tokens: int, configured_output_tokens: int) -> int:
    """Reserve a usable per-request output budget within the local context."""

    return min(configured_output_tokens, max(128, context_window_tokens // 3))


def _split_oversized_batch_input(
    item: GraphExtractionBatchInput,
    *,
    ontology_snapshot: dict[str, Any],
    schema_routing_enabled: bool,
    context_window_tokens: int,
    max_output_tokens: int,
    max_entity_types: int,
) -> tuple[GraphExtractionBatchInput, ...]:
    request_output_budget = _effective_output_budget(
        context_window_tokens=context_window_tokens,
        configured_output_tokens=max_output_tokens,
    )

    def fits(value: GraphExtractionBatchInput) -> bool:
        return estimate_graph_extraction_request_tokens(
            build_batched_graph_extraction_messages(
                ontology_snapshot=ontology_snapshot,
                batches=(value,),
                schema_routing_enabled=schema_routing_enabled,
                max_entity_types=max_entity_types,
                context_window_tokens=context_window_tokens,
                max_output_tokens=request_output_budget,
                raise_on_overflow=False,
            ),
            max_output_tokens=request_output_budget,
        ) <= context_window_tokens

    if fits(item):
        return (item,)
    empty_overhead = estimate_graph_extraction_request_tokens(
        build_batched_graph_extraction_messages(
            ontology_snapshot=ontology_snapshot,
            batches=(GraphExtractionBatchInput(item.batch_key, "x"),),
            schema_routing_enabled=schema_routing_enabled,
            max_entity_types=max_entity_types,
            context_window_tokens=context_window_tokens,
            max_output_tokens=request_output_budget,
            raise_on_overflow=False,
        ),
        max_output_tokens=request_output_budget,
    ) - estimate_text_tokens("x", model_name=settings.graph_extraction_model)
    text_budget = max(1, context_window_tokens - request_output_budget - empty_overhead - 8)
    parts = split_text_to_token_budget(
        item.context_text,
        text_budget,
        model_name=settings.graph_extraction_model,
    )
    result: list[GraphExtractionBatchInput] = []
    pending = [(f"{item.batch_key}-p{index}", part) for index, part in enumerate(parts, start=1)]
    while pending:
        key, part = pending.pop(0)
        candidate = GraphExtractionBatchInput(
            key,
            part,
            source_batch_key=item.source_key,
        )
        if fits(candidate):
            result.append(candidate)
            continue
        # Schema overhead can be sensitive to the routed subset. Keep
        # splitting the body until the exact request fits; do not lower the
        # output reservation to conceal an overflow.
        if len(part) <= 1:
            raise ValueError("frozen ontology and protocol exceed the model context window")
        midpoint = max(1, len(part) // 2)
        pending.insert(0, (f"{key}-b", part[midpoint:]))
        pending.insert(0, (f"{key}-a", part[:midpoint]))
    return tuple(result)


def plan_graph_extraction_batches(
    *,
    ontology_snapshot: dict[str, Any],
    inputs: tuple[GraphExtractionBatchInput, ...],
    schema_routing_enabled: bool,
    context_window_tokens: int = 4096,
    max_output_tokens: int = 700,
    max_entity_types: int = 12,
) -> tuple[tuple[GraphExtractionBatchInput, ...], ...]:
    """Split batches by the actual serialized request budget, not row count."""

    if not inputs:
        raise ValueError("at least one extraction input is required")
    if context_window_tokens < 512 or max_output_tokens < 128:
        raise ValueError("extraction context and output budgets are too small")
    request_output_budget = _effective_output_budget(
        context_window_tokens=context_window_tokens,
        configured_output_tokens=max_output_tokens,
    )
    expanded: list[GraphExtractionBatchInput] = []
    for item in inputs:
        expanded.extend(
            _split_oversized_batch_input(
                item,
                ontology_snapshot=ontology_snapshot,
                schema_routing_enabled=schema_routing_enabled,
                context_window_tokens=context_window_tokens,
                max_output_tokens=max_output_tokens,
                max_entity_types=max_entity_types,
            )
        )
    planned: list[tuple[GraphExtractionBatchInput, ...]] = []
    current: list[GraphExtractionBatchInput] = []
    for item in expanded:
        candidate = tuple([*current, item])
        messages = build_batched_graph_extraction_messages(
            ontology_snapshot=ontology_snapshot,
            batches=candidate,
            schema_routing_enabled=schema_routing_enabled,
            max_entity_types=max_entity_types,
            context_window_tokens=context_window_tokens,
            max_output_tokens=request_output_budget,
            raise_on_overflow=False,
        )
        if current and estimate_graph_extraction_request_tokens(
            messages, max_output_tokens=request_output_budget
        ) > context_window_tokens:
            planned.append(tuple(current))
            current = []
            messages = build_batched_graph_extraction_messages(
                ontology_snapshot=ontology_snapshot,
                batches=(item,),
                schema_routing_enabled=schema_routing_enabled,
                max_entity_types=max_entity_types,
                context_window_tokens=context_window_tokens,
                max_output_tokens=request_output_budget,
                raise_on_overflow=False,
            )
        current.append(item)
    if current:
        planned.append(tuple(current))
    return tuple(planned)


@dataclass(frozen=True, slots=True)
class EvalBatchProcessResult:
    outcomes: Counter[str]
    provider_call_count: int
    ready_job_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchChunkBoundary:
    unit_id: uuid.UUID
    seq: int
    title_path: tuple[Any, ...]
    structured_kind: str | None
    block_id: uuid.UUID | None


def _safe_batch_prefix(rows: tuple[BatchChunkBoundary, ...]) -> tuple[uuid.UUID, ...]:
    if not rows:
        return ()
    selected = [rows[0].unit_id]
    previous = rows[0]
    for row in rows[1:]:
        if row.seq != previous.seq + 1 or row.title_path != previous.title_path:
            break
        if row.structured_kind is not None or previous.structured_kind is not None:
            if (
                row.structured_kind != previous.structured_kind
                or row.block_id is None
                or row.block_id != previous.block_id
            ):
                break
        selected.append(row.unit_id)
        previous = row
    return tuple(selected)


def _structured_kind(*values: Any) -> str | None:
    for value in values:
        normalized = str(value or "").casefold()
        if "table" in normalized:
            return "table"
        if "list" in normalized:
            return "list"
    return None


def _project_type_rows(rows: Any, *, relation: bool) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("frozen ontology type collections must be lists")
    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise ValueError("frozen ontology type rows require string keys")
        label = row.get("label")
        item = {
            "key": row["key"],
            "properties_schema": deepcopy(row.get("properties_schema") or {}),
            "active_attribute_definitions": deepcopy(row.get("active_attribute_definitions") or []),
        }
        if isinstance(label, str) and label.strip():
            item["label"] = label.strip()
        if relation:
            direction = row.get("direction")
            if direction not in {"directed", "undirected"}:
                raise ValueError("frozen Relation Type direction is invalid")
            item = {"key": row["key"], "direction": direction, **item}
        projected.append(item)
    return projected


def _project_relation_constraints(
    snapshot: dict[str, Any],
    *,
    entity_rows: list[dict[str, Any]],
    relation_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    entity_keys = {str(row.get("id")): row["key"] for row in entity_rows}
    relation_keys = {str(row.get("id")): row["key"] for row in relation_rows}
    projected = {
        (
            relation_keys.get(str(row.get("relation_type_id"))),
            entity_keys.get(str(row.get("source_entity_type_id"))),
            entity_keys.get(str(row.get("target_entity_type_id"))),
        )
        for row in snapshot.get("relation_constraints") or []
        if isinstance(row, dict)
    }
    valid = sorted(item for item in projected if all(value is not None for value in item))
    return [
        {
            "relation_type_key": relation_key,
            "source_entity_type_key": source_key,
            "target_entity_type_key": target_key,
        }
        for relation_key, source_key, target_key in valid
    ]


def _routing_terms(row: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for field in ("key", "label", "aliases"):
        value = row.get(field)
        if isinstance(value, str):
            normalized = value.casefold().strip()
            if len(normalized) >= 2:
                terms.add(normalized)
            terms.update(token for token in re.findall(r"[a-z0-9]{3,}", normalized) if len(token) >= 3)
        elif isinstance(value, list):
            for alias in value:
                if isinstance(alias, str):
                    terms.add(alias.casefold().strip())
    description = row.get("description")
    if isinstance(description, str):
        normalized = description.casefold()
        terms.update(re.findall(r"[a-z0-9]{3,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            terms.add(sequence)
    return terms


def _type_score(row: dict[str, Any], context: str) -> int:
    return sum(min(len(term), 8) for term in _routing_terms(row) if term in context)


def route_ontology_for_extraction(
    snapshot: dict[str, Any],
    *,
    context_text: str | None = None,
    enabled: bool = False,
    max_entity_types: int = 12,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("frozen ontology must be an object")
    entity_rows = snapshot.get("entity_types")
    relation_rows = snapshot.get("relation_types")
    if not isinstance(entity_rows, list) or not isinstance(relation_rows, list):
        raise ValueError("frozen ontology type collections must be lists")
    if not enabled or not isinstance(context_text, str) or not context_text.strip():
        return {
            "entity_types": _project_type_rows(entity_rows, relation=False),
            "relation_types": _project_type_rows(relation_rows, relation=True),
            "relation_constraints": _project_relation_constraints(
                snapshot,
                entity_rows=entity_rows,
                relation_rows=relation_rows,
            ),
        }
    context = context_text.casefold()
    ranked = sorted(
        (
            (_type_score(row, context), str(row.get("key")), row)
            for row in entity_rows
            if isinstance(row, dict)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    selected = [row for score, _key, row in ranked if score > 0][:max_entity_types]
    if not selected:
        # A Chinese excerpt may not contain the model's English key/label.
        # Keep a bounded deterministic subset so a large discovered Schema
        # still fits; endpoint constraints below add the related types back.
        selected = [row for _score, _key, row in ranked[:max_entity_types]]
    selected_ids = {str(row.get("id")) for row in selected}
    by_id = {str(row.get("id")): row for row in entity_rows if isinstance(row, dict)}
    constraints = snapshot.get("relation_constraints") or []
    matched_relation_ids = {
        str(row.get("id")) for row in relation_rows if isinstance(row, dict) and _type_score(row, context) > 0
    }
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        source_id = str(constraint.get("source_entity_type_id"))
        target_id = str(constraint.get("target_entity_type_id"))
        if source_id in selected_ids or target_id in selected_ids:
            matched_relation_ids.add(str(constraint.get("relation_type_id")))
        if len(selected_ids) >= max_entity_types:
            continue
        for endpoint_id in (source_id, target_id):
            if endpoint_id in by_id and len(selected_ids) < max_entity_types:
                selected_ids.add(endpoint_id)
    selected = [row for row in entity_rows if str(row.get("id")) in selected_ids]
    selected_relations = [
        row for row in relation_rows if str(row.get("id")) in matched_relation_ids
    ]
    return {
        "entity_types": _project_type_rows(selected, relation=False),
        "relation_types": _project_type_rows(selected_relations, relation=True),
        "relation_constraints": _project_relation_constraints(
            snapshot,
            entity_rows=selected,
            relation_rows=selected_relations,
        ),
    }


def build_batched_graph_extraction_messages(
    *,
    ontology_snapshot: dict[str, Any],
    batches: tuple[GraphExtractionBatchInput, ...],
    schema_routing_enabled: bool = False,
    max_entity_types: int = 12,
    context_window_tokens: int | None = None,
    max_output_tokens: int | None = None,
    raise_on_overflow: bool = True,
) -> list[dict[str, str]]:
    if not 1 <= len(batches) <= 8:
        raise ValueError("batch input count must be between 1 and 8")
    keys = [row.batch_key for row in batches]
    if len(keys) != len(set(keys)):
        raise ValueError("batch input keys must be unique")
    if max_entity_types < 1:
        raise ValueError("max_entity_types must be positive")

    context_text = "\n".join(row.context_text for row in batches)

    def build_for_limit(limit: int) -> list[dict[str, str]]:
        routed_ontology = route_ontology_for_extraction(
            ontology_snapshot,
            context_text=context_text,
            enabled=schema_routing_enabled,
            max_entity_types=limit,
        )
        payload = {
            "schema_subset_hash": canonical_graph_value_hash_v1(routed_ontology),
            "frozen_ontology": {
                **routed_ontology,
                "constraints": routed_ontology.get("relation_constraints", []),
            },
            "batches": [
                {"batch_key": row.batch_key, "untrusted_context": row.context_text}
                for row in batches
            ],
            "output_protocol": {
                "batches": "array of {batch_key,entities,relations}",
                "entity": "{local_id,name,entity_type_key,aliases,properties,confidence,evidence}",
                "relation": "{source_local_id,relation_type_key,target_local_id,properties,confidence,evidence}",
                "evidence": "array of {context_ref,quote}",
            },
        }
        return [
            {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract each batch using the shared compact frozen ontology.\n"
                    + json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            },
        ]

    limit = min(max_entity_types, len(ontology_snapshot.get("entity_types") or []))
    messages = build_for_limit(max(1, limit))
    if context_window_tokens is not None and max_output_tokens is not None:
        request_output_budget = _effective_output_budget(
            context_window_tokens=context_window_tokens,
            configured_output_tokens=max_output_tokens,
        )
        while (
            estimate_graph_extraction_request_tokens(
                messages,
                max_output_tokens=request_output_budget,
            )
            > context_window_tokens
            and limit > 1
        ):
            limit -= 1
            messages = build_for_limit(limit)
        if raise_on_overflow and estimate_graph_extraction_request_tokens(
            messages,
            max_output_tokens=request_output_budget,
        ) > context_window_tokens:
            raise ValueError("frozen ontology and protocol exceed the model context window")
    return messages


def _unwrap_outer_fence(raw_content: str) -> str:
    value = (raw_content or "").strip()
    match = _OUTER_JSON_FENCE.fullmatch(value)
    return match.group("body").strip() if match is not None else value


def _coerce_batched_response_rows(decoded: Any, expected_keys: tuple[str, ...]) -> list[Any]:
    if not isinstance(decoded, dict):
        raise BatchExtractionParseError("batch response must be an object")
    if set(decoded) == {"batches"}:
        rows = decoded["batches"]
        if not isinstance(rows, list):
            raise BatchExtractionParseError("batch response batches must be a list")
        return rows
    if set(decoded) != set(expected_keys):
        raise BatchExtractionParseError("batch response must contain only batches")
    rows = []
    for key in expected_keys:
        row = decoded.get(key)
        if not isinstance(row, dict):
            raise BatchExtractionParseError("batch response payload schema is invalid")
        if "batch_key" in row and row["batch_key"] != key:
            raise BatchExtractionParseError("batch response must contain exactly the requested keys")
        rows.append({"batch_key": key, **row})
    return rows


def _without_undeclared_endpoint_relations(payload: dict[str, Any]) -> dict[str, Any]:
    entities = payload.get("entities")
    relations = payload.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        return payload
    local_ids = {
        entity.get("local_id")
        for entity in entities
        if isinstance(entity, dict) and isinstance(entity.get("local_id"), str)
    }
    return {
        **payload,
        "relations": [
            relation
            for relation in relations
            if not isinstance(relation, dict)
            or (
                relation.get("source_local_id") in local_ids
                and relation.get("target_local_id") in local_ids
            )
        ],
    }


def _coerce_numeric_evidence_context_refs(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the provider's numeric JSON spelling for an otherwise string reference."""

    normalized = dict(payload)
    for collection_key in ("entities", "relations"):
        rows = payload.get(collection_key)
        if not isinstance(rows, list):
            continue
        normalized_rows = list(rows)
        changed = False
        for index, row in enumerate(normalized_rows):
            if not isinstance(row, dict) or not isinstance(row.get("evidence"), list):
                continue
            evidence_rows = list(row["evidence"])
            row_changed = False
            for evidence_index, evidence in enumerate(evidence_rows):
                context_ref = evidence.get("context_ref") if isinstance(evidence, dict) else None
                if isinstance(context_ref, int) and not isinstance(context_ref, bool) and context_ref >= 0:
                    evidence_rows[evidence_index] = {
                        **evidence,
                        "context_ref": str(context_ref),
                    }
                    row_changed = True
            if row_changed:
                normalized_rows[index] = {**row, "evidence": evidence_rows}
                changed = True
        if changed:
            normalized[collection_key] = normalized_rows
    return normalized


def parse_batched_graph_extraction_output(
    raw_content: str,
    *,
    expected_keys: tuple[str, ...],
    allowed_entity_type_keys: set[str] | None = None,
    allowed_relation_type_keys: set[str] | None = None,
) -> dict[str, GraphExtractionPayload]:
    try:
        decoded = json.loads(_unwrap_outer_fence(raw_content))
    except (TypeError, ValueError) as exc:
        raise BatchExtractionParseError("batch response is not valid JSON") from exc
    rows = _coerce_batched_response_rows(decoded, expected_keys)
    keys = [row.get("batch_key") if isinstance(row, dict) else None for row in rows]
    if len(keys) != len(set(keys)):
        raise BatchExtractionParseError("batch response keys must be unique")
    if set(keys) != set(expected_keys) or len(keys) != len(expected_keys):
        raise BatchExtractionParseError("batch response must contain exactly the requested keys")
    by_key: dict[str, GraphExtractionPayload] = {}
    try:
        for row in rows:
            assert isinstance(row, dict)
            batch_key = row.get("batch_key")
            payload = {key: value for key, value in row.items() if key != "batch_key"}
            payload = _without_undeclared_endpoint_relations(payload)
            payload = _coerce_numeric_evidence_context_refs(payload)
            parsed = GraphExtractionPayload.model_validate(payload)
            if allowed_entity_type_keys is not None and any(
                item.entity_type_key not in allowed_entity_type_keys for item in parsed.entities
            ):
                raise ValueError("entity type is outside the routed Schema subset")
            if allowed_relation_type_keys is not None and any(
                item.relation_type_key not in allowed_relation_type_keys for item in parsed.relations
            ):
                raise ValueError("relation type is outside the routed Schema subset")
            by_key[str(batch_key)] = parsed
    except (AssertionError, ValidationError, ValueError) as exc:
        raise BatchExtractionParseError("batch response payload schema is invalid") from exc
    return by_key


def _positive_int(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


async def claim_eval_graph_extraction_batch(
    db,
    *,
    worker_id: str,
    batch_size: int,
    lease_seconds: int,
    production_only: bool = False,
    max_attempts: int,
) -> tuple[GraphExtractionUnit, ...]:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 255:
        raise ValueError("worker_id must contain 1 to 255 characters")
    if not 1 <= batch_size <= 8:
        raise ValueError("batch_size must be between 1 and 8")
    _positive_int(lease_seconds, label="lease_seconds")
    _positive_int(max_attempts, label="max_attempts")
    claimed_at = datetime.now(timezone.utc)
    async with db.begin():
        claimable = exists(
            select(GraphExtractionUnit.id).where(
                GraphExtractionUnit.job_id == GraphExtractionJob.id,
                GraphExtractionUnit.status == "queued",
                GraphExtractionUnit.model_attempt_count < max_attempts,
            )
        )
        batch_in_progress = exists(
            select(GraphExtractionUnit.id).where(
                GraphExtractionUnit.job_id == GraphExtractionJob.id,
                GraphExtractionUnit.status == "processing",
            )
        )
        job_result = await db.execute(
            select(GraphExtractionJob)
            .join(Library, Library.id == GraphExtractionJob.library_id)
            .where(
                GraphExtractionJob.status.in_(("queued", "processing")),
                (
                    GraphExtractionJob.execution_mode == "production"
                    if production_only
                    else GraphExtractionJob.execution_mode.in_(("production", "eval", "repair"))
                ),
                (GraphExtractionJob.model_config_snapshot["batch_size"].as_integer() >= 1),
                ~shadow_enabled_library_sql_predicate(),
                claimable,
                ~batch_in_progress,
            )
            .order_by(GraphExtractionJob.created_at, GraphExtractionJob.id)
            .with_for_update(skip_locked=True, of=GraphExtractionJob)
            .limit(1)
        )
        job = job_result.scalars().first()
        if job is None:
            return ()
        unit_result = await db.execute(
            select(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.job_id == job.id,
                GraphExtractionUnit.status == "queued",
                GraphExtractionUnit.model_attempt_count < max_attempts,
            )
            .order_by(GraphExtractionUnit.ordinal, GraphExtractionUnit.id)
            .with_for_update(skip_locked=True)
            .limit(batch_size)
        )
        candidate_units = tuple(unit_result.scalars().all())
        chunk_result = await db.execute(
            select(Chunk).where(
                Chunk.id.in_(unit.center_chunk_id for unit in candidate_units),
                Chunk.document_revision_id == job.document_revision_id,
            )
        )
        chunks_by_id = {row.id: row for row in chunk_result.scalars().all()}
        chunk_ids = tuple(chunks_by_id)
        link_result = await db.execute(
            select(ChunkBlock)
            .where(
                ChunkBlock.chunk_id.in_(chunk_ids),
                ChunkBlock.document_revision_id == job.document_revision_id,
            )
            .order_by(
                ChunkBlock.chunk_id.asc(),
                ChunkBlock.seq.asc(),
                ChunkBlock.document_block_id.asc(),
            )
        )
        links = tuple(link_result.scalars().all())
        block_ids = {row.document_block_id for row in links}
        block_ids.update(row.block_id for row in chunks_by_id.values() if row.block_id is not None)
        block_result = await db.execute(
            select(DocumentBlock).where(
                DocumentBlock.id.in_(block_ids),
                DocumentBlock.library_id == job.library_id,
                DocumentBlock.document_id == job.document_id,
                DocumentBlock.document_revision_id == job.document_revision_id,
            )
        )
        blocks_by_id = {row.id: row for row in block_result.scalars().all()}
        primary_block_by_chunk: dict[uuid.UUID, DocumentBlock] = {}
        for link in links:
            block = blocks_by_id.get(link.document_block_id)
            if block is not None:
                primary_block_by_chunk.setdefault(link.chunk_id, block)
        boundaries: list[BatchChunkBoundary] = []
        for unit in candidate_units:
            chunk = chunks_by_id.get(unit.center_chunk_id)
            if chunk is None:
                break
            path, _source, block = _title_info(
                chunk,
                blocks_by_id=blocks_by_id,
                primary_block_by_chunk=primary_block_by_chunk,
            )
            boundaries.append(
                BatchChunkBoundary(
                    unit_id=unit.id,
                    seq=chunk.seq,
                    title_path=tuple(path),
                    structured_kind=_structured_kind(
                        chunk.chunk_kind,
                        getattr(block, "block_kind", None),
                    ),
                    block_id=getattr(block, "id", None),
                )
            )
        selected_ids = set(_safe_batch_prefix(tuple(boundaries)))
        units = tuple(unit for unit in candidate_units if unit.id in selected_ids)
        for unit in units:
            unit.status = "processing"
            unit.worker_id = worker_id.strip()
            unit.claim_token = uuid.uuid4()
            unit.claimed_at = claimed_at
            unit.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            unit.retryable = False
            unit.error_code = None
            unit.error_message = None
            unit.started_at = unit.started_at or claimed_at
        if units:
            job.status = "processing"
            job.current_stage = "building_context"
            job.started_at = job.started_at or claimed_at
            job.error_code = None
            job.error_message = None
            await db.flush()
        return units


def _prepared_input(
    prepared: PreparedGraphExtractionUnit,
) -> tuple[str, dict[str, Any]]:
    if len(prepared.messages) != 2:
        raise ValueError("prepared Unit messages are malformed")
    content = prepared.messages[1].get("content", "")
    try:
        payload = json.loads(content.split("\n", 1)[1])
        context_text = payload["untrusted_context"]
        ontology = payload["frozen_ontology"]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("prepared Unit input payload is malformed") from exc
    if not isinstance(context_text, str) or not isinstance(ontology, dict):
        raise ValueError("prepared Unit input payload has invalid types")
    if prepared.center_only:
        try:
            context = json.loads(context_text)
            chunks = context["chunks"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("prepared center-only Context is malformed") from exc
        if not isinstance(context, dict) or not isinstance(chunks, list):
            raise ValueError("prepared center-only Context is malformed")
        center_chunks = [
            row
            for row in chunks
            if isinstance(row, dict) and row.get("context_ref") == "c0"
        ]
        if len(center_chunks) != 1:
            raise ValueError("prepared center-only Context requires exactly one c0 Chunk")
        context["chunks"] = center_chunks
        context_text = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return context_text, ontology


async def _renew_batch_leases_now(
    session_factory,
    *,
    prepared_rows: tuple[PreparedGraphExtractionUnit, ...],
    lease_seconds: int,
) -> bool:
    for prepared in prepared_rows:
        async with session_factory() as db:
            renewed = await renew_graph_extraction_unit_lease(
                db,
                unit_id=prepared.unit_id,
                claim_token=prepared.claim_token,
                lease_seconds=lease_seconds,
            )
        if not renewed:
            return False
    return True


async def _renew_batch_leases(
    session_factory,
    *,
    prepared_rows: tuple[PreparedGraphExtractionUnit, ...],
    lease_seconds: int,
    renew_seconds: int,
) -> bool:
    await asyncio.sleep(renew_seconds)
    return await _renew_batch_leases_now(
        session_factory,
        prepared_rows=prepared_rows,
        lease_seconds=lease_seconds,
    )


async def _call_provider_with_batch_renewal(
    session_factory,
    *,
    provider,
    messages: list[dict[str, str]],
    prepared_rows: tuple[PreparedGraphExtractionUnit, ...],
    lease_seconds: int,
    renew_seconds: int,
):
    if not await _renew_batch_leases_now(
        session_factory,
        prepared_rows=prepared_rows,
        lease_seconds=lease_seconds,
    ):
        return None, True

    lease_lost = False

    async def renew_loop() -> None:
        nonlocal lease_lost
        while True:
            if not await _renew_batch_leases(
                session_factory,
                prepared_rows=prepared_rows,
                lease_seconds=lease_seconds,
                renew_seconds=renew_seconds,
            ):
                lease_lost = True
                return

    renew_task = asyncio.create_task(renew_loop())
    try:
        response = await call_graph_extraction_provider(
            lambda: provider.extract(messages),
            concurrency=settings.graph_extraction_provider_max_concurrency,
            max_retries=settings.graph_extraction_provider_max_retries,
            backoff_base_seconds=(settings.graph_extraction_provider_backoff_base_seconds),
        )
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
    return response, lease_lost


@dataclass(slots=True)
class _PendingBatchLeaseGuard:
    pending: dict[uuid.UUID, PreparedGraphExtractionUnit]
    lost: set[uuid.UUID]

    def release(self, unit_id: uuid.UUID) -> None:
        self.pending.pop(unit_id, None)

    def lease_lost(self, unit_id: uuid.UUID) -> bool:
        return unit_id in self.lost


async def _renew_pending_batch_leases(
    session_factory,
    *,
    guard: _PendingBatchLeaseGuard,
    lease_seconds: int,
) -> None:
    for prepared in tuple(guard.pending.values()):
        if prepared.unit_id not in guard.pending or prepared.unit_id in guard.lost:
            continue
        async with session_factory() as db:
            renewed = await renew_graph_extraction_unit_lease(
                db,
                unit_id=prepared.unit_id,
                claim_token=prepared.claim_token,
                lease_seconds=lease_seconds,
            )
        if not renewed:
            guard.lost.add(prepared.unit_id)


@asynccontextmanager
async def _keep_batch_leases_live_during_persistence(
    session_factory,
    *,
    prepared_rows: tuple[PreparedGraphExtractionUnit, ...],
    lease_seconds: int,
    renew_seconds: int,
) -> AsyncIterator[_PendingBatchLeaseGuard]:
    guard = _PendingBatchLeaseGuard(
        pending={row.unit_id: row for row in prepared_rows},
        lost=set(),
    )
    await _renew_pending_batch_leases(
        session_factory,
        guard=guard,
        lease_seconds=lease_seconds,
    )

    async def renew_loop() -> None:
        while guard.pending:
            await asyncio.sleep(renew_seconds)
            await _renew_pending_batch_leases(
                session_factory,
                guard=guard,
                lease_seconds=lease_seconds,
            )

    renew_task = asyncio.create_task(renew_loop())
    try:
        yield guard
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass


async def _finish_batch(
    session_factory,
    *,
    claimed_rows: tuple[Any, ...],
    max_attempts: int,
    error_code: str,
    status: str = "failed",
) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    for claimed in claimed_rows:
        claim_token = claimed.claim_token
        if claim_token is None:
            outcomes["lost_lease"] += 1
            continue
        unit_id = getattr(claimed, "unit_id", None)
        if unit_id is None:
            unit_id = claimed.id
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status=status,
            error_code=error_code,
            abandon_pending=True,
        )
        outcomes[status if finished else "lost_lease"] += 1
    return outcomes


async def _consume_cached_batch_rows(
    session_factory,
    *,
    units: tuple[GraphExtractionUnit, ...],
    prepared: tuple[PreparedGraphExtractionUnit, ...],
    lease_seconds: int,
    renew_seconds: int,
    max_attempts: int,
) -> tuple[Counter[str], set[uuid.UUID], tuple[GraphExtractionUnit, ...]]:
    outcomes: Counter[str] = Counter()
    ready_job_ids: set[uuid.UUID] = set()
    remaining_ids: set[uuid.UUID] = set()
    async with _keep_batch_leases_live_during_persistence(
        session_factory,
        prepared_rows=prepared,
        lease_seconds=lease_seconds,
        renew_seconds=renew_seconds,
    ) as lease_guard:
        for row in prepared:
            if lease_guard.lease_lost(row.unit_id):
                outcomes["lost_lease"] += 1
                lease_guard.release(row.unit_id)
                continue
            try:
                payload = await _load_prepared_replay(session_factory, row)
                cache_hit = False
                if payload is None:
                    payload = await _load_prepared_cache(session_factory, row)
                    cache_hit = payload is not None
                if payload is None:
                    remaining_ids.add(row.unit_id)
                    continue
                cached = replace(row, cache_hit=cache_hit)
                # The persistence transaction locks this Unit row. Remove it from
                # the renewal queue first so it cannot block leases for later rows.
                lease_guard.release(row.unit_id)
                ready = await _persist_candidate_result(
                    session_factory,
                    prepared=cached,
                    payload=payload,
                )
            except GraphExtractionWorkerError as exc:
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="cancelled",
                    error_code=exc.code,
                )
                outcomes["cancelled" if finished else "lost_lease"] += 1
            except CandidateAggregationError as exc:
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code=exc.code,
                    error_message=type(exc).__name__,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            except (DBAPIError, OSError):
                raise
            except Exception as exc:  # noqa: BLE001
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code="candidate_processing_failed",
                    error_message=type(exc).__name__,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            else:
                if payload is not None:
                    outcomes["succeeded" if ready is not None else "lost_lease"] += 1
                    if ready:
                        ready_job_ids.add(row.job_id)
            finally:
                lease_guard.release(row.unit_id)
    remaining = tuple(unit for unit in units if unit.id in remaining_ids)
    return outcomes, ready_job_ids, remaining


def _merge_fragment_payloads(
    fragments: dict[str, GraphExtractionPayload],
    *,
    source_key: str,
) -> GraphExtractionPayload:
    """Join split responses while keeping local IDs unique for one source unit."""

    entities = []
    relations = []
    for fragment_key, payload in fragments.items():
        prefix = f"{source_key}:{fragment_key}:"
        local_ids = {item.local_id: f"{prefix}{item.local_id}" for item in payload.entities}
        entities.extend(
            item.model_copy(update={"local_id": local_ids[item.local_id]})
            for item in payload.entities
        )
        relations.extend(
            item.model_copy(
                update={
                    "source_local_id": local_ids[item.source_local_id],
                    "target_local_id": local_ids[item.target_local_id],
                }
            )
            for item in payload.relations
        )
    return GraphExtractionPayload(entities=entities, relations=relations)


async def _process_split_graph_extraction_batches(
    session_factory,
    *,
    units: tuple[GraphExtractionUnit, ...],
    prepared: tuple[PreparedGraphExtractionUnit, ...],
    planned_inputs: tuple[tuple[GraphExtractionBatchInput, ...], ...],
    ontology: dict[str, Any],
    lease_seconds: int,
    renew_seconds: int,
    max_attempts: int,
) -> EvalBatchProcessResult:
    """Execute planned text fragments and persist one merged payload per unit."""

    prepared_by_source = {f"u{index}": row for index, row in enumerate(prepared)}
    fragments_by_source: dict[str, dict[str, GraphExtractionPayload]] = {
        key: {} for key in prepared_by_source
    }
    provider_call_count = 0
    for group_ordinal, group in enumerate(planned_inputs):
        messages = build_batched_graph_extraction_messages(
            ontology_snapshot=ontology,
            batches=group,
            schema_routing_enabled=bool(
                prepared[0].model_config_snapshot.get("schema_routing_enabled", False)
            ),
            max_entity_types=int(prepared[0].model_config_snapshot.get("max_entity_types", 12)),
            context_window_tokens=int(prepared[0].model_config_snapshot.get("context_window_tokens", 4096)),
            max_output_tokens=int(prepared[0].model_config_snapshot.get("max_output_tokens", 700)),
        )
        request_payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        routed_ontology = request_payload["frozen_ontology"]
        allowed_entity_type_keys = {row["key"] for row in routed_ontology["entity_types"]}
        allowed_relation_type_keys = {row["key"] for row in routed_ontology["relation_types"]}
        group_rows = tuple(prepared_by_source[item.source_key] for item in group)
        attempts = []
        batch_request_id = uuid.uuid4()
        for ordinal, item in enumerate(group):
            row = prepared_by_source[item.source_key]
            async with session_factory() as db:
                attempt = await create_pending_attempt(
                    db,
                    unit_id=row.unit_id,
                    context_snapshot_id=row.context_snapshot_id,
                    claim_token=row.claim_token,
                    request_payload_hash=canonical_graph_value_hash_v1(
                        {"messages": messages, "model_config_hash": row.model_config_hash}
                    ),
                    max_attempts=max_attempts,
                    batch_request_id=batch_request_id,
                    batch_key=item.batch_key,
                    batch_ordinal=ordinal,
                )
            attempts.append(attempt)
        provider = _configured_provider(prepared[0])
        request_output_budget = _effective_output_budget(
            context_window_tokens=int(prepared[0].model_config_snapshot.get("context_window_tokens", 4096)),
            configured_output_tokens=int(prepared[0].model_config_snapshot.get("max_output_tokens", 700)),
        )
        if hasattr(provider, "with_output_budget"):
            provider = provider.with_output_budget(request_output_budget)
        try:
            response, lease_lost = await _call_provider_with_batch_renewal(
                session_factory,
                provider=provider,
                messages=messages,
                prepared_rows=group_rows,
                lease_seconds=lease_seconds,
                renew_seconds=renew_seconds,
            )
        except GraphExtractionProviderError as exc:
            provider_call_count += 1
            for row, attempt in zip(group_rows, attempts, strict=True):
                async with session_factory() as db:
                    await finalize_attempt(
                        db,
                        attempt_id=attempt.id,
                        claim_token=row.claim_token,
                        completion=AttemptCompletion(
                            request_status=exc.category,
                            latency_ms=exc.latency_ms,
                        ),
                    )
            outcomes = await _finish_batch(
                session_factory,
                claimed_rows=prepared,
                max_attempts=max_attempts,
                error_code=f"provider_{exc.category}",
            )
            return EvalBatchProcessResult(outcomes, provider_call_count)
        if lease_lost:
            return EvalBatchProcessResult(Counter({"lost_lease": len(prepared)}), provider_call_count)
        provider_call_count += 1
        assert response is not None
        parse_error: str | None = None
        if response.finish_reason in {"length", "max_tokens"}:
            parse_error = "provider output was truncated"
            payloads = None
            parse_status = "invalid_json"
        else:
            try:
                payloads = parse_batched_graph_extraction_output(
                    response.content,
                    expected_keys=tuple(item.batch_key for item in group),
                    allowed_entity_type_keys=allowed_entity_type_keys,
                    allowed_relation_type_keys=allowed_relation_type_keys,
                )
                parse_status = "valid"
            except BatchExtractionParseError as exc:
                payloads = None
                parse_status = "invalid_schema"
                parse_error = str(exc)
        for ordinal, (item, row, attempt) in enumerate(zip(group, group_rows, attempts, strict=True)):
            unit_response = (
                {"batch_key": item.batch_key, **payloads[item.batch_key].model_dump(mode="json")}
                if payloads is not None
                else None
            )
            async with session_factory() as db:
                await finalize_attempt(
                    db,
                    attempt_id=attempt.id,
                    claim_token=row.claim_token,
                    completion=AttemptCompletion(
                        request_status="succeeded",
                        parse_status=parse_status,
                        provider_request_id=response.provider_request_id,
                        raw_response=response.raw_response if ordinal == 0 else None,
                        parsed_response=unit_response,
                        parse_error=parse_error,
                        input_token_count=response.input_token_count if ordinal == 0 else None,
                        output_token_count=response.output_token_count if ordinal == 0 else None,
                        latency_ms=response.latency_ms,
                        finish_reason=response.finish_reason,
                    ),
                )
            if payloads is not None:
                fragments_by_source[item.source_key][item.batch_key] = payloads[item.batch_key]
        if payloads is None:
            outcomes = await _finish_batch(
                session_factory,
                claimed_rows=prepared,
                max_attempts=max_attempts,
                error_code="provider_invalid_output",
            )
            return EvalBatchProcessResult(outcomes, provider_call_count)

    outcomes: Counter[str] = Counter()
    ready_job_ids: set[uuid.UUID] = set()
    rows_to_persist = tuple(prepared_by_source.values())
    async with _keep_batch_leases_live_during_persistence(
        session_factory,
        prepared_rows=rows_to_persist,
        lease_seconds=lease_seconds,
        renew_seconds=renew_seconds,
    ) as lease_guard:
        for source_key, row in prepared_by_source.items():
            if lease_guard.lease_lost(row.unit_id):
                outcomes["lost_lease"] += 1
                lease_guard.release(row.unit_id)
                continue
            try:
                payload = _merge_fragment_payloads(
                    fragments_by_source[source_key], source_key=source_key
                )
                lease_guard.release(row.unit_id)
                persisted = await _persist_candidate_result(
                    session_factory,
                    prepared=row,
                    payload=payload,
                )
            except GraphExtractionWorkerError as exc:
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code=exc.code,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            except CandidateAggregationError as exc:
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code=exc.code,
                    error_message=type(exc).__name__,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            except (DBAPIError, OSError):
                raise
            except Exception as exc:  # noqa: BLE001
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code="candidate_processing_failed",
                    error_message=type(exc).__name__,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            else:
                outcomes["succeeded" if persisted is not None else "lost_lease"] += 1
                if persisted is True:
                    ready_job_ids.add(row.job_id)
            finally:
                lease_guard.release(row.unit_id)
    return EvalBatchProcessResult(
        outcomes,
        provider_call_count,
        tuple(sorted(ready_job_ids, key=str)),
    )


async def process_eval_graph_extraction_batch(
    session_factory,
    *,
    units: tuple[GraphExtractionUnit, ...],
    lease_seconds: int | None = None,
    renew_seconds: int | None = None,
    max_attempts: int | None = None,
) -> EvalBatchProcessResult:
    if not units:
        return EvalBatchProcessResult(Counter(), 0)
    lease_seconds = lease_seconds or settings.graph_extraction_unit_lease_seconds
    renew_seconds = renew_seconds or settings.graph_extraction_unit_lease_renew_seconds
    max_attempts = max_attempts or settings.graph_extraction_worker_max_model_attempts
    if len(units) == 1:
        unit = units[0]
        if unit.claim_token is None:
            return EvalBatchProcessResult(Counter({"lost_lease": 1}), 0)
        result = await process_graph_extraction_unit(
            session_factory,
            unit_id=unit.id,
            claim_token=unit.claim_token,
            lease_seconds=lease_seconds,
            renew_seconds=renew_seconds,
            max_attempts=max_attempts,
        )
        ready_job_ids = (unit.job_id,) if result.ready_for_materialization else ()
        return EvalBatchProcessResult(Counter({result.outcome: 1}), 1, ready_job_ids)
    prepared_rows: list[PreparedGraphExtractionUnit] = []
    try:
        for unit in units:
            if unit.claim_token is None:
                raise LostGraphExtractionLease()
            prepared_rows.append(
                await _prepare_graph_extraction_unit(
                    session_factory,
                    unit_id=unit.id,
                    claim_token=unit.claim_token,
                )
            )
        prepared = tuple(prepared_rows)
        cached_outcomes, cached_ready_job_ids, remaining = await _consume_cached_batch_rows(
            session_factory,
            units=units,
            prepared=prepared,
            lease_seconds=lease_seconds,
            renew_seconds=renew_seconds,
            max_attempts=max_attempts,
        )
        if len(remaining) != len(units):
            if remaining:
                child = await process_eval_graph_extraction_batch(
                    session_factory,
                    units=remaining,
                    lease_seconds=lease_seconds,
                    renew_seconds=renew_seconds,
                    max_attempts=max_attempts,
                )
                cached_outcomes.update(child.outcomes)
                cached_ready_job_ids.update(child.ready_job_ids)
                provider_call_count = child.provider_call_count
            else:
                provider_call_count = 0
            return EvalBatchProcessResult(
                cached_outcomes,
                provider_call_count,
                tuple(sorted(cached_ready_job_ids, key=str)),
            )
        if len(units) > 2 and prepared[0].model_config_snapshot.get("schema_routing_enabled") is False:
            midpoint = len(units) // 2
            child_results = await asyncio.gather(
                *(
                    process_eval_graph_extraction_batch(
                        session_factory,
                        units=child_units,
                        lease_seconds=lease_seconds,
                        renew_seconds=renew_seconds,
                        max_attempts=max_attempts,
                    )
                    for child_units in (units[:midpoint], units[midpoint:])
                )
            )
            outcomes: Counter[str] = Counter()
            ready_job_ids: set[uuid.UUID] = set()
            provider_call_count = 0
            for child in child_results:
                outcomes.update(child.outcomes)
                ready_job_ids.update(child.ready_job_ids)
                provider_call_count += child.provider_call_count
            return EvalBatchProcessResult(
                outcomes,
                provider_call_count,
                tuple(sorted(ready_job_ids, key=str)),
            )
        inputs: list[GraphExtractionBatchInput] = []
        ontology: dict[str, Any] | None = None
        for index, row in enumerate(prepared):
            context_text, row_ontology = _prepared_input(row)
            ontology = ontology or row_ontology
            if canonical_graph_value_hash_v1(row_ontology) != canonical_graph_value_hash_v1(ontology):
                raise ValueError("batched Units must share one frozen ontology")
            inputs.append(GraphExtractionBatchInput(f"u{index}", context_text))
        assert ontology is not None
        planned_inputs = plan_graph_extraction_batches(
            ontology_snapshot=ontology,
            inputs=tuple(inputs),
            schema_routing_enabled=bool(
            prepared[0].model_config_snapshot.get("schema_routing_enabled", False)
            ),
            context_window_tokens=int(prepared[0].model_config_snapshot["context_window_tokens"]),
            max_output_tokens=int(prepared[0].model_config_snapshot.get("max_output_tokens", 700)),
            max_entity_types=int(
                prepared[0].model_config_snapshot.get("max_entity_types", 12)
            ),
        ) if "context_window_tokens" in prepared[0].model_config_snapshot else (tuple(inputs),)
        if any(
            item.source_batch_key is not None
            for group in planned_inputs
            for item in group
        ):
            return await _process_split_graph_extraction_batches(
                session_factory,
                units=units,
                prepared=prepared,
                planned_inputs=planned_inputs,
                ontology=ontology,
                lease_seconds=lease_seconds,
                renew_seconds=renew_seconds,
                max_attempts=max_attempts,
            )
        if len(planned_inputs) > 1:
            unit_by_key = {f"u{index}": unit for index, unit in enumerate(units)}
            child_results = await asyncio.gather(
                *(
                    process_eval_graph_extraction_batch(
                        session_factory,
                        units=tuple(unit_by_key[item.batch_key] for item in group),
                        lease_seconds=lease_seconds,
                        renew_seconds=renew_seconds,
                        max_attempts=max_attempts,
                    )
                    for group in planned_inputs
                )
            )
            outcomes: Counter[str] = Counter()
            ready_job_ids: set[uuid.UUID] = set()
            provider_call_count = 0
            for child in child_results:
                outcomes.update(child.outcomes)
                ready_job_ids.update(child.ready_job_ids)
                provider_call_count += child.provider_call_count
            return EvalBatchProcessResult(
                outcomes,
                provider_call_count,
                tuple(sorted(ready_job_ids, key=str)),
            )
        messages = build_batched_graph_extraction_messages(
            ontology_snapshot=ontology,
            batches=tuple(inputs),
            schema_routing_enabled=bool(
                prepared[0].model_config_snapshot.get("schema_routing_enabled", False)
            ),
            max_entity_types=int(prepared[0].model_config_snapshot.get("max_entity_types", 12)),
            context_window_tokens=int(prepared[0].model_config_snapshot.get("context_window_tokens", 4096)),
            max_output_tokens=int(prepared[0].model_config_snapshot.get("max_output_tokens", 700)),
        )
        request_payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        routed_ontology = request_payload["frozen_ontology"]
        allowed_entity_type_keys = {row["key"] for row in routed_ontology["entity_types"]}
        allowed_relation_type_keys = {row["key"] for row in routed_ontology["relation_types"]}
    except (
        ContextBuildError,
        GraphExtractionWorkerError,
        LostGraphExtractionLease,
        ValueError,
    ):
        child_results = await asyncio.gather(
            *(
                process_eval_graph_extraction_batch(
                    session_factory,
                    units=(unit,),
                    lease_seconds=lease_seconds,
                    renew_seconds=renew_seconds,
                    max_attempts=max_attempts,
                )
                for unit in units
            )
        )
        outcomes: Counter[str] = Counter()
        ready_job_ids: set[uuid.UUID] = set()
        provider_call_count = 0
        for child in child_results:
            outcomes.update(child.outcomes)
            ready_job_ids.update(child.ready_job_ids)
            provider_call_count += child.provider_call_count
        return EvalBatchProcessResult(
            outcomes,
            provider_call_count,
            tuple(sorted(ready_job_ids, key=str)),
        )

    leader = prepared[0]
    request_hash = canonical_graph_value_hash_v1(
        {"messages": messages, "model_config_hash": leader.model_config_hash}
    )
    batch_request_id = uuid.uuid4()
    attempts = []
    try:
        for index, row in enumerate(prepared):
            async with session_factory() as db:
                attempt = await create_pending_attempt(
                    db,
                    unit_id=row.unit_id,
                    context_snapshot_id=row.context_snapshot_id,
                    claim_token=row.claim_token,
                    request_payload_hash=(
                        row.cache_key if row.cache_enabled and row.cache_key is not None else request_hash
                    ),
                    max_attempts=max_attempts,
                    batch_request_id=batch_request_id,
                    batch_key=f"u{index}",
                    batch_ordinal=index,
                )
            attempts.append(attempt)
        for row in prepared:
            await _preflight_provider_call(
                session_factory,
                unit_id=row.unit_id,
                claim_token=row.claim_token,
            )
        provider = _configured_provider(leader)
        request_output_budget = _effective_output_budget(
            context_window_tokens=int(
                leader.model_config_snapshot.get("context_window_tokens", 4096)
            ),
            configured_output_tokens=int(
                leader.model_config_snapshot.get("max_output_tokens", 700)
            ),
        )
        if hasattr(provider, "with_output_budget"):
            provider = provider.with_output_budget(request_output_budget)
        response, lease_lost = await _call_provider_with_batch_renewal(
            session_factory,
            provider=provider,
            messages=messages,
            prepared_rows=prepared,
            lease_seconds=lease_seconds,
            renew_seconds=renew_seconds,
        )
    except AttemptStateError:
        outcomes = await _finish_batch(
            session_factory,
            claimed_rows=prepared,
            max_attempts=max_attempts,
            error_code="batch_attempt_preparation_failed",
        )
        return EvalBatchProcessResult(outcomes, 0)
    except (GraphExtractionWorkerError, LostGraphExtractionLease) as exc:
        outcomes = await _finish_batch(
            session_factory,
            claimed_rows=prepared,
            max_attempts=max_attempts,
            error_code=getattr(exc, "code", "batch_preflight_failed"),
            status="cancelled",
        )
        return EvalBatchProcessResult(outcomes, 0)
    except GraphExtractionProviderError as exc:
        for row, attempt in zip(prepared, attempts, strict=True):
            async with session_factory() as db:
                await finalize_attempt(
                    db,
                    attempt_id=attempt.id,
                    claim_token=row.claim_token,
                    completion=AttemptCompletion(
                        request_status=exc.category,
                        latency_ms=exc.latency_ms,
                    ),
                )
        outcomes = await _finish_batch(
            session_factory,
            claimed_rows=prepared,
            max_attempts=max_attempts,
            error_code=f"provider_{exc.category}",
        )
        return EvalBatchProcessResult(outcomes, 1)
    if lease_lost:
        return EvalBatchProcessResult(Counter({"lost_lease": len(prepared)}), 0)
    assert response is not None

    payloads: dict[str, GraphExtractionPayload] | None = None
    parse_error: str | None = None
    if response.finish_reason in {"length", "max_tokens"}:
        parse_status = "invalid_json"
        parse_error = "provider output was truncated"
    else:
        try:
            payloads = parse_batched_graph_extraction_output(
                response.content,
                expected_keys=tuple(row.batch_key for row in inputs),
                allowed_entity_type_keys=allowed_entity_type_keys,
                allowed_relation_type_keys=allowed_relation_type_keys,
            )
            parse_status = "valid"
        except BatchExtractionParseError as exc:
            parse_status = "invalid_schema"
            parse_error = str(exc)

    finalized_all = True
    for index, (row, attempt) in enumerate(zip(prepared, attempts, strict=True)):
        unit_response = (
            {
                "batch_key": f"u{index}",
                **payloads[f"u{index}"].model_dump(mode="json"),
            }
            if payloads is not None
            else None
        )
        async with session_factory() as db:
            finalized = await finalize_attempt(
                db,
                attempt_id=attempt.id,
                claim_token=row.claim_token,
                completion=AttemptCompletion(
                    request_status="succeeded",
                    parse_status=parse_status,
                    provider_request_id=response.provider_request_id,
                    raw_response=response.raw_response if index == 0 else None,
                    parsed_response=unit_response,
                    parse_error=parse_error,
                    input_token_count=(response.input_token_count if index == 0 else None),
                    output_token_count=(response.output_token_count if index == 0 else None),
                    latency_ms=response.latency_ms,
                    finish_reason=response.finish_reason,
                ),
            )
        finalized_all = finalized_all and finalized
    if not finalized_all:
        return EvalBatchProcessResult(Counter({"lost_lease": len(prepared)}), 1)
    ready_job_ids: set[uuid.UUID] = set()
    if payloads is None:
        midpoint = len(units) // 2
        child_results = await asyncio.gather(
            *(
                process_eval_graph_extraction_batch(
                    session_factory,
                    units=child_units,
                    lease_seconds=lease_seconds,
                    renew_seconds=renew_seconds,
                    max_attempts=max_attempts,
                )
                for child_units in (units[:midpoint], units[midpoint:])
            )
        )
        outcomes: Counter[str] = Counter()
        provider_call_count = 1
        for child in child_results:
            outcomes.update(child.outcomes)
            ready_job_ids.update(child.ready_job_ids)
            provider_call_count += child.provider_call_count
        return EvalBatchProcessResult(
            outcomes,
            provider_call_count,
            tuple(sorted(ready_job_ids, key=str)),
        )

    outcomes: Counter[str] = Counter()
    async with _keep_batch_leases_live_during_persistence(
        session_factory,
        prepared_rows=prepared,
        lease_seconds=lease_seconds,
        renew_seconds=renew_seconds,
    ) as lease_guard:
        for index, row in enumerate(prepared):
            if lease_guard.lease_lost(row.unit_id):
                outcomes["lost_lease"] += 1
                lease_guard.release(row.unit_id)
                continue
            try:
                lease_guard.release(row.unit_id)
                persisted = await _persist_candidate_result(
                    session_factory,
                    prepared=row,
                    payload=payloads[f"u{index}"],
                )
            except GraphExtractionWorkerError as exc:
                finished = await _finish_claim_after_error(
                    session_factory,
                    unit_id=row.unit_id,
                    claim_token=row.claim_token,
                    max_attempts=max_attempts,
                    status="failed",
                    error_code=exc.code,
                )
                outcomes["failed" if finished else "lost_lease"] += 1
            else:
                outcomes["succeeded" if persisted is not None else "lost_lease"] += 1
                if persisted is True:
                    ready_job_ids.add(row.job_id)
            finally:
                lease_guard.release(row.unit_id)
    return EvalBatchProcessResult(outcomes, 1, tuple(sorted(ready_job_ids, key=str)))


async def drain_eval_graph_extraction_batches(
    session_factory,
    *,
    worker_index: int,
    batch_size: int,
) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    while True:
        async with session_factory() as db:
            units = await claim_eval_graph_extraction_batch(
                db,
                worker_id=f"m6-batch-eval-{worker_index}",
                batch_size=batch_size,
                lease_seconds=settings.graph_extraction_unit_lease_seconds,
                max_attempts=settings.graph_extraction_worker_max_model_attempts,
            )
        if not units:
            return outcomes
        result = await process_eval_graph_extraction_batch(
            session_factory,
            units=units,
        )
        outcomes.update(result.outcomes)
