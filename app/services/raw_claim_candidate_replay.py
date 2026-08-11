"""Offline audit of the existing graph candidate boundary.

This module is deliberately diagnostic.  It calls the existing extraction
parsers, candidate-key helper, and relation aggregation function, but it does
not import a session, worker, ORM write path, or publication code.  Its output
describes what the current short-lived candidate pipeline can retain before a
future shadow persistence design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

from app.schemas.raw_claim import RawClaimV1
from app.services.graph_candidate_aggregation import (
    RelationAggregateOccurrence,
    aggregate_relation_occurrence_payloads,
    relation_candidate_key_v1,
)
from app.services.graph_extraction_batch_eval import (
    BatchExtractionParseError,
    parse_batched_graph_extraction_output,
)
from app.services.graph_extraction_parser import GraphExtractionParseError, parse_graph_extraction_output


ReplayDisposition = Literal["preserved", "transformed", "unavailable", "rejected", "removed"]

REPLAY_FIELDS = (
    "raw_predicate",
    "canonical_relation_type_key",
    "source_local_id",
    "target_local_id",
    "source_surface",
    "target_surface",
    "surface_direction",
    "negation",
    "modality",
    "qualifiers",
    "valid_time",
    "effective_time",
    "evidence_refs",
    "scope",
    "provenance",
)


@dataclass(frozen=True, slots=True)
class ReplayStage:
    name: str
    input_relation_count: int
    output_relation_count: int
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeProjection:
    """The payload fields cleared by the existing candidate purge SQL."""

    occurrence_raw_payload: None
    candidate_proposed_properties: None
    candidate_evidence_quote: None


@dataclass(frozen=True, slots=True)
class CandidateBoundaryReplay:
    batch_key: str
    claim_content_fingerprint: str
    occurrence_payload: dict[str, Any] | None
    candidate_key: str | None
    candidate_status: str
    candidate_rejection_reason: str | None
    aggregate_properties: dict[str, Any] | None
    stages: tuple[ReplayStage, ...]
    field_matrix: dict[str, dict[str, ReplayDisposition]]
    purge: PurgeProjection
    endpoint_validation: Literal["matched", "mismatch", "unavailable"]


def _stage_matrix(
    *,
    relation_available: bool,
    property_keys: set[str],
    candidate_status: str,
) -> dict[str, dict[str, ReplayDisposition]]:
    if not relation_available:
        matrix = {
            field: {
                "parser": "rejected",
                "occurrence": "unavailable",
                "candidate": "unavailable",
                "aggregate": "unavailable",
                "purge": "removed",
            }
            for field in REPLAY_FIELDS
        }
        matrix["raw_predicate"] = {
            "parser": "unavailable",
            "occurrence": "unavailable",
            "candidate": "unavailable",
            "aggregate": "unavailable",
            "purge": "removed",
        }
        return matrix

    opaque_fields = {
        "surface_direction": "surface_direction",
        "negation": "negation",
        "modality": "modality",
        "qualifiers": "qualifiers",
        "valid_time": "valid_time",
        "effective_time": "effective_time",
        "evidence_refs": "evidence_refs",
    }
    matrix: dict[str, dict[str, ReplayDisposition]] = {}
    for field in REPLAY_FIELDS:
        if field in opaque_fields and opaque_fields[field] in property_keys:
            candidate_value: ReplayDisposition = (
                "preserved" if candidate_status != "not_persisted" else "unavailable"
            )
            matrix[field] = {
                "parser": "transformed",
                "occurrence": "preserved",
                "candidate": candidate_value,
                "aggregate": candidate_value,
                "purge": "removed",
            }
        else:
            matrix[field] = {
                "parser": "unavailable",
                "occurrence": "unavailable",
                "candidate": "unavailable",
                "aggregate": "unavailable",
                "purge": "removed",
            }
    matrix["raw_predicate"] = {
        # GraphExtractionPayload has no surface raw-predicate field. The
        # canonical key cannot be used to reconstruct it.
        "parser": "unavailable",
        "occurrence": "unavailable",
        "candidate": "unavailable",
        "aggregate": "unavailable",
        "purge": "removed",
    }
    matrix["canonical_relation_type_key"] = {
        "parser": "preserved",
        "occurrence": "preserved",
        "candidate": "transformed",
        "aggregate": "unavailable",
        "purge": "removed",
    }
    for field in ("source_local_id", "target_local_id"):
        matrix[field] = {
            "parser": "preserved",
            "occurrence": "preserved",
            "candidate": "transformed",
            "aggregate": "unavailable",
            "purge": "removed",
        }
    for field in ("source_surface", "target_surface"):
        matrix[field] = {
            "parser": "preserved",
            "occurrence": "unavailable",
            "candidate": "unavailable",
            "aggregate": "unavailable",
            "purge": "removed",
        }
    matrix["scope"] = {
        "parser": "unavailable",
        "occurrence": "transformed",
        "candidate": "transformed",
        "aggregate": "unavailable",
        "purge": "removed",
    }
    matrix["provenance"] = {
        "parser": "unavailable",
        "occurrence": "unavailable",
        "candidate": "unavailable",
        "aggregate": "unavailable",
        "purge": "removed",
    }
    return matrix


def replay_raw_relation_candidate_boundary(
    raw_content: str,
    *,
    claim: RawClaimV1,
    ontology_version_id: UUID,
    batch_key: str = "u0",
    source_candidate_key: str = "source-candidate",
    target_candidate_key: str = "target-candidate",
    relation_type_known: bool = True,
    endpoint_types_known: bool = True,
    allowed_entity_type_keys: set[str] | None = None,
    allowed_relation_type_keys: set[str] | None = None,
) -> CandidateBoundaryReplay:
    """Replay one relation through the DB-free portion of the current boundary."""

    decoded = json.loads(raw_content)
    if not isinstance(decoded, dict):
        raise ValueError("raw extraction fixture must be an object")
    raw_relations = decoded.get("relations", [])
    if not isinstance(raw_relations, list):
        raise ValueError("raw extraction fixture relations must be an array")
    raw_relation_count = len(raw_relations)
    stages: list[ReplayStage] = [
        ReplayStage("raw_json", raw_relation_count, raw_relation_count, "accepted")
    ]

    try:
        parsed = parse_graph_extraction_output(raw_content)
    except GraphExtractionParseError as exc:
        stages.append(ReplayStage("parser", raw_relation_count, 0, "rejected", exc.parse_status))
    else:
        stages.append(ReplayStage("parser", raw_relation_count, len(parsed.relations), "accepted"))

    batch_content = json.dumps(
        {"batches": [{"batch_key": batch_key, **decoded}]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        parsed_batches = parse_batched_graph_extraction_output(
            batch_content,
            expected_keys=(batch_key,),
            allowed_entity_type_keys=allowed_entity_type_keys,
            allowed_relation_type_keys=allowed_relation_type_keys,
        )
        batched = parsed_batches[batch_key]
    except BatchExtractionParseError as exc:
        stages.append(ReplayStage("batch_parser", raw_relation_count, 0, "rejected", str(exc)))
        return CandidateBoundaryReplay(
            batch_key=batch_key,
            claim_content_fingerprint=claim.content_scoped_claim_fingerprint or "",
            occurrence_payload=None,
            candidate_key=None,
            candidate_status="not_persisted",
            candidate_rejection_reason="batch_parser_rejected",
            aggregate_properties=None,
            stages=tuple(stages),
            field_matrix=_stage_matrix(
                relation_available=False,
                property_keys=set(),
                candidate_status="not_persisted",
            ),
            purge=PurgeProjection(None, None, None),
            endpoint_validation="unavailable",
        )
    stages.append(ReplayStage("batch_parser", raw_relation_count, len(batched.relations), "accepted"))
    if not batched.relations:
        return CandidateBoundaryReplay(
            batch_key=batch_key,
            claim_content_fingerprint=claim.content_scoped_claim_fingerprint or "",
            occurrence_payload=None,
            candidate_key=None,
            candidate_status="not_persisted",
            candidate_rejection_reason="relation_not_emitted_by_batch_parser",
            aggregate_properties=None,
            stages=tuple(stages),
            field_matrix=_stage_matrix(
                relation_available=False,
                property_keys=set(),
                candidate_status="not_persisted",
            ),
            purge=PurgeProjection(None, None, None),
            endpoint_validation="unavailable",
        )

    relation = batched.relations[0]
    endpoint_matches = (
        relation.source_local_id == claim.source_mention.local_id
        and relation.target_local_id == claim.target_mention.local_id
    )
    if not endpoint_matches:
        stages.append(
            ReplayStage(
                "claim_endpoint_validation",
                1,
                0,
                "rejected",
                "endpoint_local_id_mismatch",
            )
        )
        stages.append(ReplayStage("candidate_purge", 0, 0, "purged"))
        return CandidateBoundaryReplay(
            batch_key=batch_key,
            claim_content_fingerprint=claim.content_scoped_claim_fingerprint or "",
            occurrence_payload=None,
            candidate_key=None,
            candidate_status="rejected",
            candidate_rejection_reason="endpoint_local_id_mismatch",
            aggregate_properties=None,
            stages=tuple(stages),
            field_matrix=_stage_matrix(
                relation_available=True,
                property_keys=set(relation.properties),
                candidate_status="not_persisted",
            ),
            purge=PurgeProjection(None, None, None),
            endpoint_validation="mismatch",
        )
    occurrence_payload = relation.model_dump(mode="json")
    stages.append(ReplayStage("occurrence_raw_payload", 1, 1, "accepted"))
    candidate_key = relation_candidate_key_v1(
        ontology_version_id=ontology_version_id,
        source_candidate_key=source_candidate_key,
        relation_type_key=relation.relation_type_key,
        target_candidate_key=target_candidate_key,
        properties=relation.properties,
    )
    aggregate = aggregate_relation_occurrence_payloads(
        [
            RelationAggregateOccurrence(
                unit_ordinal=0,
                ordinal=0,
                model_confidence=relation.confidence,
                raw_payload=occurrence_payload,
            )
        ]
    )
    stages.append(ReplayStage("candidate_aggregation", 1, 1, "accepted"))
    candidate_status = "aggregated" if relation_type_known and endpoint_types_known else "rejected"
    rejection_reason = None if candidate_status == "aggregated" else "schema_extension_candidate"
    stages.append(ReplayStage("candidate_validation", 1, 1, candidate_status, rejection_reason))
    stages.append(ReplayStage("candidate_purge", 1, 0, "purged"))
    return CandidateBoundaryReplay(
        batch_key=batch_key,
        claim_content_fingerprint=claim.content_scoped_claim_fingerprint or "",
        occurrence_payload=occurrence_payload,
        candidate_key=candidate_key,
        candidate_status=candidate_status,
        candidate_rejection_reason=rejection_reason,
        aggregate_properties=aggregate.proposed_properties,
        stages=tuple(stages),
        field_matrix=_stage_matrix(
            relation_available=True,
            property_keys=set(relation.properties),
            candidate_status=candidate_status,
        ),
        purge=PurgeProjection(None, None, None),
        endpoint_validation="matched",
    )


def measure_replay_latency(
    raw_content: str,
    *,
    claim: RawClaimV1,
    ontology_version_id: UUID,
    samples: int = 20,
) -> dict[str, float | int]:
    if isinstance(samples, bool) or samples < 1 or samples > 1000:
        raise ValueError("samples must be between 1 and 1000")
    durations: list[float] = []
    for _ in range(samples):
        started = perf_counter()
        replay_raw_relation_candidate_boundary(
            raw_content,
            claim=claim,
            ontology_version_id=ontology_version_id,
        )
        durations.append((perf_counter() - started) * 1000)
    durations.sort()
    def percentile(value: float) -> float:
        return durations[min(len(durations) - 1, ceil(value * len(durations)) - 1)]

    return {
        "samples": samples,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
    }
