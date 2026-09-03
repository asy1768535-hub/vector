from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text

from app.models.fact_foundation import (
    FactAssertion,
    FactResolutionDecision,
    LogicalFact,
    StablePredicateIdentity,
    StablePredicateMapping,
)
from app.services.stable_predicate_resolution_policy import (
    StablePredicateResolutionPolicy,
    is_predicate_ready_for_fact_resolution,
    parse_stable_predicate_resolution_policy,
)


class GraphRelationFactResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LogicalFactPlan:
    identity_fingerprint: str
    object_kind: str | None
    object_canonical_entity_id: uuid.UUID | None
    object_value: dict[str, Any] | None
    identity_qualifiers: dict[str, Any]
    temporal_identity_key: str | None


@dataclass(frozen=True, slots=True)
class FactAssertionPlan:
    assertion_fingerprint: str
    asserted_object_kind: str | None
    asserted_object_canonical_entity_id: uuid.UUID | None
    asserted_value: dict[str, Any] | None
    polarity: str
    modality: str
    qualifiers: dict[str, Any]
    valid_time: dict[str, Any] | None
    effective_time: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class GraphRelationFactPlan:
    status: str
    reason_code: str | None
    source_fingerprint: str
    subject_fingerprint: str
    decision_fingerprint: str
    source_snapshot: dict[str, Any]
    candidate_snapshot: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    logical_fact: LogicalFactPlan | None
    assertion: FactAssertionPlan | None
    predicate: StablePredicateIdentity | None


@dataclass(frozen=True, slots=True)
class GraphRelationFactResolutionResult:
    outcome: str
    decision: FactResolutionDecision
    logical_fact: LogicalFact | None
    assertion: FactAssertion | None


@dataclass(frozen=True, slots=True)
class GraphRelationFactPreflight:
    plan: GraphRelationFactPlan
    decision: FactResolutionDecision | None
    logical_fact: LogicalFact | None
    assertion: FactAssertion | None
    supersedes: FactResolutionDecision | None

    @property
    def is_resolved(self) -> bool:
        return self.plan.status == "resolved"


@dataclass(frozen=True, slots=True)
class GraphRelationFactMaterialization:
    logical_fact: LogicalFact
    assertion: FactAssertion
    logical_outcome: str
    assertion_outcome: str


@dataclass(frozen=True, slots=True)
class FactResolutionSource:
    source_kind: str
    raw_claim_id: uuid.UUID | None = None
    method: str = "p2_graph_relation_candidate_v1"
    resolver_version: str = "p2_3_fact_resolution_v1"

    @classmethod
    def graph_relation_candidate(cls) -> FactResolutionSource:
        return cls("graph_relation_candidate")

    @classmethod
    def raw_claim(cls, raw_claim_id: uuid.UUID) -> FactResolutionSource:
        return cls(
            "raw_claim",
            raw_claim_id=raw_claim_id,
            method="p2_raw_claim_projection_v1",
            resolver_version="p2_5_raw_claim_fact_resolution_v1",
        )


_FINGERPRINT_VERSION = "p2_graph_relation_fact_v1"
_LOCK_PREFIX = "vector-kb:fact-resolution-subject:"


def _unresolved_outcome(status: str) -> str:
    return "REJECT" if status == "rejected" else "PENDING"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def with_fact_resolution_source(
    plan: GraphRelationFactPlan,
    *,
    source: FactResolutionSource,
) -> GraphRelationFactPlan:
    """Namespace Decision identity without changing the source Assertion identity."""
    if source.source_kind == "graph_relation_candidate":
        return plan
    return replace(
        plan,
        decision_fingerprint=_fingerprint(
            {
                "base_decision_fingerprint": plan.decision_fingerprint,
                "schema_version": "fact_resolution_decision_source_v1",
                "source_kind": source.source_kind,
            }
        ),
    )


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("value is not numeric")
    text_value = str(value).strip()
    if not text_value:
        raise ValueError("numeric value is empty")
    try:
        decimal = Decimal(text_value)
    except InvalidOperation as exc:
        raise ValueError("value is not numeric") from exc
    if not decimal.is_finite():
        raise ValueError("numeric value must be finite")
    return decimal


def _normalized_property(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("property float must be finite")
        return {"kind": "decimal", "value": _decimal_text(_as_decimal(value))}
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("property string is empty")
        return normalized
    if isinstance(value, Mapping):
        return {str(key): _normalized_property(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized_property(item) for item in value]
    raise ValueError("property value is not JSON-compatible")


def _typed_qualifier(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, int):
        return {"kind": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"kind": "decimal", "value": _decimal_text(_as_decimal(value))}
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("qualifier string is empty")
        return {"kind": "string", "value": normalized}
    return {"kind": "json", "value": _normalized_property(value)}


def _policy_value(policy: Any, properties: Mapping[str, Any]) -> str | None:
    if policy.source == "none":
        return None
    if policy.source == "fixed":
        return policy.value
    value = properties.get(policy.property_key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("policy property value is missing")
    return unicodedata.normalize("NFKC", value).strip()


def _typed_literal(value: Any, policy: Any, properties: Mapping[str, Any]) -> dict[str, Any]:
    literal_type = policy.literal_type
    unit = _policy_value(policy.unit_policy, properties)
    currency = _policy_value(policy.currency_policy, properties)
    if literal_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("boolean literal is invalid")
        return {"kind": "boolean", "value": value}
    if literal_type == "date":
        if not isinstance(value, str):
            raise ValueError("date literal is invalid")
        try:
            normalized = date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise ValueError("date literal is invalid") from exc
        return {"kind": "date", "value": normalized}
    if literal_type == "string":
        if not isinstance(value, str):
            raise ValueError("string literal is invalid")
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("string literal is empty")
        return {"kind": "string", "value": normalized}

    numeric_value = value
    if isinstance(value, str):
        numeric_value = unicodedata.normalize("NFKC", value).strip()
    if literal_type == "ratio" and isinstance(numeric_value, str) and numeric_value.endswith("%"):
        numeric_value = _as_decimal(numeric_value[:-1]) / Decimal("100")
    if literal_type == "money" and isinstance(numeric_value, str):
        if numeric_value.endswith("亿元"):
            numeric_value = _as_decimal(numeric_value.removesuffix("亿元")) * Decimal("100000000")
        elif currency and numeric_value.endswith(f" {currency}"):
            numeric_value = numeric_value.removesuffix(f" {currency}")
    decimal = _as_decimal(numeric_value)
    if literal_type == "integer" and decimal != decimal.to_integral_value():
        raise ValueError("integer literal is not integral")

    result: dict[str, Any] = {
        "kind": "integer" if literal_type == "integer" else "decimal",
        "value": _decimal_text(decimal),
    }
    if unit is not None:
        result["unit"] = unit
    if currency is not None:
        result["currency"] = currency
    return result


def _normalized_time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("time value is invalid")
    raw = value.strip()
    if not raw:
        raise ValueError("time value is empty")
    try:
        if "T" in raw or " " in raw:
            normalized = datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        else:
            normalized = date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError("time value is invalid") from exc
    return normalized


def _time_value(policy: Any, properties: Mapping[str, Any]) -> dict[str, Any] | None:
    if policy.source == "none":
        return None
    if policy.source == "instant_property":
        return {"kind": "instant", "value": _normalized_time(properties.get(policy.property_key))}
    return {
        "from": _normalized_time(properties.get(policy.from_property_key)),
        "kind": "range",
        "to": _normalized_time(properties.get(policy.to_property_key)),
    }


def _value_from_mapping(policy: Any, properties: Mapping[str, Any]) -> str:
    if policy.source == "fixed":
        return policy.value
    raw = properties.get(policy.property_key)
    if not isinstance(raw, str):
        raise ValueError("policy value is missing")
    mapped = policy.mapping.get(raw)
    if mapped is None:
        raise ValueError("policy value is not mapped")
    return mapped


def _policy_property_keys(policy: StablePredicateResolutionPolicy) -> set[str]:
    keys = {
        *policy.qualifier_policy.identity_bearing,
        *policy.qualifier_policy.assertion_bearing,
        *policy.qualifier_policy.evidence_only,
    }
    if policy.object_policy.property_key is not None:
        keys.add(policy.object_policy.property_key)
    if policy.measurement_policy is not None:
        keys.add(policy.measurement_policy.value_property_key)
        for item in (policy.measurement_policy.unit_policy, policy.measurement_policy.currency_policy):
            if item.property_key is not None:
                keys.add(item.property_key)
    for item in (policy.object_policy.unit_policy, policy.object_policy.currency_policy):
        if item is not None and item.property_key is not None:
            keys.add(item.property_key)
    for item in (policy.polarity_policy, policy.modality_policy):
        if item.property_key is not None:
            keys.add(item.property_key)
    for item in (policy.valid_time_policy, policy.effective_time_policy):
        for key in (item.property_key, item.from_property_key, item.to_property_key):
            if key is not None:
                keys.add(key)
    if (
        policy.event_temporal_identity_policy is not None
        and policy.event_temporal_identity_policy.property_key is not None
    ):
        keys.add(policy.event_temporal_identity_policy.property_key)
    return keys


def _source_occurrence_snapshot(library_id: uuid.UUID, evidence: Any) -> dict[str, Any]:
    required = (
        "resolved_document_id",
        "resolved_document_revision_id",
        "resolved_evidence_id",
        "resolved_chunk_id",
        "resolved_source_span",
    )
    if any(getattr(evidence, field, None) is None for field in required):
        raise ValueError("resolved evidence lineage is incomplete")
    source_span = getattr(evidence, "resolved_source_span")
    if not isinstance(source_span, Mapping):
        raise ValueError("resolved evidence span is invalid")
    return {
        "document_id": str(evidence.resolved_document_id),
        "document_revision_id": str(evidence.resolved_document_revision_id),
        "evidence_id": str(evidence.resolved_evidence_id),
        "chunk_id": str(evidence.resolved_chunk_id),
        "block_id": (
            str(evidence.resolved_block_id)
            if getattr(evidence, "resolved_block_id", None) is not None
            else None
        ),
        "source_span": _normalized_property(source_span),
        "library_id": str(library_id),
        "schema_version": "graph_relation_source_occurrence_v1",
    }


def source_occurrence_fingerprint_v1(library_id: uuid.UUID, evidence: Any) -> str:
    return _fingerprint(_source_occurrence_snapshot(library_id, evidence))


def _source_occurrence_group_snapshot(
    library_id: uuid.UUID,
    evidence_rows: list[Any],
) -> dict[str, Any]:
    occurrences = sorted(
        (_source_occurrence_snapshot(library_id, row) for row in evidence_rows),
        key=_canonical_json,
    )
    if not occurrences:
        raise ValueError("resolved evidence lineage is incomplete")
    return {
        "library_id": str(library_id),
        "occurrences": occurrences,
        "schema_version": "graph_relation_source_occurrence_group_v1",
    }


def resolution_subject_fingerprint_v1(
    library_id: uuid.UUID,
    *,
    source_fingerprint: str,
    candidate: Any,
    source_entity: Any,
    target_entity: Any,
) -> str:
    return _fingerprint(
        {
            "library_id": str(library_id),
            "observed_relation_type_key": str(candidate.relation_type_key),
            "schema_version": "fact_resolution_subject_v2",
            "source_canonical_entity_id": (
                str(source_entity.canonical_entity_id)
                if getattr(source_entity, "canonical_entity_id", None) is not None
                else None
            ),
            "source_fingerprint": source_fingerprint,
            "target_canonical_entity_id": (
                str(target_entity.canonical_entity_id)
                if getattr(target_entity, "canonical_entity_id", None) is not None
                else None
            ),
        }
    )


def _candidate_snapshot(candidate: Any, properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "properties": _normalized_property(properties),
        "relation_type_key": candidate.relation_type_key,
        "schema_version": "graph_relation_candidate_fact_input_v1",
    }


def _predicate_snapshot(predicate: StablePredicateIdentity) -> dict[str, Any]:
    return {
        "contract_version": predicate.contract_version,
        "identity_policy_version": predicate.identity_policy_version,
        "key": predicate.key,
        "namespace": predicate.namespace,
        "predicate_id": str(predicate.id),
        "temporal_class": predicate.temporal_class,
    }


def _pending_plan(
    *,
    library_id: uuid.UUID,
    candidate: Any,
    evidence: Any,
    reason_code: str,
    status: str = "pending",
    predicate: StablePredicateIdentity | None = None,
    subject_canonical_entity_id: uuid.UUID | None = None,
    source_snapshot: Mapping[str, Any] | None = None,
) -> GraphRelationFactPlan:
    source_snapshot = dict(source_snapshot or _source_occurrence_snapshot(library_id, evidence))
    source_fingerprint = _fingerprint(source_snapshot)
    properties = candidate.proposed_properties if isinstance(candidate.proposed_properties, Mapping) else {}
    candidate_snapshot = _candidate_snapshot(candidate, properties)
    subject_fingerprint = _fingerprint(
        {
            "library_id": str(library_id),
            "schema_version": "fact_resolution_subject_v1",
            "subject_canonical_entity_id": (
                str(subject_canonical_entity_id) if subject_canonical_entity_id is not None else None
            ),
        }
    )
    decision_fingerprint = _fingerprint(
        {
            "candidate": candidate_snapshot,
            "predicate_id": str(predicate.id) if predicate is not None else None,
            "reason_code": reason_code,
            "schema_version": _FINGERPRINT_VERSION,
            "source_fingerprint": source_fingerprint,
        }
    )
    return GraphRelationFactPlan(
        status=status,
        reason_code=reason_code,
        source_fingerprint=source_fingerprint,
        subject_fingerprint=subject_fingerprint,
        decision_fingerprint=decision_fingerprint,
        source_snapshot={
            **source_snapshot,
            "predicate": _predicate_snapshot(predicate) if predicate is not None else None,
            "source_fingerprint": source_fingerprint,
        },
        candidate_snapshot=candidate_snapshot,
        evidence_refs=[source_snapshot],
        logical_fact=None,
        assertion=None,
        predicate=predicate,
    )


def build_graph_relation_fact_plan(
    *,
    library_id: uuid.UUID,
    predicate: StablePredicateIdentity,
    candidate: Any,
    source_entity: Any,
    target_entity: Any,
    evidence: Any,
) -> GraphRelationFactPlan:
    source_canonical_entity_id = getattr(source_entity, "canonical_entity_id", None)
    if source_canonical_entity_id is None:
        return _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="source_canonical_pending",
            predicate=predicate,
        )
    if predicate.resolution_status != "resolved" or predicate.identity_policy_version == "legacy_v1":
        return _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_not_ready",
            predicate=predicate,
            subject_canonical_entity_id=source_canonical_entity_id,
        )
    try:
        policy = parse_stable_predicate_resolution_policy(predicate.resolution_policy or {})
        properties = candidate.proposed_properties
        if not isinstance(properties, Mapping):
            raise ValueError("candidate properties are invalid")
        extra_keys = set(properties) - _policy_property_keys(policy)
        if extra_keys:
            return _pending_plan(
                library_id=library_id,
                candidate=candidate,
                evidence=evidence,
                reason_code="unclassified_property",
                predicate=predicate,
                subject_canonical_entity_id=source_canonical_entity_id,
            )
        target_canonical_entity_id = getattr(target_entity, "canonical_entity_id", None)
        object_kind: str | None
        object_canonical_entity_id: uuid.UUID | None
        object_value: dict[str, Any] | None
        if policy.object_policy.source == "target_entity":
            if target_canonical_entity_id is None:
                return _pending_plan(
                    library_id=library_id,
                    candidate=candidate,
                    evidence=evidence,
                    reason_code="target_canonical_pending",
                    predicate=predicate,
                    subject_canonical_entity_id=source_canonical_entity_id,
                )
            object_kind = "entity"
            object_canonical_entity_id = target_canonical_entity_id
            object_value = None
        elif policy.object_policy.source == "property_literal":
            value = properties.get(policy.object_policy.property_key)
            if value is None:
                raise ValueError("object literal is missing")
            object_kind = "literal"
            object_canonical_entity_id = None
            object_value = _typed_literal(value, policy.object_policy, properties)
        else:
            object_kind = None
            object_canonical_entity_id = None
            object_value = None

        identity_qualifiers = {
            key: _typed_qualifier(properties[key])
            for key in policy.qualifier_policy.identity_bearing
            if key in properties
        }
        if len(identity_qualifiers) != len(policy.qualifier_policy.identity_bearing):
            raise ValueError("identity qualifier is missing")
        assertion_qualifiers = {
            key: _typed_qualifier(properties[key])
            for key in policy.qualifier_policy.assertion_bearing
            if key in properties
        }
        valid_time = _time_value(policy.valid_time_policy, properties)
        effective_time = _time_value(policy.effective_time_policy, properties)
        temporal_identity_key = None
        if policy.temporal_class == "event_fact":
            event_policy = policy.event_temporal_identity_policy
            assert event_policy is not None
            if event_policy.source == "valid_time":
                temporal_value = valid_time
            elif event_policy.source == "effective_time":
                temporal_value = effective_time
            else:
                temporal_value = _typed_qualifier(properties.get(event_policy.property_key))
            if temporal_value is None:
                raise ValueError("event temporal identity is missing")
            temporal_identity_key = _fingerprint(
                {"schema_version": "event_temporal_identity_v1", "value": temporal_value}
            )
        asserted_value = None
        if policy.temporal_class == "measurement_slot":
            measurement = policy.measurement_policy
            assert measurement is not None
            value = properties.get(measurement.value_property_key)
            if value is None:
                raise ValueError("measurement value is missing")
            asserted_value = _typed_literal(
                value,
                type("MeasurementObjectPolicy", (), {
                    "literal_type": measurement.value_type,
                    "unit_policy": measurement.unit_policy,
                    "currency_policy": measurement.currency_policy,
                })(),
                properties,
            )
        polarity = _value_from_mapping(policy.polarity_policy, properties)
        modality = _value_from_mapping(policy.modality_policy, properties)
    except (TypeError, ValueError, KeyError):
        return _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="policy_input_invalid",
            predicate=predicate,
            subject_canonical_entity_id=source_canonical_entity_id,
        )

    source_snapshot = _source_occurrence_snapshot(library_id, evidence)
    source_fingerprint = _fingerprint(source_snapshot)
    predicate_snapshot = _predicate_snapshot(predicate)
    logical_fact_fingerprint = _fingerprint(
        {
            "identity_policy_version": predicate.identity_policy_version,
            "identity_qualifiers": identity_qualifiers,
            "library_id": str(library_id),
            "object_canonical_entity_id": (
                str(object_canonical_entity_id) if object_canonical_entity_id is not None else None
            ),
            "object_kind": object_kind,
            "object_value": object_value,
            "predicate": predicate_snapshot,
            "schema_version": "logical_fact_identity_v1",
            "subject_canonical_entity_id": str(source_canonical_entity_id),
            "temporal_identity_key": temporal_identity_key,
        }
    )
    assertion_fingerprint = _fingerprint(
        {
            "asserted_value": asserted_value,
            "effective_time": effective_time,
            "logical_fact_identity_fingerprint": logical_fact_fingerprint,
            "modality": modality,
            "polarity": polarity,
            "qualifiers": assertion_qualifiers,
            "schema_version": "fact_assertion_identity_v1",
            "source_fingerprint": source_fingerprint,
            "valid_time": valid_time,
        }
    )
    candidate_snapshot = _candidate_snapshot(candidate, properties)
    decision_fingerprint = _fingerprint(
        {
            "assertion_fingerprint": assertion_fingerprint,
            "candidate": candidate_snapshot,
            "logical_fact_identity_fingerprint": logical_fact_fingerprint,
            "predicate": predicate_snapshot,
            "schema_version": _FINGERPRINT_VERSION,
            "source_fingerprint": source_fingerprint,
        }
    )
    subject_fingerprint = resolution_subject_fingerprint_v1(
        library_id,
        source_fingerprint=source_fingerprint,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
    )
    return GraphRelationFactPlan(
        status="resolved",
        reason_code=None,
        source_fingerprint=source_fingerprint,
        subject_fingerprint=subject_fingerprint,
        decision_fingerprint=decision_fingerprint,
        source_snapshot={
            **source_snapshot,
            "predicate": predicate_snapshot,
            "source_fingerprint": source_fingerprint,
        },
        candidate_snapshot=candidate_snapshot,
        evidence_refs=[source_snapshot],
        logical_fact=LogicalFactPlan(
            identity_fingerprint=logical_fact_fingerprint,
            object_kind=object_kind,
            object_canonical_entity_id=object_canonical_entity_id,
            object_value=object_value,
            identity_qualifiers=identity_qualifiers,
            temporal_identity_key=temporal_identity_key,
        ),
        assertion=FactAssertionPlan(
            assertion_fingerprint=assertion_fingerprint,
            asserted_object_kind=object_kind,
            asserted_object_canonical_entity_id=object_canonical_entity_id,
            asserted_value=asserted_value,
            polarity=polarity,
            modality=modality,
            qualifiers=assertion_qualifiers,
            valid_time=valid_time,
            effective_time=effective_time,
        ),
        predicate=predicate,
    )


async def _first(db: Any, statement: Any) -> Any | None:
    return (await db.execute(statement)).scalars().first()


async def _active_predicates(db: Any, *, library_id: uuid.UUID, relation_type_id: uuid.UUID) -> list[StablePredicateIdentity]:
    mappings = (
        await db.execute(
            select(StablePredicateMapping).where(
                StablePredicateMapping.library_id == library_id,
                StablePredicateMapping.relation_type_id == relation_type_id,
                StablePredicateMapping.mapping_status == "active",
            )
        )
    ).scalars().all()
    predicates: list[StablePredicateIdentity] = []
    for mapping in mappings:
        predicate = await db.get(StablePredicateIdentity, mapping.stable_predicate_identity_id)
        if predicate is not None and predicate.library_id == library_id:
            predicates.append(predicate)
    return predicates


def _decision(
    *,
    library_id: uuid.UUID,
    candidate: Any,
    plan: GraphRelationFactPlan,
    status: str,
    logical_fact: LogicalFact | None = None,
    assertion: FactAssertion | None = None,
    supersedes: FactResolutionDecision | None = None,
    source: FactResolutionSource | None = None,
) -> FactResolutionDecision:
    source = source or FactResolutionSource.graph_relation_candidate()
    return FactResolutionDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        source_kind=source.source_kind,
        subject_fingerprint=plan.subject_fingerprint,
        decision_fingerprint=plan.decision_fingerprint,
        graph_relation_candidate_id=(
            candidate.id if source.source_kind == "graph_relation_candidate" else None
        ),
        raw_claim_id=source.raw_claim_id,
        stable_predicate_identity_id=plan.predicate.id if plan.predicate is not None else None,
        logical_fact_id=logical_fact.id if logical_fact is not None else None,
        fact_assertion_id=assertion.id if assertion is not None else None,
        source_snapshot=plan.source_snapshot,
        candidate_snapshot=plan.candidate_snapshot,
        evidence_refs=plan.evidence_refs,
        status=status,
        method=source.method,
        confidence=getattr(candidate, "final_confidence", None),
        reason_code=plan.reason_code,
        resolver_version=source.resolver_version,
        supersedes_decision_id=supersedes.id if supersedes is not None else None,
        resolved_at=datetime.now().astimezone() if status == "resolved" else None,
    )


def _bridge(
    *,
    relation: Any,
    relation_evidence: Any,
    logical_fact: LogicalFact,
    assertion: FactAssertion,
) -> str | None:
    if relation.logical_fact_id not in {None, logical_fact.id}:
        return "knowledge_relation_fact_conflict"
    if relation_evidence.fact_assertion_id not in {None, assertion.id}:
        return "relation_evidence_assertion_conflict"
    relation.logical_fact_id = logical_fact.id
    relation_evidence.fact_assertion_id = assertion.id
    return None


def _with_resolution_subject(
    plan: GraphRelationFactPlan,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    source_entity: Any,
    target_entity: Any,
) -> GraphRelationFactPlan:
    return replace(
        plan,
        subject_fingerprint=resolution_subject_fingerprint_v1(
            library_id,
            source_fingerprint=plan.source_fingerprint,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
        ),
    )


def _advisory_lock_key(library_id: uuid.UUID, *, scope: str, fingerprint: str) -> int:
    digest = hashlib.sha256(f"{_LOCK_PREFIX}{scope}:{library_id}:{fingerprint}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _resolution_subject_lock_key(library_id: uuid.UUID, subject_fingerprint: str) -> int:
    return _advisory_lock_key(
        library_id,
        scope="decision-subject",
        fingerprint=subject_fingerprint,
    )


def _logical_fact_lock_key(library_id: uuid.UUID, identity_fingerprint: str) -> int:
    return _advisory_lock_key(
        library_id,
        scope="logical-fact",
        fingerprint=identity_fingerprint,
    )


async def _lock_advisory_key(db: Any, lock_key: int) -> None:
    try:
        bind = db.get_bind()
        dialect = bind.dialect.name if bind is not None else None
    except AttributeError:
        dialect = None
    if dialect is None:
        return
    if dialect != "postgresql":
        raise GraphRelationFactResolutionError("fact resolution requires PostgreSQL")
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


async def _lock_resolution_subject_v2(
    db: Any,
    *,
    library_id: uuid.UUID,
    subject_fingerprint: str,
) -> None:
    await _lock_advisory_key(db, _resolution_subject_lock_key(library_id, subject_fingerprint))


async def _lock_logical_fact_v2(
    db: Any,
    *,
    library_id: uuid.UUID,
    identity_fingerprint: str,
) -> None:
    await _lock_advisory_key(db, _logical_fact_lock_key(library_id, identity_fingerprint))


async def _current_decision_by_fingerprint(
    db: Any,
    *,
    library_id: uuid.UUID,
    decision_fingerprint: str,
) -> FactResolutionDecision | None:
    return await _first(
        db,
        select(FactResolutionDecision).where(
            FactResolutionDecision.library_id == library_id,
            FactResolutionDecision.decision_fingerprint == decision_fingerprint,
            FactResolutionDecision.status != "superseded",
        ),
    )


async def _current_decision_for_subject(
    db: Any,
    *,
    library_id: uuid.UUID,
    subject_fingerprint: str,
    source_kind: str = "graph_relation_candidate",
) -> FactResolutionDecision | None:
    return await _first(
        db,
        select(FactResolutionDecision)
        .where(
            FactResolutionDecision.library_id == library_id,
            FactResolutionDecision.source_kind == source_kind,
            FactResolutionDecision.subject_fingerprint == subject_fingerprint,
            FactResolutionDecision.status != "superseded",
        )
        .order_by(FactResolutionDecision.created_at.desc()),
    )


async def _persist_preflight_unresolved(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    source_entity: Any,
    target_entity: Any,
    plan: GraphRelationFactPlan,
    source: FactResolutionSource,
) -> GraphRelationFactPreflight:
    plan = with_fact_resolution_source(plan, source=source)
    plan = _with_resolution_subject(
        plan,
        library_id=library_id,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
    )
    return await _persist_unresolved_fact_plan(
        db,
        library_id=library_id,
        candidate=candidate,
        plan=plan,
        source=source,
    )


async def _persist_unresolved_fact_plan(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    plan: GraphRelationFactPlan,
    source: FactResolutionSource,
) -> GraphRelationFactPreflight:
    await _lock_resolution_subject_v2(
        db,
        library_id=library_id,
        subject_fingerprint=plan.subject_fingerprint,
    )
    existing = await _current_decision_by_fingerprint(
        db,
        library_id=library_id,
        decision_fingerprint=plan.decision_fingerprint,
    )
    if existing is not None:
        return GraphRelationFactPreflight(plan, existing, None, None, None)
    supersedes = await _current_decision_for_subject(
        db,
        library_id=library_id,
        subject_fingerprint=plan.subject_fingerprint,
        source_kind=source.source_kind,
    )
    if supersedes is not None:
        supersedes.status = "superseded"
        await db.flush()
    decision = _decision(
        library_id=library_id,
        candidate=candidate,
        plan=plan,
        status=plan.status,
        supersedes=supersedes,
        source=source,
    )
    db.add(decision)
    await db.flush()
    return GraphRelationFactPreflight(plan, decision, None, None, None)


async def preflight_raw_claim_projection_pending_fact(
    db: Any,
    *,
    library_id: uuid.UUID,
    raw_claim_id: uuid.UUID,
    source_content_fingerprint: str,
    evidence: Any,
    reason_code: str,
    projection_contexts: list[Mapping[str, Any]],
) -> GraphRelationFactPreflight:
    """Persist a RawClaim projection pending decision through the P2.3 decision lifecycle."""
    if (
        not isinstance(source_content_fingerprint, str)
        or len(source_content_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in source_content_fingerprint)
    ):
        raise GraphRelationFactResolutionError("raw claim content fingerprint is invalid")
    if len(projection_contexts) < 2:
        raise GraphRelationFactResolutionError("raw claim projection ambiguity requires multiple projections")
    normalized_contexts = sorted(
        (_normalized_property(context) for context in projection_contexts),
        key=_canonical_json,
    )
    source_occurrence = _source_occurrence_snapshot(library_id, evidence)
    source_snapshot = {
        "raw_claim_content_fingerprint": source_content_fingerprint,
        "schema_version": "raw_claim_fact_source_v1",
        "source_occurrence": source_occurrence,
    }
    source_fingerprint = _fingerprint(source_snapshot)
    plan = GraphRelationFactPlan(
        status="pending",
        reason_code=reason_code,
        source_fingerprint=source_fingerprint,
        subject_fingerprint=_fingerprint(
            {
                "library_id": str(library_id),
                "schema_version": "raw_claim_projection_subject_v1",
                "source_fingerprint": source_fingerprint,
            }
        ),
        decision_fingerprint=_fingerprint(
            {
                "projection_contexts": normalized_contexts,
                "reason_code": reason_code,
                "schema_version": "raw_claim_projection_pending_fact_v1",
                "source_fingerprint": source_fingerprint,
            }
        ),
        source_snapshot={**source_snapshot, "source_fingerprint": source_fingerprint},
        candidate_snapshot={
            "projection_contexts": normalized_contexts,
            "schema_version": "raw_claim_projection_pending_v1",
        },
        evidence_refs=[source_occurrence],
        logical_fact=None,
        assertion=None,
        predicate=None,
    )
    source = FactResolutionSource.raw_claim(raw_claim_id)
    plan = with_fact_resolution_source(plan, source=source)
    return await _persist_unresolved_fact_plan(
        db,
        library_id=library_id,
        candidate=SimpleNamespace(final_confidence=None),
        plan=plan,
        source=source,
    )


def _pending_from_rows(
    *,
    library_id: uuid.UUID,
    candidate: Any,
    evidence_rows: list[Any],
    reason_code: str,
    status: str = "pending",
    predicate: StablePredicateIdentity | None = None,
    subject_canonical_entity_id: uuid.UUID | None = None,
) -> GraphRelationFactPlan:
    source_snapshot = _source_occurrence_group_snapshot(library_id, evidence_rows)
    return _pending_plan(
        library_id=library_id,
        candidate=candidate,
        evidence=evidence_rows[0],
        reason_code=reason_code,
        status=status,
        predicate=predicate,
        subject_canonical_entity_id=subject_canonical_entity_id,
        source_snapshot=source_snapshot,
    )


async def preflight_graph_relation_candidate_fact(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    relation_type_id: uuid.UUID,
    source_entity: Any,
    target_entity: Any,
    evidence_rows: list[Any],
    source: FactResolutionSource | None = None,
    expected_logical_fact: LogicalFact | None = None,
    expected_assertion: FactAssertion | None = None,
    candidate_adapter: Callable[[StablePredicateIdentity], Any] | None = None,
) -> GraphRelationFactPreflight:
    source = source or FactResolutionSource.graph_relation_candidate()
    if not evidence_rows:
        raise GraphRelationFactResolutionError("fact resolution requires resolved evidence")
    source_canonical_entity_id = getattr(source_entity, "canonical_entity_id", None)
    if getattr(candidate, "evidence_support_mode", "single_evidence") != "single_evidence" or len(evidence_rows) != 1:
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=_pending_from_rows(
                library_id=library_id,
                candidate=candidate,
                evidence_rows=evidence_rows,
                reason_code="multi_evidence_fact_resolution_unsupported",
                subject_canonical_entity_id=source_canonical_entity_id,
            ),
            source=source,
        )

    evidence = evidence_rows[0]
    predicates = await _active_predicates(
        db,
        library_id=library_id,
        relation_type_id=relation_type_id,
    )
    if not predicates:
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_mapping_pending",
            subject_canonical_entity_id=source_canonical_entity_id,
        )
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=plan,
            source=source,
        )
    if len(predicates) != 1:
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_mapping_ambiguous",
            subject_canonical_entity_id=source_canonical_entity_id,
        )
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=plan,
            source=source,
        )
    predicate = predicates[0]
    if predicate.resolution_status == "rejected":
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_rejected",
            status="rejected",
            predicate=predicate,
            subject_canonical_entity_id=source_canonical_entity_id,
        )
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=plan,
            source=source,
        )
    if not is_predicate_ready_for_fact_resolution(predicate, active_mapping_count=1):
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_not_ready",
            predicate=predicate,
            subject_canonical_entity_id=source_canonical_entity_id,
        )
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=plan,
            source=source,
        )
    if candidate_adapter is not None:
        adapted = candidate_adapter(predicate)
        if getattr(adapted, "status", None) != "ready" or getattr(adapted, "candidate", None) is None:
            plan = _pending_plan(
                library_id=library_id,
                candidate=candidate,
                evidence=evidence,
                reason_code=getattr(adapted, "reason_code", None) or "policy_input_invalid",
                predicate=predicate,
                subject_canonical_entity_id=source_canonical_entity_id,
            )
            return await _persist_preflight_unresolved(
                db,
                library_id=library_id,
                candidate=candidate,
                source_entity=source_entity,
                target_entity=target_entity,
                plan=plan,
                source=source,
            )
        candidate = adapted.candidate

    plan = build_graph_relation_fact_plan(
        library_id=library_id,
        predicate=predicate,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence=evidence,
    )
    plan = _with_resolution_subject(
        plan,
        library_id=library_id,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
    )
    if plan.status != "resolved":
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=plan,
            source=source,
        )
    if (
        (expected_logical_fact is not None
         and plan.logical_fact is not None
         and expected_logical_fact.identity_fingerprint != plan.logical_fact.identity_fingerprint)
        or (
            expected_assertion is not None
            and plan.assertion is not None
            and expected_assertion.assertion_fingerprint != plan.assertion.assertion_fingerprint
        )
    ):
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=_pending_plan(
                library_id=library_id,
                candidate=candidate,
                evidence=evidence,
                reason_code="raw_claim_projection_semantic_conflict",
                predicate=predicate,
                subject_canonical_entity_id=source_canonical_entity_id,
            ),
            source=source,
        )
    plan = with_fact_resolution_source(plan, source=source)

    await _lock_resolution_subject_v2(
        db,
        library_id=library_id,
        subject_fingerprint=plan.subject_fingerprint,
    )
    existing = await _current_decision_by_fingerprint(
        db,
        library_id=library_id,
        decision_fingerprint=plan.decision_fingerprint,
    )
    if existing is not None:
        if existing.status != "resolved":
            return GraphRelationFactPreflight(plan, existing, None, None, None)
        assert plan.logical_fact is not None
        await _lock_logical_fact_v2(
            db,
            library_id=library_id,
            identity_fingerprint=plan.logical_fact.identity_fingerprint,
        )
        logical_fact = await db.get(LogicalFact, existing.logical_fact_id)
        assertion = await db.get(FactAssertion, existing.fact_assertion_id)
        if logical_fact is None or assertion is None:
            raise GraphRelationFactResolutionError("resolved decision has missing fact assertion links")
        return GraphRelationFactPreflight(plan, existing, logical_fact, assertion, None)

    assert plan.logical_fact is not None
    assert plan.assertion is not None
    await _lock_logical_fact_v2(
        db,
        library_id=library_id,
        identity_fingerprint=plan.logical_fact.identity_fingerprint,
    )
    facts = (
        await db.execute(
            select(LogicalFact).where(
                LogicalFact.library_id == library_id,
                LogicalFact.identity_fingerprint == plan.logical_fact.identity_fingerprint,
            )
        )
    ).scalars().all()
    if len(facts) > 1:
        ambiguous_plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="logical_fact_identity_ambiguous",
            predicate=predicate,
            subject_canonical_entity_id=source_canonical_entity_id,
        )
        return await _persist_preflight_unresolved(
            db,
            library_id=library_id,
            candidate=candidate,
            source_entity=source_entity,
            target_entity=target_entity,
            plan=ambiguous_plan,
            source=source,
        )
    logical_fact = facts[0] if facts else None
    assertion = await _first(
        db,
        select(FactAssertion).where(
            FactAssertion.library_id == library_id,
            FactAssertion.assertion_fingerprint == plan.assertion.assertion_fingerprint,
        ),
    )
    if assertion is not None and (logical_fact is None or assertion.logical_fact_id != logical_fact.id):
        raise GraphRelationFactResolutionError("assertion fingerprint does not match logical fact")
    supersedes = await _current_decision_for_subject(
        db,
        library_id=library_id,
        subject_fingerprint=plan.subject_fingerprint,
        source_kind=source.source_kind,
    )
    return GraphRelationFactPreflight(plan, None, logical_fact, assertion, supersedes)


async def materialize_resolved_graph_relation_fact(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    relation: Any,
    source_entity: Any,
    preflight: GraphRelationFactPreflight,
    source: FactResolutionSource | None = None,
) -> GraphRelationFactMaterialization:
    source = source or FactResolutionSource.graph_relation_candidate()
    if not preflight.is_resolved:
        raise GraphRelationFactResolutionError("cannot materialize an unresolved fact plan")
    if preflight.decision is not None:
        if preflight.logical_fact is None or preflight.assertion is None:
            raise GraphRelationFactResolutionError("resolved decision has missing fact assertion links")
        return GraphRelationFactMaterialization(
            preflight.logical_fact,
            preflight.assertion,
            "REUSE",
            "REUSE",
        )

    plan = preflight.plan
    predicate = plan.predicate
    assert plan.logical_fact is not None
    assert plan.assertion is not None
    assert predicate is not None
    logical_fact = preflight.logical_fact
    if logical_fact is None:
        logical_fact = LogicalFact(
            id=uuid.uuid4(),
            library_id=library_id,
            stable_predicate_identity_id=predicate.id,
            subject_canonical_entity_id=source_entity.canonical_entity_id,
            object_kind=plan.logical_fact.object_kind,
            object_canonical_entity_id=plan.logical_fact.object_canonical_entity_id,
            object_value=plan.logical_fact.object_value,
            identity_qualifiers=plan.logical_fact.identity_qualifiers,
            temporal_identity_key=plan.logical_fact.temporal_identity_key,
            identity_policy_version=predicate.identity_policy_version,
            identity_fingerprint=plan.logical_fact.identity_fingerprint,
            status="active",
        )
        db.add(logical_fact)
        logical_outcome = "CREATE"
    else:
        logical_outcome = "REUSE"
    assertion = preflight.assertion
    if assertion is None:
        assertion = FactAssertion(
            id=uuid.uuid4(),
            library_id=library_id,
            logical_fact_id=logical_fact.id,
            knowledge_relation_id=relation.id,
            assertion_fingerprint=plan.assertion.assertion_fingerprint,
            asserted_object_kind=plan.assertion.asserted_object_kind,
            asserted_object_canonical_entity_id=plan.assertion.asserted_object_canonical_entity_id,
            asserted_value=plan.assertion.asserted_value,
            polarity=plan.assertion.polarity,
            modality=plan.assertion.modality,
            qualifiers=plan.assertion.qualifiers,
            valid_time=plan.assertion.valid_time,
            effective_time=plan.assertion.effective_time,
            confidence=getattr(candidate, "final_confidence", None),
            source_kind=source.source_kind,
            raw_claim_id=source.raw_claim_id,
            graph_relation_candidate_id=(
                candidate.id if source.source_kind == "graph_relation_candidate" else None
            ),
            status="active",
        )
        db.add(assertion)
        assertion_outcome = "CREATE"
    else:
        assertion_outcome = "REUSE"
    return GraphRelationFactMaterialization(logical_fact, assertion, logical_outcome, assertion_outcome)


async def finalize_resolved_graph_relation_fact(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    relation: Any,
    relation_evidence: Any,
    preflight: GraphRelationFactPreflight,
    materialization: GraphRelationFactMaterialization,
    source: FactResolutionSource | None = None,
) -> GraphRelationFactResolutionResult:
    source = source or FactResolutionSource.graph_relation_candidate()
    bridge_error = _bridge(
        relation=relation,
        relation_evidence=relation_evidence,
        logical_fact=materialization.logical_fact,
        assertion=materialization.assertion,
    )
    if bridge_error is not None:
        raise GraphRelationFactResolutionError(bridge_error)
    if preflight.decision is not None:
        return GraphRelationFactResolutionResult(
            "REUSE",
            preflight.decision,
            materialization.logical_fact,
            materialization.assertion,
        )
    if preflight.supersedes is not None:
        preflight.supersedes.status = "superseded"
        await db.flush()
    decision = _decision(
        library_id=library_id,
        candidate=candidate,
        plan=preflight.plan,
        status="resolved",
        logical_fact=materialization.logical_fact,
        assertion=materialization.assertion,
        supersedes=preflight.supersedes,
        source=source,
    )
    db.add(decision)
    await db.flush()
    outcome = (
        "CREATE"
        if materialization.logical_outcome == "CREATE" or materialization.assertion_outcome == "CREATE"
        else "REUSE"
    )
    return GraphRelationFactResolutionResult(
        outcome,
        decision,
        materialization.logical_fact,
        materialization.assertion,
    )


async def resolve_graph_relation_candidate_fact(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    relation: Any,
    relation_evidence: Any,
    source_entity: Any,
    target_entity: Any,
    evidence: Any,
) -> GraphRelationFactResolutionResult:
    """Compatibility wrapper for direct resolver callers with an existing projection."""
    preflight = await preflight_graph_relation_candidate_fact(
        db,
        library_id=library_id,
        candidate=candidate,
        relation_type_id=relation.relation_type_id,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence_rows=[evidence],
    )
    if not preflight.is_resolved:
        assert preflight.decision is not None
        return GraphRelationFactResolutionResult(
            _unresolved_outcome(preflight.decision.status),
            preflight.decision,
            None,
            None,
        )
    materialization = await materialize_resolved_graph_relation_fact(
        db,
        library_id=library_id,
        candidate=candidate,
        relation=relation,
        source_entity=source_entity,
        preflight=preflight,
    )
    return await finalize_resolved_graph_relation_fact(
        db,
        library_id=library_id,
        candidate=candidate,
        relation=relation,
        relation_evidence=relation_evidence,
        preflight=preflight,
        materialization=materialization,
    )
