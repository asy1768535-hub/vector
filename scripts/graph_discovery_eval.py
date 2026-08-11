"""Evaluate discovery/extraction predictions against the 20/18 gold corpus.

The scorer is intentionally independent of a live model.  A deterministic fake
provider can exercise the pipeline in tests; a real provider run can pass its
actual predictions to this module and retain the resulting artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.graph_normalization import (
    evidence_backed_aliases_v1,
    filter_identifier_aliases_v1,
    normalize_graph_name_v1,
)


_LAST_REAL_MODEL_RESPONSE: str | None = None
_REAL_EVAL_TRACE: list[dict[str, Any]] = []
_REAL_EVAL_COMPLETED_STAGES: list[str] = []
_SEMANTIC_ALIGNMENT_FILENAME = "semantic_alignment.json"


def _redacted_value(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"<redacted chars={len(value)} sha256={digest}>"


def _desensitize_json(value: Any) -> Any:
    if isinstance(value, list):
        return [_desensitize_json(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {
            "text",
            "untrusted_context",
            "quote",
            "name",
            "canonical_name",
            "label",
            "description",
        } and isinstance(item, str):
            result[key] = _redacted_value(item)
        elif key == "aliases" and isinstance(item, list):
            result[key] = [
                _redacted_value(alias) if isinstance(alias, str) else _desensitize_json(alias)
                for alias in item
            ]
        else:
            result[key] = _desensitize_json(item)
    return result


def _desensitize_content(content: str) -> str:
    start = content.find("{")
    if start < 0:
        return content
    try:
        value = json.loads(content[start:])
    except json.JSONDecodeError:
        return _redacted_value(content)
    return content[:start] + json.dumps(
        _desensitize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _recorded_request(stage: str, messages) -> dict[str, Any]:
    serialized_messages = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "kind": "desensitized_real_request",
        "payload_hash": hashlib.sha256(serialized_messages.encode("utf-8")).hexdigest(),
        "messages": [
            {**message, "content": _desensitize_content(message.get("content", ""))}
            for message in messages
        ],
    }


def _recorded_exchange(stage: str, messages, response) -> dict[str, Any]:
    return {
        "stage": stage,
        "request": _recorded_request(stage, messages),
        "response": {
            "kind": "desensitized_real_response",
            "content_hash": hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
            "content": _desensitize_content(response.content),
            "finish_reason": response.finish_reason,
            "input_token_count": response.input_token_count,
            "output_token_count": response.output_token_count,
            "latency_ms": response.latency_ms,
            "provider_request_id": (
                _redacted_value(response.provider_request_id)
                if response.provider_request_id
                else None
            ),
        },
    }


def _exchange_stage(parent_stage: str, messages) -> str:
    if parent_stage != "discovery":
        return parent_stage
    system = messages[0].get("content", "") if messages else ""
    user = messages[-1].get("content", "") if messages else ""
    if "concept inventory protocol" in system:
        return "concept_inventory"
    if "refinement_instruction" in user:
        return "semantic_refinement"
    if "repair_instruction" in user or "previous_schema" in user:
        return "schema_repair"
    return "schema_synthesis"


class _RecordingProvider:
    """Record desensitized copies of the exact provider exchanges."""

    def __init__(
        self,
        provider,
        *,
        stage: str,
        trace: list[dict[str, Any]],
        state: dict[str, str | None] | None = None,
    ) -> None:
        self._provider = provider
        self._stage = stage
        self._trace = trace
        self._state = state if state is not None else {"response": None}
        self.supports_concept_inventory = bool(
            getattr(provider, "supports_concept_inventory", False)
        )

    def with_output_budget(self, max_output_tokens: int):
        return _RecordingProvider(
            self._provider.with_output_budget(max_output_tokens),
            stage=self._stage,
            trace=self._trace,
            state=self._state,
        )

    async def extract(self, messages):
        stage = _exchange_stage(self._stage, messages)
        try:
            response = await self._provider.extract(messages)
        except Exception as exc:  # noqa: BLE001 - retain attempted request audit
            self._trace.append(
                {
                    "stage": stage,
                    "request": _recorded_request(stage, messages),
                    "response": {
                        "kind": "desensitized_real_response_error",
                        "error_type": type(exc).__name__,
                    },
                }
            )
            raise
        self._state["response"] = response.content
        self._trace.append(_recorded_exchange(stage, messages, response))
        global _LAST_REAL_MODEL_RESPONSE
        _LAST_REAL_MODEL_RESPONSE = response.content
        return response


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _extraction_name_key(value: Any) -> str:
    """Use the production name identity for extraction aggregation only."""

    return normalize_graph_name_v1(str(value or ""))


def _extraction_entity_names(entity: dict[str, Any]) -> set[str]:
    return {
        _extraction_name_key(entity.get("canonical_name") or entity.get("name")),
        *(_extraction_name_key(alias) for alias in entity.get("aliases", [])),
    } - {""}


def _entity_names(entity: dict[str, Any]) -> set[str]:
    return {
        _normalise(entity.get("canonical_name") or entity.get("name")),
        *(_normalise(alias) for alias in entity.get("aliases", [])),
    } - {""}


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _prf(*, matched: int, predicted: int, gold: int) -> dict[str, float]:
    precision = matched / predicted if predicted else (1.0 if gold == 0 else 0.0)
    recall = matched / gold if gold else 1.0
    return {"precision": precision, "recall": recall, "f1": _f1(precision, recall)}


def _match_entities(
    gold_entities: list[dict[str, Any]],
    predicted_entities: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    matched: dict[str, dict[str, Any]] = {}
    used: set[int] = set()
    for gold_entity in gold_entities:
        gold_names = {_normalise(gold_entity.get("canonical_name"))}
        gold_names.update(_normalise(alias) for alias in gold_entity.get("aliases", []))
        for index, predicted in enumerate(predicted_entities):
            if index not in used and gold_names & _entity_names(predicted):
                matched[str(gold_entity["gold_id"])] = predicted
                used.add(index)
                break
    return matched, used


def _relation_matches(
    gold: dict[str, Any],
    predictions: list[dict[str, Any]],
    matched_entities: dict[str, dict[str, Any]],
    *,
    semantic_alignment: dict[str, Any] | None = None,
) -> tuple[int, int, int, int]:
    used: set[int] = set()
    exact = 0
    direction_correct = 0
    direction_total = 0
    for relation in gold.get("gold_relations", []):
        source = matched_entities.get(str(relation["source_gold_id"]))
        target = matched_entities.get(str(relation["target_gold_id"]))
        if source is None or target is None:
            continue
        source_names = _entity_names(source)
        target_names = _entity_names(target)
        expected_directed = bool(relation.get("directed", True))
        for index, prediction in enumerate(predictions):
            if index in used:
                continue
            equivalents = [(str(prediction.get("relation_type_key") or ""), False)]
            if semantic_alignment is not None:
                equivalents.extend(
                    (
                        str(row.get("gold_relation_key") or ""),
                        bool(row.get("reverse_endpoints", False)),
                    )
                    for row in semantic_alignment.get("relation_equivalences", [])
                    if isinstance(row, dict)
                    and row.get("predicted_relation_key") == prediction.get("relation_type_key")
                )
            equivalent = next(
                (
                    row
                    for row in equivalents
                    if row[0] == relation.get("relation_type_key")
                ),
                None,
            )
            if equivalent is None:
                continue
            source_name = _normalise(prediction.get("target_name" if equivalent[1] else "source_name"))
            target_name = _normalise(prediction.get("source_name" if equivalent[1] else "target_name"))
            if source_name not in source_names or target_name not in target_names:
                continue
            used.add(index)
            exact += 1
            direction_total += 1
            direction_correct += int(bool(prediction.get("directed", True)) == expected_directed)
            break
    return exact, len(used), direction_correct, direction_total


def score_predictions(
    gold: dict[str, Any],
    predictions: dict[str, Any],
    *,
    semantic_alignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gold_entities = list(gold.get("gold_entities", []))
    predicted_entities = list(predictions.get("entities", []))
    gold_relations = list(gold.get("gold_relations", []))
    predicted_relations = list(predictions.get("relations", []))
    matched_entities, used_entity_ids = _match_entities(gold_entities, predicted_entities)

    entity_metrics = _prf(
        matched=len(matched_entities), predicted=len(predicted_entities), gold=len(gold_entities)
    )
    accepted_type_aliases = (
        semantic_alignment.get("accepted_entity_type_aliases", {})
        if semantic_alignment is not None
        else {}
    )
    type_matches = sum(
        predicted.get("entity_type_key") == gold_entity.get("entity_type_key")
        or gold_entity.get("entity_type_key")
        in accepted_type_aliases.get(predicted.get("entity_type_key"), [])
        for gold_entity in gold_entities
        for predicted in [matched_entities.get(str(gold_entity["gold_id"]))]
        if predicted is not None
    )
    type_accuracy = type_matches / len(matched_entities) if matched_entities else 0.0

    relation_matches, used_relation_ids, direction_correct, direction_total = _relation_matches(
        gold,
        predicted_relations,
        matched_entities,
        semantic_alignment=semantic_alignment,
    )
    relation_metrics = _prf(
        matched=relation_matches,
        predicted=len(predicted_relations),
        gold=len(gold_relations),
    )
    aliases = [alias for entity in gold_entities for alias in entity.get("aliases", [])]
    merged_aliases = sum(
        _normalise(alias) in _entity_names(matched_entities[str(entity["gold_id"])])
        for entity in gold_entities
        for alias in entity.get("aliases", [])
        if str(entity["gold_id"]) in matched_entities
    )
    gold_name_union = {
        name
        for entity in gold_entities
        for name in ({_normalise(entity.get("canonical_name"))} | {
            _normalise(alias) for alias in entity.get("aliases", [])
        })
        if name
    }
    matching_prediction_count = sum(
        bool(gold_name_union & _entity_names(entity)) for entity in predicted_entities
    )
    duplicate_entities = max(0, matching_prediction_count - len(matched_entities))
    return {
        "entity_precision": entity_metrics["precision"],
        "entity_recall": entity_metrics["recall"],
        "entity_f1": entity_metrics["f1"],
        "relation_precision": relation_metrics["precision"],
        "relation_recall": relation_metrics["recall"],
        "relation_f1": relation_metrics["f1"],
        "type_accuracy": type_accuracy,
        "relation_direction_accuracy": direction_correct / direction_total if direction_total else 0.0,
        "alias_merge_rate": merged_aliases / len(aliases) if aliases else 1.0,
        "duplicate_entities": duplicate_entities,
        "extra_entities": len(predicted_entities) - len(used_entity_ids),
        "extra_relations": len(predicted_relations) - used_relation_ids,
        "gold_entity_count": len(gold_entities),
        "gold_relation_count": len(gold_relations),
        "predicted_entity_count": len(predicted_entities),
        "predicted_relation_count": len(predicted_relations),
    }


def write_eval_artifact(
    *,
    gold: dict[str, Any],
    predictions: dict[str, Any],
    output_dir: Path,
    model: str,
    prompt_hash: str,
    schema_hash: str,
    run_kind: str,
    trace: list[dict[str, Any]] | None = None,
    completed_stages: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    pipeline_counts: dict[str, int] | None = None,
    discovery_summary: dict[str, Any] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = output_dir / f"graph-discovery-{run_kind}-{timestamp}.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "graph-discovery-eval-artifact-v2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "run_kind": run_kind,
                "model": model,
                "prompt_hash": prompt_hash,
                "schema_hash": schema_hash,
                "status": "success",
                "completed_stages": completed_stages or [],
                "real_provider_exchanges": trace or [],
                "gold_hash": hashlib.sha256(
                    json.dumps(gold, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "metrics": metrics if metrics is not None else score_predictions(gold, predictions),
                "pipeline_counts": pipeline_counts or {},
                "discovery_summary": discovery_summary or {},
                "predictions": predictions,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return artifact


def write_eval_failure_artifact(
    *,
    gold: dict[str, Any],
    output_dir: Path,
    model: str,
    prompt_hash: str,
    schema_hash: str,
    run_kind: str,
    error: BaseException,
    response_content: str | None = None,
    trace: list[dict[str, Any]] | None = None,
    completed_stages: list[str] | None = None,
) -> Path:
    """Persist a failed real-model run instead of turning it into a score."""

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = output_dir / f"graph-discovery-{run_kind}-{timestamp}-failed.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "graph-discovery-eval-artifact-v2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "run_kind": run_kind,
                "status": "failed",
                "model": model,
                "prompt_hash": prompt_hash,
                "schema_hash": schema_hash,
                "completed_stages": completed_stages or [],
                "real_provider_exchanges": trace or [],
                "gold_hash": hashlib.sha256(
                    json.dumps(gold, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "error": {"type": type(error).__name__, "message": str(error)[:4000]},
                "last_model_response": (
                    _desensitize_content((response_content or "")[:20000])
                    if response_content
                    else None
                ),
                "predictions": {"entities": [], "relations": []},
                "metrics": None,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return artifact


def _relation_is_directed(snapshot: dict[str, Any], relation_key: str) -> bool:
    for row in snapshot.get("relation_types", []):
        if row.get("key") == relation_key:
            return row.get("direction", "directed") == "directed"
    return True


def _formal_relation_counts(raw_content: str, expected_keys: tuple[str, ...]) -> tuple[int, int]:
    """Count model relations before and after same-batch endpoint validation."""

    from app.services.graph_extraction_batch_eval import (
        _coerce_batched_response_rows,
        _without_undeclared_endpoint_relations,
    )

    rows = _coerce_batched_response_rows(json.loads(raw_content), expected_keys)
    raw_count = sum(len(row.get("relations", [])) for row in rows if isinstance(row, dict))
    endpoint_count = sum(
        len(
            _without_undeclared_endpoint_relations(
                {key: value for key, value in row.items() if key != "batch_key"}
            ).get("relations", [])
        )
        for row in rows
        if isinstance(row, dict)
    )
    return raw_count, endpoint_count


def _load_semantic_alignment(gold_path: Path) -> dict[str, Any]:
    path = gold_path.with_name(_SEMANTIC_ALIGNMENT_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "graph-discovery-semantic-alignment-v1":
        raise ValueError("semantic alignment fixture schema is invalid")
    return payload


def _aggregate_extraction_outputs(
    outputs: list[tuple[str, Any]],
    *,
    snapshot: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Merge per-document payloads before validation and export."""

    entities: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    relation_indexes: dict[tuple[Any, ...], int] = {}

    def find_entity(entity: Any, aliases: list[str]) -> dict[str, Any] | None:
        names = {
            _extraction_name_key(entity.name),
            *(_extraction_name_key(alias) for alias in aliases),
        } - {""}
        for row in entities:
            if (
                row["entity_type_key"] == entity.entity_type_key
                and names & _extraction_entity_names(row)
            ):
                return row
        return None

    for batch_key, payload in outputs:
        local_to_global: dict[str, dict[str, Any]] = {}
        for entity in payload.entities:
            derived_aliases = evidence_backed_aliases_v1(
                name=entity.name,
                explicit_aliases=entity.aliases,
                properties=entity.properties,
                evidence_quotes=(item.quote for item in entity.evidence),
            )
            entity_aliases = [*entity.aliases, *derived_aliases]
            row = find_entity(entity, entity_aliases)
            if row is None:
                row = {
                    "canonical_name": entity.name,
                    "entity_type_key": entity.entity_type_key,
                    "aliases": entity_aliases,
                    "evidence": [item.model_dump() for item in entity.evidence],
                }
                entities.append(row)
            else:
                aliases = set(row.get("aliases", []))
                aliases.update(entity_aliases)
                if entity.name != row["canonical_name"]:
                    aliases.add(entity.name)
                row["aliases"] = sorted(alias for alias in aliases if alias)
                row.setdefault("evidence", []).extend(item.model_dump() for item in entity.evidence)
            local_to_global[entity.local_id] = row
        for relation in payload.relations:
            source = local_to_global.get(relation.source_local_id)
            target = local_to_global.get(relation.target_local_id)
            if source is None or target is None:
                continue
            current = {
                "source_name": source["canonical_name"],
                "relation_type_key": relation.relation_type_key,
                "target_name": target["canonical_name"],
                "directed": _relation_is_directed(snapshot, relation.relation_type_key),
                "evidence": [item.model_dump() for item in relation.evidence],
            }
            directed = bool(current["directed"])
            endpoint_keys = (
                _extraction_name_key(current["source_name"]),
                _extraction_name_key(current["target_name"]),
            )
            if not directed:
                endpoint_keys = tuple(sorted(endpoint_keys))
            relation_key = (current["relation_type_key"], directed, *endpoint_keys)
            previous_index = relation_indexes.get(relation_key)
            if previous_index is None:
                relation_indexes[relation_key] = len(relation_rows)
                relation_rows.append(current)
                continue
            previous = relation_rows[previous_index]
            evidence = previous.setdefault("evidence", [])
            seen_evidence = {
                (item.get("context_ref"), item.get("quote"))
                for item in evidence
                if isinstance(item, dict)
            }
            for item in current["evidence"]:
                marker = (item.get("context_ref"), item.get("quote"))
                if marker not in seen_evidence:
                    evidence.append(item)
                    seen_evidence.add(marker)
    return {"entities": filter_identifier_aliases_v1(entities), "relations": relation_rows}


def _validate_export(
    predictions: dict[str, list[dict[str, Any]]],
    snapshot: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    type_by_id = {row.get("id"): row for row in snapshot.get("entity_types", [])}
    relation_by_id = {row.get("id"): row.get("key") for row in snapshot.get("relation_types", [])}
    constraints = {
        (
            relation_by_id.get(row.get("relation_type_id")),
            type_by_id.get(row.get("source_entity_type_id"), {}).get("key"),
            type_by_id.get(row.get("target_entity_type_id"), {}).get("key"),
        )
        for row in snapshot.get("relation_constraints", [])
    }
    valid_relations: list[dict[str, Any]] = []
    for relation in predictions["relations"]:
        source = next(
            (
                row
                for row in predictions["entities"]
                if _extraction_name_key(row["canonical_name"])
                == _extraction_name_key(relation["source_name"])
                or _extraction_name_key(relation["source_name"])
                in {_extraction_name_key(alias) for alias in row.get("aliases", [])}
            ),
            None,
        )
        target = next(
            (
                row
                for row in predictions["entities"]
                if _extraction_name_key(row["canonical_name"])
                == _extraction_name_key(relation["target_name"])
                or _extraction_name_key(relation["target_name"])
                in {_extraction_name_key(alias) for alias in row.get("aliases", [])}
            ),
            None,
        )
        if source is None or target is None:
            continue
        if not constraints or (
            relation["relation_type_key"],
            source["entity_type_key"],
            target["entity_type_key"],
        ) in constraints:
            valid_relations.append(relation)
    return {"entities": predictions["entities"], "relations": valid_relations}


async def run_real_evaluation(*, gold_path: Path, output_dir: Path) -> Path:
    """Run discovery, extraction, merge, validation, export and scoring."""

    _REAL_EVAL_TRACE.clear()
    _REAL_EVAL_COMPLETED_STAGES.clear()

    from app.config import settings
    from app.services.graph_extraction_batch_eval import (
        GraphExtractionBatchInput,
        build_batched_graph_extraction_messages,
        parse_batched_graph_extraction_output,
        plan_graph_extraction_batches,
    )
    from app.services.graph_extraction_provider import OpenAICompatibleGraphExtractor
    from app.services.graph_schema_discovery import (
        DiscoveryText,
        discover_business_schema,
        schema_draft_to_snapshot,
        validate_business_schema_draft,
    )
    from app.services.graph_canonical import canonical_graph_value_hash_v1

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    source_hash = hashlib.sha256(
        json.dumps(gold["documents"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    discovery_output_budget = (
        settings.graph_extraction_max_output_tokens
        if settings.graph_extraction_output_budget_enabled
        else None
    )
    extraction_output_budget = (
        min(
            settings.graph_extraction_max_output_tokens,
            max(512, settings.graph_extraction_context_window_tokens // 2),
        )
        if settings.graph_extraction_output_budget_enabled
        else None
    )
    extraction_planning_budget = extraction_output_budget or max(
        512,
        settings.graph_extraction_context_window_tokens // 2,
    )
    evaluation_context_window = max(
        settings.graph_extraction_context_window_tokens,
        32_768 if discovery_output_budget is None else 0,
    )
    discovery_provider = _RecordingProvider(
        OpenAICompatibleGraphExtractor(
            base_url=settings.graph_extraction_base_url,
            model=settings.graph_extraction_model,
            api_key=settings.graph_extraction_api_key.get_secret_value(),
            timeout_seconds=settings.graph_extraction_timeout_seconds,
            max_output_tokens=discovery_output_budget,
        ),
        stage="discovery",
        trace=_REAL_EVAL_TRACE,
    )
    extraction_provider = _RecordingProvider(
        OpenAICompatibleGraphExtractor(
            base_url=settings.graph_extraction_base_url,
            model=settings.graph_extraction_model,
            api_key=settings.graph_extraction_api_key.get_secret_value(),
            timeout_seconds=settings.graph_extraction_timeout_seconds,
            max_output_tokens=extraction_output_budget,
        ),
        stage="formal_extraction",
        trace=_REAL_EVAL_TRACE,
    )
    texts = [DiscoveryText(row["document_key"], row["text"]) for row in gold["documents"]]
    draft = await discover_business_schema(
        texts,
        provider=discovery_provider,
        source_hash=source_hash,
        context_window_tokens=evaluation_context_window,
        max_output_tokens=discovery_output_budget,
        unbounded_output=discovery_output_budget is None,
    )
    _REAL_EVAL_COMPLETED_STAGES.append("discovery")
    validate_business_schema_draft(draft)
    _REAL_EVAL_COMPLETED_STAGES.append("draft_validation")
    snapshot = schema_draft_to_snapshot(draft, ontology_version_id=uuid.uuid4())
    _REAL_EVAL_COMPLETED_STAGES.append("frozen_snapshot")
    inputs = tuple(GraphExtractionBatchInput(row["document_key"], row["text"]) for row in gold["documents"])
    planned = plan_graph_extraction_batches(
        ontology_snapshot=snapshot,
        inputs=inputs,
        schema_routing_enabled=True,
        context_window_tokens=evaluation_context_window,
        max_output_tokens=extraction_planning_budget,
        max_entity_types=12,
    )
    extracted: list[tuple[str, Any]] = []
    raw_relation_count = 0
    endpoint_valid_relation_count = 0
    allowed_entities = {row["key"] for row in snapshot["entity_types"]}
    allowed_relations = {row["key"] for row in snapshot["relation_types"]}
    # Keep each real-model request to one document.  This makes the protocol
    # key unambiguous while the planner still enforces the shared Schema and
    # exact token budget for every request.
    for batch in ((item,) for group in planned for item in group):
        response = await extraction_provider.extract(
            build_batched_graph_extraction_messages(
                ontology_snapshot=snapshot,
                batches=batch,
                schema_routing_enabled=True,
                max_entity_types=12,
                context_window_tokens=evaluation_context_window,
                max_output_tokens=extraction_planning_budget,
            )
        )
        parsed = parse_batched_graph_extraction_output(
            response.content,
            expected_keys=tuple(row.batch_key for row in batch),
            allowed_entity_type_keys=allowed_entities,
            allowed_relation_type_keys=allowed_relations,
        )
        raw_count, endpoint_count = _formal_relation_counts(
            response.content,
            tuple(row.batch_key for row in batch),
        )
        raw_relation_count += raw_count
        endpoint_valid_relation_count += endpoint_count
        extracted.extend(parsed.items())
    _REAL_EVAL_COMPLETED_STAGES.append("formal_extraction")
    aggregated = _aggregate_extraction_outputs(extracted, snapshot=snapshot)
    predictions = _validate_export(aggregated, snapshot)
    strict_metrics = score_predictions(gold, predictions)
    metrics = {
        **strict_metrics,
        "semantic_aligned": score_predictions(
            gold,
            predictions,
            semantic_alignment=_load_semantic_alignment(gold_path),
        ),
    }
    _REAL_EVAL_COMPLETED_STAGES.append("scoring")
    return write_eval_artifact(
        gold=gold,
        predictions=predictions,
        output_dir=output_dir,
        model=settings.graph_extraction_model,
        prompt_hash=canonical_graph_value_hash_v1({"discovery": "v1", "extraction": "v4"}),
        schema_hash=canonical_graph_value_hash_v1(snapshot),
        run_kind="real",
        trace=_REAL_EVAL_TRACE,
        completed_stages=_REAL_EVAL_COMPLETED_STAGES,
        metrics=metrics,
        pipeline_counts={
            "raw_model_relations": raw_relation_count,
            "after_endpoint_filter": endpoint_valid_relation_count,
            "after_parser_and_schema_key_validation": sum(
                len(payload.relations) for _batch_key, payload in extracted
            ),
            "after_aggregation": len(aggregated["relations"]),
            "after_export_constraint_validation": len(predictions["relations"]),
        },
        discovery_summary={
            "concept_count": len(draft.concept_inventory),
            "entity_type_count": len(snapshot["entity_types"]),
            "entity_type_keys": [row["key"] for row in snapshot["entity_types"]],
            "relation_type_count": len(snapshot["relation_types"]),
            "relation_type_keys": [row["key"] for row in snapshot["relation_types"]],
            "constraint_count": len(snapshot["relation_constraints"]),
            "semantic_refinement": draft.trace.get("semantic_refinement", {}),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, default=Path("eval/graph_extraction/gold_standard.json"))
    parser.add_argument(
        "--predictions",
        type=Path,
        help="prediction JSON for scoring an existing run; not needed with --run-real",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("eval/graph_extraction/results"))
    parser.add_argument("--model", default="unknown")
    parser.add_argument("--prompt-hash", default="unknown")
    parser.add_argument("--schema-hash", default="unknown")
    parser.add_argument("--run-kind", choices=("fake", "real"), default="real")
    parser.add_argument("--run-real", action="store_true")
    args = parser.parse_args()
    if args.run_real:
        try:
            artifact = asyncio.run(run_real_evaluation(gold_path=args.gold, output_dir=args.output_dir))
        except Exception as exc:  # noqa: BLE001 - the failure is the evaluation result
            from app.config import settings

            gold = json.loads(args.gold.read_text(encoding="utf-8"))
            artifact = write_eval_failure_artifact(
                gold=gold,
                output_dir=args.output_dir,
                model=settings.graph_extraction_model,
                prompt_hash=hashlib.sha256(b"schema-discovery-v1+graph-extraction-v4").hexdigest(),
                schema_hash="unavailable_discovery_failed",
                run_kind="real",
                error=exc,
                response_content=_LAST_REAL_MODEL_RESPONSE,
                trace=_REAL_EVAL_TRACE,
                completed_stages=_REAL_EVAL_COMPLETED_STAGES,
            )
            print(json.dumps({"artifact": str(artifact), "status": "failed"}, ensure_ascii=False, indent=2))
            raise
        return
    if args.predictions is None:
        parser.error("--predictions is required unless --run-real is used")
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
    artifact = write_eval_artifact(
        gold=gold,
        predictions=predictions,
        output_dir=args.output_dir,
        model=args.model,
        prompt_hash=args.prompt_hash,
        schema_hash=args.schema_hash,
        run_kind=args.run_kind,
    )
    print(json.dumps({"artifact": str(artifact), "metrics": score_predictions(gold, predictions)}, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
