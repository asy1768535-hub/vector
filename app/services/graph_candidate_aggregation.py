from __future__ import annotations

import math
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphExtractionConflict
from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.graph_canonical import (
    canonical_graph_json_v1,
    canonical_graph_value_hash_v1,
)
from app.services.graph_candidate_evidence import (
    create_entity_candidate_evidence,
    create_relation_candidate_evidence,
)
from app.services.graph_normalization import normalize_graph_name_v1


class CandidateAggregationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CandidateReplayError(CandidateAggregationError):
    pass


@dataclass(frozen=True)
class EntityAggregateOccurrence:
    unit_ordinal: int
    local_ref: str
    model_confidence: float | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class RelationAggregateOccurrence:
    unit_ordinal: int
    ordinal: int
    model_confidence: float | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class PropertyConflictSpec:
    field: str
    values_by_hash: tuple[tuple[str, Any], ...]

    @property
    def value_hashes(self) -> tuple[str, ...]:
        return tuple(item[0] for item in self.values_by_hash)


@dataclass(frozen=True)
class EntityAggregate:
    canonical_name: str
    normalized_name: str
    proposed_aliases: list[str]
    proposed_properties: dict[str, Any]
    external_mapping_hints: list[dict[str, str]]
    model_confidence: float | None
    property_conflicts: tuple[PropertyConflictSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RelationAggregate:
    proposed_properties: dict[str, Any]
    model_confidence: float | None


@dataclass(frozen=True)
class UnitCandidateStageResult:
    entity_candidate_ids: tuple[uuid.UUID, ...]
    relation_candidate_ids: tuple[uuid.UUID, ...]
    entity_occurrence_ids: tuple[uuid.UUID, ...]
    relation_occurrence_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class JobCandidateAggregateResult:
    entity_candidate_count: int
    relation_candidate_count: int
    property_conflict_count: int


def _canonical_uuid(value: uuid.UUID | str | None, *, nullable: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        raise TypeError("UUID value cannot be null")
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("value must be a UUID") from exc


def _require_non_empty(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def entity_candidate_key_v1(
    *,
    ontology_version_id: uuid.UUID | str,
    entity_type_key: str,
    normalized_name: str,
) -> str:
    return canonical_graph_value_hash_v1(
        {
            "ontology_version_id": _canonical_uuid(ontology_version_id),
            "entity_type_key": _require_non_empty(entity_type_key, field="entity_type_key"),
            "normalized_name": _require_non_empty(normalized_name, field="normalized_name"),
        }
    )


def relation_candidate_key_v1(
    *,
    ontology_version_id: uuid.UUID | str,
    source_candidate_key: str,
    relation_type_key: str,
    target_candidate_key: str,
    properties: dict[str, Any],
) -> str:
    if not isinstance(properties, dict):
        raise TypeError("properties must be an object")
    return canonical_graph_value_hash_v1(
        {
            "ontology_version_id": _canonical_uuid(ontology_version_id),
            "source_candidate_key": _require_non_empty(
                source_candidate_key, field="source_candidate_key"
            ),
            "relation_type_key": _require_non_empty(
                relation_type_key, field="relation_type_key"
            ),
            "target_candidate_key": _require_non_empty(
                target_candidate_key, field="target_candidate_key"
            ),
            "properties": properties,
        }
    )


def merge_candidate_key_v1(
    *,
    job_id: uuid.UUID | str,
    entity_candidate_id: uuid.UUID | str,
    suggested_target_entity_id: uuid.UUID | str | None,
    reason: str,
) -> str:
    return canonical_graph_value_hash_v1(
        {
            "job_id": _canonical_uuid(job_id),
            "entity_candidate_id": _canonical_uuid(entity_candidate_id),
            "suggested_target_entity_id": _canonical_uuid(
                suggested_target_entity_id, nullable=True
            ),
            "reason": _require_non_empty(reason, field="reason"),
        }
    )


def _sorted_uuid_strings(values: Iterable[uuid.UUID | str]) -> list[str]:
    return sorted({_canonical_uuid(value) for value in values})


def _canonical_conflicting_fields(
    conflicting_fields: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_field: dict[str, set[str]] = {}
    for item in conflicting_fields:
        if not isinstance(item, dict) or set(item) != {"field", "value_hashes"}:
            raise ValueError("conflicting_fields entries must contain field and value_hashes")
        field = _require_non_empty(item["field"], field="conflicting field")
        hashes = item["value_hashes"]
        if not isinstance(hashes, list):
            raise TypeError("value_hashes must be a list")
        target = by_field.setdefault(field, set())
        for value_hash in hashes:
            target.add(_require_non_empty(value_hash, field="value_hash"))
    return [
        {"field": field, "value_hashes": sorted(value_hashes)}
        for field, value_hashes in sorted(by_field.items())
    ]


def conflict_key_v1(
    *,
    job_id: uuid.UUID | str,
    conflict_type: str,
    entity_candidate_ids: Iterable[uuid.UUID | str],
    relation_candidate_ids: Iterable[uuid.UUID | str],
    conflicting_fields: Iterable[dict[str, Any]],
) -> str:
    entity_ids = _sorted_uuid_strings(entity_candidate_ids)
    relation_ids = _sorted_uuid_strings(relation_candidate_ids)
    if not entity_ids and not relation_ids:
        raise ValueError("conflict must reference at least one Candidate")
    return canonical_graph_value_hash_v1(
        {
            "job_id": _canonical_uuid(job_id),
            "conflict_type": _require_non_empty(conflict_type, field="conflict_type"),
            "entity_candidate_ids": entity_ids,
            "relation_candidate_ids": relation_ids,
            "conflicting_fields": _canonical_conflicting_fields(conflicting_fields),
        }
    )


def _require_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateAggregationError(
            "invalid_occurrence_payload", "model confidence must be numeric"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise CandidateAggregationError(
            "invalid_occurrence_payload", "model confidence must be between 0 and 1"
        )
    return result


def _payload_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateAggregationError(
            "invalid_occurrence_payload", "Occurrence raw_payload must be an object"
        )
    canonical_graph_json_v1(value)
    return value


def _aggregate_aliases(occurrences: Iterable[dict[str, Any]]) -> list[str]:
    aliases_by_normalized: dict[str, str] = {}
    for raw_payload in occurrences:
        aliases = raw_payload.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise CandidateAggregationError(
                "invalid_occurrence_payload", "Entity aliases must be a string array"
            )
        for alias in aliases:
            original = alias.strip()
            normalized = normalize_graph_name_v1(original)
            if not normalized:
                continue
            current = aliases_by_normalized.get(normalized)
            if current is None or original < current:
                aliases_by_normalized[normalized] = original
    return [
        original
        for _, original in sorted(
            aliases_by_normalized.items(), key=lambda item: (item[0], item[1])
        )
    ]


def _aggregate_external_mapping_hints(
    occurrences: Iterable[dict[str, Any]],
) -> list[dict[str, str]]:
    by_json: dict[str, dict[str, str]] = {}
    for raw_payload in occurrences:
        hints = raw_payload.get("external_mapping_hints", [])
        if not isinstance(hints, list):
            raise CandidateAggregationError(
                "invalid_occurrence_payload", "external_mapping_hints must be an array"
            )
        for hint in hints:
            if not isinstance(hint, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in hint.items()
            ):
                raise CandidateAggregationError(
                    "invalid_occurrence_payload",
                    "external_mapping_hints entries must be string objects",
                )
            encoded = canonical_graph_json_v1(hint)
            by_json.setdefault(encoded, deepcopy(hint))
    return [by_json[key] for key in sorted(by_json)]


def _aggregate_properties(
    occurrences: list[dict[str, Any]],
) -> tuple[dict[str, Any], tuple[PropertyConflictSpec, ...]]:
    keys: set[str] = set()
    for raw_payload in occurrences:
        properties = raw_payload.get("properties", {})
        if not isinstance(properties, dict):
            raise CandidateAggregationError(
                "invalid_occurrence_payload", "properties must be an object"
            )
        canonical_graph_json_v1(properties)
        keys.update(properties)

    aggregate: dict[str, Any] = {}
    conflicts: list[PropertyConflictSpec] = []
    for key in sorted(keys):
        values_by_hash: dict[str, Any] = {}
        representative_value: Any = None
        representative_found = False
        for raw_payload in occurrences:
            properties = raw_payload["properties"]
            if key not in properties:
                continue
            value = properties[key]
            if not representative_found:
                representative_value = deepcopy(value)
                representative_found = True
            value_hash = canonical_graph_value_hash_v1(value)
            values_by_hash.setdefault(value_hash, deepcopy(value))
        if representative_found:
            aggregate[key] = representative_value
        if len(values_by_hash) > 1:
            conflicts.append(
                PropertyConflictSpec(
                    field=f"properties.{key}",
                    values_by_hash=tuple(sorted(values_by_hash.items())),
                )
            )
    return aggregate, tuple(conflicts)


def aggregate_entity_occurrence_payloads(
    occurrences: Iterable[EntityAggregateOccurrence],
) -> EntityAggregate:
    ordered = sorted(
        occurrences,
        key=lambda item: (item.unit_ordinal, item.local_ref),
    )
    if not ordered:
        raise CandidateAggregationError(
            "missing_occurrences", "Entity Candidate has no non-purged Occurrences"
        )
    payloads = [_payload_object(item.raw_payload) for item in ordered]
    representative = payloads[0]
    name = representative.get("name")
    if not isinstance(name, str):
        raise CandidateAggregationError(
            "invalid_occurrence_payload", "Entity name must be a string"
        )
    canonical_name = name.strip()
    normalized_name = normalize_graph_name_v1(canonical_name)
    if not normalized_name:
        raise CandidateAggregationError(
            "invalid_entity_name", "Entity name is empty after normalization"
        )
    properties, conflicts = _aggregate_properties(payloads)
    confidences = [
        confidence
        for item in ordered
        if (confidence := _require_confidence(item.model_confidence)) is not None
    ]
    return EntityAggregate(
        canonical_name=canonical_name,
        normalized_name=normalized_name,
        proposed_aliases=_aggregate_aliases(payloads),
        proposed_properties=properties,
        external_mapping_hints=_aggregate_external_mapping_hints(payloads),
        model_confidence=max(confidences) if confidences else None,
        property_conflicts=conflicts,
    )


def aggregate_relation_occurrence_payloads(
    occurrences: Iterable[RelationAggregateOccurrence],
) -> RelationAggregate:
    ordered = sorted(occurrences, key=lambda item: (item.unit_ordinal, item.ordinal))
    if not ordered:
        raise CandidateAggregationError(
            "missing_occurrences", "Relation Candidate has no non-purged Occurrences"
        )
    payloads = [_payload_object(item.raw_payload) for item in ordered]
    representative_properties = payloads[0].get("properties", {})
    if not isinstance(representative_properties, dict):
        raise CandidateAggregationError(
            "invalid_occurrence_payload", "Relation properties must be an object"
        )
    expected = canonical_graph_json_v1(representative_properties)
    for payload in payloads[1:]:
        properties = payload.get("properties", {})
        if not isinstance(properties, dict) or canonical_graph_json_v1(properties) != expected:
            raise CandidateAggregationError(
                "relation_candidate_key_mismatch",
                "Relation Candidate contains non-canonical properties",
            )
    confidences = [
        confidence
        for item in ordered
        if (confidence := _require_confidence(item.model_confidence)) is not None
    ]
    return RelationAggregate(
        proposed_properties=deepcopy(representative_properties),
        model_confidence=max(confidences) if confidences else None,
    )


def _validate_stage_scope(*, job: Any, unit: Any, snapshot: Any) -> None:
    if (
        getattr(unit, "job_id", None) != job.id
        or getattr(unit, "library_id", None) != job.library_id
        or getattr(unit, "document_revision_id", None) != job.document_revision_id
        or getattr(snapshot, "job_id", None) != job.id
        or getattr(snapshot, "extraction_unit_id", None) != unit.id
    ):
        raise CandidateAggregationError(
            "candidate_scope_mismatch", "Job, Unit and Snapshot scope does not match"
        )
    if getattr(snapshot, "purged_at", None) is not None:
        raise CandidateAggregationError(
            "context_snapshot_purged", "purged Context Snapshot cannot stage Candidates"
        )
    if getattr(job, "normalization_rule_version", None) != "normalization_v1":
        raise CandidateAggregationError(
            "unsupported_normalization_rule", "Job normalization rule is not supported"
        )


async def _lock_entity_candidate(db, *, job: Any, candidate_key: str) -> GraphEntityCandidate:
    result = await db.execute(
        select(GraphEntityCandidate)
        .where(
            GraphEntityCandidate.job_id == job.id,
            GraphEntityCandidate.candidate_key == candidate_key,
        )
        .with_for_update()
    )
    candidate = result.scalars().first()
    if candidate is None:
        raise CandidateAggregationError(
            "candidate_upsert_failed", "Entity Candidate could not be reloaded"
        )
    return candidate


async def _upsert_entity_candidate(
    db,
    *,
    job: Any,
    payload: dict[str, Any],
    candidate_key: str,
    normalized_name: str,
) -> GraphEntityCandidate:
    candidate_id = uuid.uuid4()
    await db.execute(
        pg_insert(GraphEntityCandidate)
        .values(
            id=candidate_id,
            job_id=job.id,
            library_id=job.library_id,
            ontology_version_id=job.ontology_version_id,
            entity_type_key=payload["entity_type_key"],
            canonical_name=payload["name"].strip(),
            normalized_name=normalized_name,
            proposed_aliases=deepcopy(payload.get("aliases", [])),
            proposed_properties=deepcopy(payload.get("properties", {})),
            external_mapping_hints=deepcopy(payload.get("external_mapping_hints", [])),
            candidate_key=candidate_key,
            model_confidence=payload.get("confidence"),
            status="extracted",
        )
        .on_conflict_do_nothing(
            index_elements=["job_id", "candidate_key"]
        )
    )
    candidate = await _lock_entity_candidate(db, job=job, candidate_key=candidate_key)
    if (
        candidate.purged_at is not None
        or candidate.library_id != job.library_id
        or candidate.ontology_version_id != job.ontology_version_id
        or candidate.entity_type_key != payload["entity_type_key"]
        or candidate.normalized_name != normalized_name
    ):
        raise CandidateReplayError(
            "entity_candidate_replay_mismatch",
            "existing Entity Candidate does not match the computed key inputs",
        )
    return candidate


async def _upsert_entity_occurrence(
    db,
    *,
    job: Any,
    unit: Any,
    candidate: GraphEntityCandidate,
    payload: dict[str, Any],
) -> GraphEntityOccurrence:
    result = await db.execute(
        select(GraphEntityOccurrence)
        .where(
            GraphEntityOccurrence.extraction_unit_id == unit.id,
            GraphEntityOccurrence.local_ref == payload["local_id"],
        )
        .with_for_update()
    )
    existing = result.scalars().first()
    if existing is not None:
        if (
            existing.purged_at is not None
            or existing.job_id != job.id
            or existing.entity_candidate_id != candidate.id
            or existing.model_confidence != payload["confidence"]
            or canonical_graph_json_v1(existing.raw_payload)
            != canonical_graph_json_v1(payload)
        ):
            raise CandidateReplayError(
                "entity_occurrence_replay_mismatch",
                "existing Entity Occurrence does not match replayed payload",
            )
        return existing
    row = GraphEntityOccurrence(
        job_id=job.id,
        extraction_unit_id=unit.id,
        local_ref=payload["local_id"],
        entity_candidate_id=candidate.id,
        model_confidence=payload["confidence"],
        raw_payload=deepcopy(payload),
    )
    db.add(row)
    await db.flush()
    return row


async def _lock_relation_candidate(db, *, job: Any, candidate_key: str) -> GraphRelationCandidate:
    result = await db.execute(
        select(GraphRelationCandidate)
        .where(
            GraphRelationCandidate.job_id == job.id,
            GraphRelationCandidate.candidate_key == candidate_key,
        )
        .with_for_update()
    )
    candidate = result.scalars().first()
    if candidate is None:
        raise CandidateAggregationError(
            "candidate_upsert_failed", "Relation Candidate could not be reloaded"
        )
    return candidate


async def _upsert_relation_candidate(
    db,
    *,
    job: Any,
    payload: dict[str, Any],
    candidate_key: str,
    source_candidate: GraphEntityCandidate,
    target_candidate: GraphEntityCandidate,
) -> GraphRelationCandidate:
    await db.execute(
        pg_insert(GraphRelationCandidate)
        .values(
            id=uuid.uuid4(),
            job_id=job.id,
            library_id=job.library_id,
            ontology_version_id=job.ontology_version_id,
            source_candidate_id=source_candidate.id,
            relation_type_key=payload["relation_type_key"],
            target_candidate_id=target_candidate.id,
            proposed_properties=deepcopy(payload.get("properties", {})),
            candidate_key=candidate_key,
            evidence_support_mode="single_evidence",
            model_confidence=payload.get("confidence"),
            has_conflict=False,
            status="extracted",
        )
        .on_conflict_do_nothing(
            index_elements=["job_id", "candidate_key"]
        )
    )
    candidate = await _lock_relation_candidate(db, job=job, candidate_key=candidate_key)
    if (
        candidate.purged_at is not None
        or candidate.library_id != job.library_id
        or candidate.ontology_version_id != job.ontology_version_id
        or candidate.source_candidate_id != source_candidate.id
        or candidate.target_candidate_id != target_candidate.id
        or candidate.relation_type_key != payload["relation_type_key"]
        or canonical_graph_json_v1(candidate.proposed_properties)
        != canonical_graph_json_v1(payload.get("properties", {}))
    ):
        raise CandidateReplayError(
            "relation_candidate_replay_mismatch",
            "existing Relation Candidate does not match the computed key inputs",
        )
    return candidate


async def _upsert_relation_occurrence(
    db,
    *,
    job: Any,
    unit: Any,
    ordinal: int,
    candidate: GraphRelationCandidate,
    source_occurrence: GraphEntityOccurrence,
    target_occurrence: GraphEntityOccurrence,
    payload: dict[str, Any],
) -> GraphRelationOccurrence:
    result = await db.execute(
        select(GraphRelationOccurrence)
        .where(
            GraphRelationOccurrence.extraction_unit_id == unit.id,
            GraphRelationOccurrence.ordinal == ordinal,
        )
        .with_for_update()
    )
    existing = result.scalars().first()
    if existing is not None:
        if (
            existing.purged_at is not None
            or existing.job_id != job.id
            or existing.relation_candidate_id != candidate.id
            or existing.source_entity_occurrence_id != source_occurrence.id
            or existing.target_entity_occurrence_id != target_occurrence.id
            or existing.model_confidence != payload["confidence"]
            or canonical_graph_json_v1(existing.raw_payload)
            != canonical_graph_json_v1(payload)
        ):
            raise CandidateReplayError(
                "relation_occurrence_replay_mismatch",
                "existing Relation Occurrence does not match replayed payload",
            )
        return existing
    row = GraphRelationOccurrence(
        job_id=job.id,
        extraction_unit_id=unit.id,
        ordinal=ordinal,
        source_entity_occurrence_id=source_occurrence.id,
        target_entity_occurrence_id=target_occurrence.id,
        relation_candidate_id=candidate.id,
        model_confidence=payload["confidence"],
        raw_payload=deepcopy(payload),
    )
    db.add(row)
    await db.flush()
    return row


async def _handle_endpoint_replay_mismatch(
    db,
    *,
    job: Any,
    unit: Any,
    ordinal: int,
    source_occurrence: GraphEntityOccurrence,
    target_occurrence: GraphEntityOccurrence,
    proposed_candidate_key: str,
) -> tuple[GraphRelationOccurrence, GraphRelationCandidate] | None:
    result = await db.execute(
        select(GraphRelationOccurrence)
        .where(
            GraphRelationOccurrence.extraction_unit_id == unit.id,
            GraphRelationOccurrence.ordinal == ordinal,
        )
        .with_for_update()
    )
    occurrence = result.scalars().first()
    if occurrence is None:
        return None
    if occurrence.purged_at is not None:
        raise CandidateReplayError(
            "relation_occurrence_purged", "purged Relation Occurrence cannot be replayed"
        )
    candidate = await db.get(GraphRelationCandidate, occurrence.relation_candidate_id)
    if candidate is None or candidate.job_id != job.id:
        raise CandidateReplayError(
            "relation_occurrence_replay_mismatch",
            "existing Relation Occurrence Candidate is missing or out of scope",
        )
    endpoints_match = (
        occurrence.source_entity_occurrence_id == source_occurrence.id
        and occurrence.target_entity_occurrence_id == target_occurrence.id
    )
    if endpoints_match:
        return None

    old_source_occurrence = await db.get(
        GraphEntityOccurrence, occurrence.source_entity_occurrence_id
    )
    old_target_occurrence = await db.get(
        GraphEntityOccurrence, occurrence.target_entity_occurrence_id
    )
    if old_source_occurrence is None or old_target_occurrence is None:
        raise CandidateReplayError(
            "relation_occurrence_replay_mismatch",
            "existing Relation Occurrence endpoint audit row is missing",
        )
    endpoint_values = {
        "source_candidate_id": (
            old_source_occurrence.entity_candidate_id,
            source_occurrence.entity_candidate_id,
        ),
        "target_candidate_id": (
            old_target_occurrence.entity_candidate_id,
            target_occurrence.entity_candidate_id,
        ),
    }
    fields = [
        {
            "field": field,
            "value_hashes": sorted(
                {
                    canonical_graph_value_hash_v1(str(value))
                    for value in values
                }
            ),
        }
        for field, values in sorted(endpoint_values.items())
        if values[0] != values[1]
    ]
    if not fields:
        return None
    key = conflict_key_v1(
        job_id=job.id,
        conflict_type="endpoint_mismatch",
        entity_candidate_ids=[],
        relation_candidate_ids=[candidate.id],
        conflicting_fields=fields,
    )
    await db.execute(
        pg_insert(GraphExtractionConflict)
        .values(
            id=uuid.uuid4(),
            job_id=job.id,
            library_id=job.library_id,
            conflict_key=key,
            conflict_type="endpoint_mismatch",
            entity_candidate_ids=[],
            relation_candidate_ids=[str(candidate.id)],
            conflicting_fields=fields,
            status="open",
            details={
                "existing_candidate_key": candidate.candidate_key,
                "proposed_candidate_key": proposed_candidate_key,
                "existing_source_candidate_id": str(
                    old_source_occurrence.entity_candidate_id
                ),
                "proposed_source_candidate_id": str(
                    source_occurrence.entity_candidate_id
                ),
                "existing_target_candidate_id": str(
                    old_target_occurrence.entity_candidate_id
                ),
                "proposed_target_candidate_id": str(
                    target_occurrence.entity_candidate_id
                ),
            },
        )
        .on_conflict_do_nothing(index_elements=["job_id", "conflict_key"])
    )
    candidate.has_conflict = True
    candidate.status = "pending_review"
    candidate.review_reason = "endpoint_mismatch"
    candidate.validation_errors = [
        {"code": "endpoint_mismatch", "field": item["field"]} for item in fields
    ]
    return occurrence, candidate


def _evidence_quality(rows: list[Any]) -> float:
    if not rows or any(row.validation_status != "valid" for row in rows):
        return 0.0
    scores = [row.evidence_quality_score for row in rows]
    if any(score is None for score in scores):
        return 0.0
    return min(float(score) for score in scores)


async def _persist_property_conflicts(
    db,
    *,
    job: Any,
    candidate: GraphEntityCandidate,
    conflicts: tuple[PropertyConflictSpec, ...],
) -> int:
    result = await db.execute(
        select(GraphExtractionConflict)
        .where(
            GraphExtractionConflict.job_id == job.id,
            GraphExtractionConflict.conflict_type == "property_conflict",
        )
        .with_for_update()
    )
    candidate_id = str(candidate.id)
    existing_rows = [
        row for row in result.scalars().all() if candidate_id in row.entity_candidate_ids
    ]
    if not conflicts:
        for row in existing_rows:
            if row.purged_at is None and row.status == "open":
                row.status = "superseded"
        if candidate.review_reason == "property_conflict":
            candidate.status = "aggregated"
            candidate.review_reason = None
            candidate.validation_errors = []
        return 0
    fields = [
        {"field": conflict.field, "value_hashes": list(conflict.value_hashes)}
        for conflict in conflicts
    ]
    conflict_key = conflict_key_v1(
        job_id=job.id,
        conflict_type="property_conflict",
        entity_candidate_ids=[candidate.id],
        relation_candidate_ids=[],
        conflicting_fields=fields,
    )
    details = {
        "fields": [
            {
                "field": conflict.field,
                "values": [
                    {"value_hash": value_hash, "value": deepcopy(value)}
                    for value_hash, value in conflict.values_by_hash
                ],
            }
            for conflict in conflicts
        ]
    }
    existing = next((row for row in existing_rows if row.conflict_key == conflict_key), None)
    for row in existing_rows:
        if row.conflict_key != conflict_key and row.purged_at is None and row.status == "open":
            row.status = "superseded"
    if existing is None:
        await db.execute(
            pg_insert(GraphExtractionConflict)
            .values(
                id=uuid.uuid4(),
                job_id=job.id,
                library_id=job.library_id,
                conflict_key=conflict_key,
                conflict_type="property_conflict",
                entity_candidate_ids=[candidate_id],
                relation_candidate_ids=[],
                conflicting_fields=fields,
                status="open",
                details=details,
            )
            .on_conflict_do_nothing(index_elements=["job_id", "conflict_key"])
        )
    elif existing.purged_at is None:
        if (
            existing.library_id != job.library_id
            or existing.entity_candidate_ids != [candidate_id]
            or existing.relation_candidate_ids != []
            or existing.conflicting_fields != fields
        ):
            raise CandidateReplayError(
                "property_conflict_identity_mismatch",
                "existing Property Conflict does not match its stable identity",
            )
        existing.status = "open"
        existing.details = details
    candidate.status = "pending_review"
    candidate.review_reason = "property_conflict"
    candidate.validation_errors = [
        {"code": "property_conflict", "field": conflict.field}
        for conflict in conflicts
    ]
    return 1


async def _recompute_entity_candidate(
    db,
    *,
    job: Any,
    candidate: GraphEntityCandidate,
) -> int:
    result = await db.execute(
        select(GraphEntityOccurrence, GraphExtractionUnit.ordinal)
        .join(
            GraphExtractionUnit,
            GraphExtractionUnit.id == GraphEntityOccurrence.extraction_unit_id,
        )
        .where(
            GraphEntityOccurrence.entity_candidate_id == candidate.id,
            GraphEntityOccurrence.purged_at.is_(None),
        )
        .order_by(
            GraphExtractionUnit.ordinal,
            GraphEntityOccurrence.local_ref,
            GraphEntityOccurrence.id,
        )
    )
    occurrences = [
        EntityAggregateOccurrence(
            unit_ordinal=unit_ordinal,
            local_ref=row.local_ref,
            model_confidence=row.model_confidence,
            raw_payload=row.raw_payload,
        )
        for row, unit_ordinal in result.all()
    ]
    aggregate = aggregate_entity_occurrence_payloads(occurrences)
    if (
        aggregate.normalized_name != candidate.normalized_name
        or entity_candidate_key_v1(
            ontology_version_id=candidate.ontology_version_id,
            entity_type_key=candidate.entity_type_key,
            normalized_name=aggregate.normalized_name,
        )
        != candidate.candidate_key
    ):
        raise CandidateAggregationError(
            "entity_candidate_key_mismatch", "Entity aggregate does not match Candidate key"
        )
    candidate.canonical_name = aggregate.canonical_name
    candidate.proposed_aliases = aggregate.proposed_aliases
    candidate.proposed_properties = aggregate.proposed_properties
    candidate.external_mapping_hints = aggregate.external_mapping_hints  # type: ignore[assignment]
    candidate.model_confidence = aggregate.model_confidence

    evidence_result = await db.execute(
        select(GraphEntityCandidateEvidence).where(
            GraphEntityCandidateEvidence.candidate_id == candidate.id,
            GraphEntityCandidateEvidence.purged_at.is_(None),
        )
    )
    candidate.evidence_quality_score = _evidence_quality(
        list(evidence_result.scalars().all())
    )
    return await _persist_property_conflicts(
        db,
        job=job,
        candidate=candidate,
        conflicts=aggregate.property_conflicts,
    )


async def _recompute_relation_candidate(
    db,
    *,
    candidate: GraphRelationCandidate,
) -> None:
    result = await db.execute(
        select(GraphRelationOccurrence, GraphExtractionUnit.ordinal)
        .join(
            GraphExtractionUnit,
            GraphExtractionUnit.id == GraphRelationOccurrence.extraction_unit_id,
        )
        .where(
            GraphRelationOccurrence.relation_candidate_id == candidate.id,
            GraphRelationOccurrence.purged_at.is_(None),
        )
        .order_by(
            GraphExtractionUnit.ordinal,
            GraphRelationOccurrence.ordinal,
            GraphRelationOccurrence.id,
        )
    )
    occurrences = [
        RelationAggregateOccurrence(
            unit_ordinal=unit_ordinal,
            ordinal=row.ordinal,
            model_confidence=row.model_confidence,
            raw_payload=row.raw_payload,
        )
        for row, unit_ordinal in result.all()
    ]
    aggregate = aggregate_relation_occurrence_payloads(occurrences)
    candidate.proposed_properties = aggregate.proposed_properties
    candidate.model_confidence = aggregate.model_confidence

    evidence_result = await db.execute(
        select(GraphRelationCandidateEvidence).where(
            GraphRelationCandidateEvidence.candidate_id == candidate.id,
            GraphRelationCandidateEvidence.purged_at.is_(None),
        )
    )
    evidence_rows = list(evidence_result.scalars().all())
    candidate.evidence_quality_score = _evidence_quality(evidence_rows)
    valid_evidence_ids = {
        row.resolved_evidence_id
        for row in evidence_rows
        if row.validation_status == "valid" and row.resolved_evidence_id is not None
    }
    candidate.evidence_support_mode = (
        "evidence_group" if len(valid_evidence_ids) >= 2 else "single_evidence"
    )
    if candidate.status in {"extracted", "aggregated"}:
        candidate.status = "aggregated"


async def stage_unit_candidate_occurrences(
    db,
    *,
    job: Any,
    unit: Any,
    snapshot: Any,
    payload: GraphExtractionPayload,
) -> UnitCandidateStageResult:
    _validate_stage_scope(job=job, unit=unit, snapshot=snapshot)
    if not isinstance(payload, GraphExtractionPayload):
        raise TypeError("payload must be GraphExtractionPayload")

    entity_entries: list[tuple[str, dict[str, Any]]] = []
    for entity in payload.entities:
        raw = entity.model_dump(mode="json")
        normalized_name = normalize_graph_name_v1(entity.name)
        key = entity_candidate_key_v1(
            ontology_version_id=job.ontology_version_id,
            entity_type_key=entity.entity_type_key,
            normalized_name=normalized_name,
        )
        entity_entries.append((key, raw))

    candidates_by_local: dict[str, GraphEntityCandidate] = {}
    occurrences_by_local: dict[str, GraphEntityOccurrence] = {}
    entity_candidates: dict[uuid.UUID, GraphEntityCandidate] = {}
    entity_occurrences: list[GraphEntityOccurrence] = []
    for candidate_key, raw in sorted(
        entity_entries, key=lambda item: (item[0], item[1]["local_id"])
    ):
        normalized_name = normalize_graph_name_v1(raw["name"])
        candidate = await _upsert_entity_candidate(
            db,
            job=job,
            payload=raw,
            candidate_key=candidate_key,
            normalized_name=normalized_name,
        )
        occurrence = await _upsert_entity_occurrence(
            db, job=job, unit=unit, candidate=candidate, payload=raw
        )
        candidates_by_local[raw["local_id"]] = candidate
        occurrences_by_local[raw["local_id"]] = occurrence
        entity_candidates[candidate.id] = candidate
        entity_occurrences.append(occurrence)
        for claim in raw["evidence"]:
            await create_entity_candidate_evidence(
                db,
                job=job,
                unit=unit,
                candidate=candidate,
                snapshot=snapshot,
                context_ref=claim["context_ref"],
                quote=claim["quote"],
            )

    property_conflict_count = 0
    for candidate in sorted(entity_candidates.values(), key=lambda item: item.candidate_key):
        property_conflict_count += await _recompute_entity_candidate(
            db, job=job, candidate=candidate
        )

    relation_entries: list[
        tuple[str, int, dict[str, Any], GraphEntityCandidate, GraphEntityCandidate]
    ] = []
    for ordinal, relation in enumerate(payload.relations):
        raw = relation.model_dump(mode="json")
        source_candidate = candidates_by_local[relation.source_local_id]
        target_candidate = candidates_by_local[relation.target_local_id]
        key = relation_candidate_key_v1(
            ontology_version_id=job.ontology_version_id,
            source_candidate_key=source_candidate.candidate_key,
            relation_type_key=relation.relation_type_key,
            target_candidate_key=target_candidate.candidate_key,
            properties=relation.properties,
        )
        relation_entries.append((key, ordinal, raw, source_candidate, target_candidate))

    relation_candidates: dict[uuid.UUID, GraphRelationCandidate] = {}
    relation_occurrences: list[GraphRelationOccurrence] = []
    for candidate_key, ordinal, raw, source_candidate, target_candidate in sorted(
        relation_entries, key=lambda item: (item[0], item[1])
    ):
        source_occurrence = occurrences_by_local[raw["source_local_id"]]
        target_occurrence = occurrences_by_local[raw["target_local_id"]]
        mismatch = await _handle_endpoint_replay_mismatch(
            db,
            job=job,
            unit=unit,
            ordinal=ordinal,
            source_occurrence=source_occurrence,
            target_occurrence=target_occurrence,
            proposed_candidate_key=candidate_key,
        )
        if mismatch is not None:
            occurrence, existing_candidate = mismatch
            relation_candidates[existing_candidate.id] = existing_candidate
            relation_occurrences.append(occurrence)
            continue
        candidate = await _upsert_relation_candidate(
            db,
            job=job,
            payload=raw,
            candidate_key=candidate_key,
            source_candidate=source_candidate,
            target_candidate=target_candidate,
        )
        occurrence = await _upsert_relation_occurrence(
            db,
            job=job,
            unit=unit,
            ordinal=ordinal,
            candidate=candidate,
            source_occurrence=source_occurrence,
            target_occurrence=target_occurrence,
            payload=raw,
        )
        relation_candidates[candidate.id] = candidate
        relation_occurrences.append(occurrence)
        for claim in raw["evidence"]:
            await create_relation_candidate_evidence(
                db,
                job=job,
                unit=unit,
                candidate=candidate,
                snapshot=snapshot,
                context_ref=claim["context_ref"],
                quote=claim["quote"],
            )

    for candidate in sorted(
        relation_candidates.values(), key=lambda item: item.candidate_key
    ):
        if candidate.review_reason != "endpoint_mismatch":
            await _recompute_relation_candidate(db, candidate=candidate)

    await db.flush()
    return UnitCandidateStageResult(
        entity_candidate_ids=tuple(sorted(entity_candidates)),
        relation_candidate_ids=tuple(sorted(relation_candidates)),
        entity_occurrence_ids=tuple(sorted(row.id for row in entity_occurrences)),
        relation_occurrence_ids=tuple(sorted(row.id for row in relation_occurrences)),
    )


async def recompute_job_candidate_aggregates(
    db,
    *,
    job: Any,
) -> JobCandidateAggregateResult:
    entity_result = await db.execute(
        select(GraphEntityCandidate)
        .where(
            GraphEntityCandidate.job_id == job.id,
            GraphEntityCandidate.purged_at.is_(None),
            GraphEntityCandidate.status.notin_({"materialized", "superseded"}),
        )
        .order_by(GraphEntityCandidate.candidate_key)
        .with_for_update()
    )
    entity_candidates = list(entity_result.scalars().all())
    property_conflict_count = 0
    for candidate in entity_candidates:
        property_conflict_count += await _recompute_entity_candidate(
            db, job=job, candidate=candidate
        )

    relation_result = await db.execute(
        select(GraphRelationCandidate)
        .where(
            GraphRelationCandidate.job_id == job.id,
            GraphRelationCandidate.purged_at.is_(None),
            GraphRelationCandidate.status.notin_({"materialized", "superseded"}),
        )
        .order_by(GraphRelationCandidate.candidate_key)
        .with_for_update()
    )
    relation_candidates = list(relation_result.scalars().all())
    for candidate in relation_candidates:
        await _recompute_relation_candidate(db, candidate=candidate)
    await db.flush()
    return JobCandidateAggregateResult(
        entity_candidate_count=len(entity_candidates),
        relation_candidate_count=len(relation_candidates),
        property_conflict_count=property_conflict_count,
    )
