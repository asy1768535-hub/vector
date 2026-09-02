from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

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


_FINGERPRINT_VERSION = "p2_graph_relation_fact_v1"
_LOCK_PREFIX = "vector-kb:fact-resolution-subject:"


def _unresolved_outcome(status: str) -> str:
    return "REJECT" if status == "rejected" else "PENDING"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


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


def _with_projection_provenance(
    plan: GraphRelationFactPlan,
    *,
    candidate: Any,
    relation: Any,
) -> GraphRelationFactPlan:
    candidate_snapshot = dict(plan.candidate_snapshot)
    candidate_snapshot.update(
        {
            "graph_relation_candidate_id": str(candidate.id),
            "knowledge_relation_id": str(relation.id),
        }
    )
    return replace(plan, candidate_snapshot=candidate_snapshot)


def _pending_plan(
    *,
    library_id: uuid.UUID,
    candidate: Any,
    evidence: Any,
    reason_code: str,
    status: str = "pending",
    predicate: StablePredicateIdentity | None = None,
    subject_canonical_entity_id: uuid.UUID | None = None,
) -> GraphRelationFactPlan:
    source_snapshot = _source_occurrence_snapshot(library_id, evidence)
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
    subject_fingerprint = _fingerprint(
        {
            "library_id": str(library_id),
            "schema_version": "fact_resolution_subject_v1",
            "subject_canonical_entity_id": str(source_canonical_entity_id),
        }
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


def _fact_resolution_lock_key(library_id: uuid.UUID, subject_fingerprint: str) -> int:
    digest = hashlib.sha256(
        f"{_LOCK_PREFIX}{library_id}:{subject_fingerprint}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _lock_fact_resolution_subject(db: Any, *, library_id: uuid.UUID, subject_fingerprint: str) -> None:
    try:
        bind = db.get_bind()
        dialect = bind.dialect.name if bind is not None else None
    except AttributeError:
        dialect = None
    if dialect is None:
        return
    if dialect != "postgresql":
        raise GraphRelationFactResolutionError("fact resolution requires PostgreSQL")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _fact_resolution_lock_key(library_id, subject_fingerprint)},
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


async def _existing_decision(db: Any, *, library_id: uuid.UUID, decision_fingerprint: str) -> FactResolutionDecision | None:
    return await _first(
        db,
        select(FactResolutionDecision).where(
            FactResolutionDecision.library_id == library_id,
            FactResolutionDecision.decision_fingerprint == decision_fingerprint,
        ),
    )


async def _decision_to_supersede(db: Any, *, library_id: uuid.UUID, candidate_id: uuid.UUID) -> FactResolutionDecision | None:
    return await _first(
        db,
        select(FactResolutionDecision)
        .where(
            FactResolutionDecision.library_id == library_id,
            FactResolutionDecision.graph_relation_candidate_id == candidate_id,
            FactResolutionDecision.status != "superseded",
        )
        .order_by(FactResolutionDecision.created_at.desc()),
    )


def _decision(
    *,
    library_id: uuid.UUID,
    candidate: Any,
    plan: GraphRelationFactPlan,
    status: str,
    logical_fact: LogicalFact | None = None,
    assertion: FactAssertion | None = None,
    supersedes: FactResolutionDecision | None = None,
) -> FactResolutionDecision:
    return FactResolutionDecision(
        id=uuid.uuid4(),
        library_id=library_id,
        source_kind="graph_relation_candidate",
        subject_fingerprint=plan.subject_fingerprint,
        decision_fingerprint=plan.decision_fingerprint,
        graph_relation_candidate_id=candidate.id,
        stable_predicate_identity_id=plan.predicate.id if plan.predicate is not None else None,
        logical_fact_id=logical_fact.id if logical_fact is not None else None,
        fact_assertion_id=assertion.id if assertion is not None else None,
        source_snapshot=plan.source_snapshot,
        candidate_snapshot=plan.candidate_snapshot,
        evidence_refs=plan.evidence_refs,
        status=status,
        method="p2_graph_relation_candidate_v1",
        confidence=getattr(candidate, "final_confidence", None),
        reason_code=plan.reason_code,
        resolver_version="p2_3_fact_resolution_v1",
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


async def _persist_pending(
    db: Any,
    *,
    library_id: uuid.UUID,
    candidate: Any,
    plan: GraphRelationFactPlan,
) -> GraphRelationFactResolutionResult:
    existing = await _existing_decision(
        db,
        library_id=library_id,
        decision_fingerprint=plan.decision_fingerprint,
    )
    if existing is not None:
        return GraphRelationFactResolutionResult(
            "REUSE" if existing.status != plan.status else _unresolved_outcome(plan.status),
            existing,
            None,
            None,
        )
    supersedes = await _decision_to_supersede(db, library_id=library_id, candidate_id=candidate.id)
    if supersedes is not None:
        supersedes.status = "superseded"
    decision = _decision(
        library_id=library_id,
        candidate=candidate,
        plan=plan,
        status=plan.status,
        supersedes=supersedes,
    )
    db.add(decision)
    await db.flush()
    return GraphRelationFactResolutionResult(_unresolved_outcome(plan.status), decision, None, None)


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
    predicates = await _active_predicates(
        db,
        library_id=library_id,
        relation_type_id=relation.relation_type_id,
    )
    subject_canonical_entity_id = getattr(source_entity, "canonical_entity_id", None)
    if not predicates:
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_mapping_pending",
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        plan = _with_projection_provenance(plan, candidate=candidate, relation=relation)
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=plan)
    if len(predicates) != 1:
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_mapping_ambiguous",
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        plan = _with_projection_provenance(plan, candidate=candidate, relation=relation)
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=plan)
    predicate = predicates[0]
    if predicate.resolution_status == "rejected":
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_rejected",
            status="rejected",
            predicate=predicate,
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        plan = _with_projection_provenance(plan, candidate=candidate, relation=relation)
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=plan)
    if not is_predicate_ready_for_fact_resolution(predicate, active_mapping_count=1):
        plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code="predicate_not_ready",
            predicate=predicate,
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        plan = _with_projection_provenance(plan, candidate=candidate, relation=relation)
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=plan)
    plan = build_graph_relation_fact_plan(
        library_id=library_id,
        predicate=predicate,
        candidate=candidate,
        source_entity=source_entity,
        target_entity=target_entity,
        evidence=evidence,
    )
    plan = _with_projection_provenance(plan, candidate=candidate, relation=relation)
    if plan.status != "resolved":
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=plan)

    await _lock_fact_resolution_subject(
        db,
        library_id=library_id,
        subject_fingerprint=plan.subject_fingerprint,
    )
    existing_decision = await _existing_decision(
        db,
        library_id=library_id,
        decision_fingerprint=plan.decision_fingerprint,
    )
    if existing_decision is not None:
        if existing_decision.status != "resolved":
            return GraphRelationFactResolutionResult("REUSE", existing_decision, None, None)
        logical_fact = await db.get(LogicalFact, existing_decision.logical_fact_id)
        assertion = await db.get(FactAssertion, existing_decision.fact_assertion_id)
        if logical_fact is None or assertion is None:
            raise GraphRelationFactResolutionError("resolved decision has missing fact assertion links")
        bridge_error = _bridge(
            relation=relation,
            relation_evidence=relation_evidence,
            logical_fact=logical_fact,
            assertion=assertion,
        )
        if bridge_error is not None:
            conflict_plan = _pending_plan(
                library_id=library_id,
                candidate=candidate,
                evidence=evidence,
                reason_code=bridge_error,
                predicate=predicate,
                subject_canonical_entity_id=subject_canonical_entity_id,
            )
            return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=conflict_plan)
        return GraphRelationFactResolutionResult("REUSE", existing_decision, logical_fact, assertion)

    assert plan.logical_fact is not None
    assert plan.assertion is not None
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
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=ambiguous_plan)
    if facts:
        logical_fact = facts[0]
        logical_outcome = "REUSE"
    else:
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
        logical_outcome = "CREATE"

    existing_assertion = await _first(
        db,
        select(FactAssertion).where(
            FactAssertion.library_id == library_id,
            FactAssertion.assertion_fingerprint == plan.assertion.assertion_fingerprint,
        ),
    )
    if existing_assertion is not None:
        if existing_assertion.logical_fact_id != logical_fact.id:
            raise GraphRelationFactResolutionError("assertion fingerprint does not match logical fact")
        assertion = existing_assertion
        assertion_outcome = "REUSE"
    else:
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
            source_kind="graph_relation_candidate",
            graph_relation_candidate_id=candidate.id,
            status="active",
        )
        assertion_outcome = "CREATE"

    bridge_error = _bridge(
        relation=relation,
        relation_evidence=relation_evidence,
        logical_fact=logical_fact,
        assertion=assertion,
    )
    if bridge_error is not None:
        conflict_plan = _pending_plan(
            library_id=library_id,
            candidate=candidate,
            evidence=evidence,
            reason_code=bridge_error,
            predicate=predicate,
            subject_canonical_entity_id=subject_canonical_entity_id,
        )
        return await _persist_pending(db, library_id=library_id, candidate=candidate, plan=conflict_plan)
    if logical_outcome == "CREATE":
        db.add(logical_fact)
    if assertion_outcome == "CREATE":
        db.add(assertion)

    supersedes = await _decision_to_supersede(db, library_id=library_id, candidate_id=candidate.id)
    if supersedes is not None:
        supersedes.status = "superseded"
    decision = _decision(
        library_id=library_id,
        candidate=candidate,
        plan=plan,
        status="resolved",
        logical_fact=logical_fact,
        assertion=assertion,
        supersedes=supersedes,
    )
    db.add(decision)
    await db.flush()
    outcome = "CREATE" if logical_outcome == "CREATE" or assertion_outcome == "CREATE" else "REUSE"
    return GraphRelationFactResolutionResult(outcome, decision, logical_fact, assertion)
