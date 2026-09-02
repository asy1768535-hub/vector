"""Deterministic FactAssertion and LogicalFact lifecycle recalculation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from itertools import combinations
from typing import Any, Iterable

from sqlalchemy import select, text

from app.models.entity import Entity
from app.models.fact_foundation import FactAssertion, FactResolutionDecision, LogicalFact, StablePredicateIdentity
from app.models.knowledge_relation import KnowledgeRelation
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_relation_fact_resolution import _logical_fact_lock_key


FACTUAL_MODALITIES = {"confirmed", "completed"}
FUNCTIONAL_SOURCE_CARDINALITIES = {"one_to_one", "many_to_one"}


def derive_assertion_lifecycle_status(
    current_status: str,
    evidence_rows: Iterable[Any],
    *,
    allow_reactivation: bool = False,
) -> str:
    """Derive source lifecycle without treating absent legacy lineage as stale."""
    if current_status == "rejected":
        return current_status
    rows = list(evidence_rows)
    if not rows:
        return current_status
    if any(row.status == "active" for row in rows):
        if current_status == "superseded" and not allow_reactivation:
            return current_status
        return "active"
    if current_status == "superseded":
        return current_status
    return "stale"


def is_effectively_supported_assertion(assertion: Any, evidence_rows: Iterable[Any]) -> bool:
    return assertion.status == "active" and any(
        row.status == "active" and row.support_type == "supports" for row in evidence_rows
    )


def _time_kind(value: dict[str, Any]) -> str | None:
    kind = value.get("kind")
    if kind not in {"instant", "range"}:
        return None
    raw = value.get("value") if kind == "instant" else value.get("from")
    if not isinstance(raw, str):
        return None
    return "datetime" if "T" in raw or " " in raw else "date"


def _parse_time(value: str, kind: str) -> date | datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if kind == "datetime" else date.fromisoformat(value)
    except ValueError:
        return None


def _dimension_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    left_kind = _time_kind(left)
    right_kind = _time_kind(right)
    if left_kind is None or right_kind is None or left_kind != right_kind:
        return None
    left_from = left.get("value") if left["kind"] == "instant" else left.get("from")
    left_to = left_from if left["kind"] == "instant" else left.get("to")
    right_from = right.get("value") if right["kind"] == "instant" else right.get("from")
    right_to = right_from if right["kind"] == "instant" else right.get("to")
    if not all(isinstance(item, str) for item in (left_from, left_to, right_from, right_to)):
        return None
    parsed = [_parse_time(item, left_kind) for item in (left_from, left_to, right_from, right_to)]
    if any(item is None for item in parsed):
        return None
    left_start, left_end, right_start, right_end = parsed
    if isinstance(left_start, datetime) and (
        left_start.tzinfo is None
        or left_end.tzinfo is None
        or right_start.tzinfo is None
        or right_end.tzinfo is None
    ):
        return None
    return left_start <= right_end and right_start <= left_end


def time_overlap_v1(
    left_valid_time: dict[str, Any] | None,
    right_valid_time: dict[str, Any] | None,
    left_effective_time: dict[str, Any] | None,
    right_effective_time: dict[str, Any] | None,
) -> bool | None:
    dimensions = [
        (left_valid_time, right_valid_time),
        (left_effective_time, right_effective_time),
    ]
    comparable: list[bool | None] = [
        _dimension_overlap(left, right)
        for left, right in dimensions
        if left is not None and right is not None
    ]
    if any(result is False for result in comparable):
        return False
    if comparable and all(result is True for result in comparable):
        return True
    if all(left is None and right is None for left, right in dimensions):
        return True
    return None


def _factual(assertion: Any) -> bool:
    return assertion.modality in FACTUAL_MODALITIES


def _assertions_overlap(left: Any, right: Any) -> bool:
    return time_overlap_v1(
        left.valid_time,
        right.valid_time,
        left.effective_time,
        right.effective_time,
    ) is True


def logical_fact_conflict_status(temporal_class: str, eligible_assertions: Iterable[Any]) -> str:
    assertions = [assertion for assertion in eligible_assertions if _factual(assertion)]
    for left, right in combinations(assertions, 2):
        if not _assertions_overlap(left, right):
            continue
        if {left.polarity, right.polarity} == {"affirmed", "negated"}:
            return "conflicted"
        if (
            temporal_class == "measurement_slot"
            and left.polarity == right.polarity == "affirmed"
            and left.asserted_value != right.asserted_value
        ):
            return "conflicted"
    return "active"


async def reconcile_fact_assertion_lifecycle(
    db: Any,
    *,
    library_id: uuid.UUID,
    assertion_id: uuid.UUID,
    allow_reactivation: bool = False,
) -> uuid.UUID | None:
    assertion = await db.get(FactAssertion, assertion_id)
    if assertion is None or assertion.library_id != library_id:
        return None
    evidence_rows = await _rows(
        db,
        select(RelationEvidence).where(
            RelationEvidence.library_id == library_id,
            RelationEvidence.fact_assertion_id == assertion.id,
        ),
    )
    assertion.status = derive_assertion_lifecycle_status(
        assertion.status,
        evidence_rows,
        allow_reactivation=allow_reactivation,
    )
    return assertion.logical_fact_id


async def reconcile_relation_evidence_lifecycle(
    db: Any,
    *,
    library_id: uuid.UUID,
    relation_evidence_rows: Iterable[RelationEvidence],
) -> None:
    affected_facts: set[uuid.UUID] = set()
    for assertion_id in {row.fact_assertion_id for row in relation_evidence_rows if row.fact_assertion_id is not None}:
        fact_id = await reconcile_fact_assertion_lifecycle(
            db,
            library_id=library_id,
            assertion_id=assertion_id,
        )
        if fact_id is not None:
            affected_facts.add(fact_id)
    for fact_id in affected_facts:
        await recalculate_logical_fact_status(db, library_id=library_id, logical_fact_id=fact_id)


async def reconcile_resolved_fact_decision(
    db: Any,
    *,
    library_id: uuid.UUID,
    decision: FactResolutionDecision,
) -> None:
    if decision.library_id != library_id or decision.status != "resolved" or decision.fact_assertion_id is None:
        return
    affected_facts: set[uuid.UUID] = set()
    current_fact_id = await reconcile_fact_assertion_lifecycle(
        db,
        library_id=library_id,
        assertion_id=decision.fact_assertion_id,
        allow_reactivation=True,
    )
    if current_fact_id is not None:
        affected_facts.add(current_fact_id)
    if decision.supersedes_decision_id is not None:
        previous = await db.get(FactResolutionDecision, decision.supersedes_decision_id)
        previous_assertion_id = getattr(previous, "fact_assertion_id", None)
        if previous_assertion_id is not None and previous_assertion_id != decision.fact_assertion_id:
            current_references = await _rows(
                db,
                select(FactResolutionDecision).where(
                    FactResolutionDecision.library_id == library_id,
                    FactResolutionDecision.fact_assertion_id == previous_assertion_id,
                    FactResolutionDecision.status == "resolved",
                ),
            )
            if not current_references:
                previous_assertion = await db.get(FactAssertion, previous_assertion_id)
                if previous_assertion is not None:
                    previous_assertion.status = "superseded"
                    affected_facts.add(previous_assertion.logical_fact_id)
    for fact_id in affected_facts:
        await recalculate_logical_fact_status(db, library_id=library_id, logical_fact_id=fact_id)


async def recalculate_logical_fact_status(
    db: Any,
    *,
    library_id: uuid.UUID,
    logical_fact_id: uuid.UUID,
) -> str | None:
    fact = await db.get(LogicalFact, logical_fact_id)
    if fact is None or fact.library_id != library_id:
        return None
    await _lock_fact(db, library_id, fact.identity_fingerprint)
    predicate = await db.get(StablePredicateIdentity, fact.stable_predicate_identity_id)
    if predicate is None or predicate.library_id != library_id:
        return None
    if predicate.temporal_class == "state_fact":
        await recalculate_functional_fact_scope(db, library_id=library_id, fact=fact, predicate=predicate)
        return fact.status
    fact.status = await _derived_fact_status(db, library_id=library_id, fact=fact, predicate=predicate)
    return fact.status


async def recalculate_functional_fact_scope(
    db: Any,
    *,
    library_id: uuid.UUID,
    fact: LogicalFact,
    predicate: StablePredicateIdentity,
) -> None:
    await _lock_functional_scope(db, library_id, fact)
    siblings = [
        row
        for row in await _rows(
            db,
            select(LogicalFact).where(
                LogicalFact.library_id == library_id,
                LogicalFact.subject_canonical_entity_id == fact.subject_canonical_entity_id,
                LogicalFact.stable_predicate_identity_id == fact.stable_predicate_identity_id,
                LogicalFact.identity_policy_version == fact.identity_policy_version,
            ),
        )
        if row.identity_qualifiers == fact.identity_qualifiers
    ]
    assertions = await _rows(
        db,
        select(FactAssertion).where(
            FactAssertion.library_id == library_id,
            FactAssertion.logical_fact_id.in_([row.id for row in siblings]),
        ),
    )
    evidence_by_assertion = await _evidence_by_assertion(db, library_id, assertions)
    eligible_by_fact = {
        row.id: [
            assertion
            for assertion in assertions
            if assertion.logical_fact_id == row.id
            and is_effectively_supported_assertion(assertion, evidence_by_assertion.get(assertion.id, []))
        ]
        for row in siblings
    }
    conflicted_ids: set[uuid.UUID] = set()
    for left, right in combinations(siblings, 2):
        if not await _facts_have_functional_conflict(
            db,
            library_id=library_id,
            left_assertions=eligible_by_fact[left.id],
            right_assertions=eligible_by_fact[right.id],
        ):
            continue
        conflicted_ids.update((left.id, right.id))
    for row in siblings:
        status = _status_from_assertions(
            predicate.temporal_class,
            [assertion for assertion in assertions if assertion.logical_fact_id == row.id],
            evidence_by_assertion,
        )
        row.status = "conflicted" if row.id in conflicted_ids else status


async def _derived_fact_status(
    db: Any,
    *,
    library_id: uuid.UUID,
    fact: LogicalFact,
    predicate: StablePredicateIdentity,
) -> str:
    assertions = await _rows(
        db,
        select(FactAssertion).where(
            FactAssertion.library_id == library_id,
            FactAssertion.logical_fact_id == fact.id,
        ),
    )
    return _status_from_assertions(
        predicate.temporal_class,
        assertions,
        await _evidence_by_assertion(db, library_id, assertions),
    )


def _status_from_assertions(
    temporal_class: str,
    assertions: list[FactAssertion],
    evidence_by_assertion: dict[uuid.UUID, list[RelationEvidence]],
) -> str:
    eligible = [
        assertion
        for assertion in assertions
        if is_effectively_supported_assertion(assertion, evidence_by_assertion.get(assertion.id, []))
    ]
    if eligible:
        return logical_fact_conflict_status(temporal_class, eligible)
    if any(assertion.status == "active" and not evidence_by_assertion.get(assertion.id) for assertion in assertions):
        return "active"
    return "inactive"


async def _facts_have_functional_conflict(
    db: Any,
    *,
    library_id: uuid.UUID,
    left_assertions: list[FactAssertion],
    right_assertions: list[FactAssertion],
) -> bool:
    for left in left_assertions:
        for right in right_assertions:
            if not (_factual(left) and _factual(right) and left.polarity == right.polarity == "affirmed"):
                continue
            if not _assertions_overlap(left, right):
                continue
            if await _has_functional_source_contract(db, library_id=library_id, assertion=left) and await _has_functional_source_contract(db, library_id=library_id, assertion=right):
                return True
    return False


async def _has_functional_source_contract(db: Any, *, library_id: uuid.UUID, assertion: FactAssertion) -> bool:
    if assertion.knowledge_relation_id is None:
        return False
    relation = await db.get(KnowledgeRelation, assertion.knowledge_relation_id)
    if relation is None or relation.library_id != library_id:
        return False
    source = await db.get(Entity, relation.source_entity_id)
    target = await db.get(Entity, relation.target_entity_id)
    if source is None or target is None:
        return False
    constraints = await _rows(
        db,
        select(RelationTypeConstraint).where(
            RelationTypeConstraint.library_id == library_id,
            RelationTypeConstraint.ontology_version_id == relation.ontology_version_id,
            RelationTypeConstraint.relation_type_id == relation.relation_type_id,
            RelationTypeConstraint.source_entity_type_id == source.entity_type_id,
            RelationTypeConstraint.target_entity_type_id == target.entity_type_id,
            RelationTypeConstraint.status == "active",
        ),
    )
    return len(constraints) == 1 and constraints[0].cardinality in FUNCTIONAL_SOURCE_CARDINALITIES


async def _evidence_by_assertion(
    db: Any,
    library_id: uuid.UUID,
    assertions: Iterable[FactAssertion],
) -> dict[uuid.UUID, list[RelationEvidence]]:
    assertion_ids = [assertion.id for assertion in assertions]
    if not assertion_ids:
        return {}
    rows = await _rows(
        db,
        select(RelationEvidence).where(
            RelationEvidence.library_id == library_id,
            RelationEvidence.fact_assertion_id.in_(assertion_ids),
        ),
    )
    result = {assertion_id: [] for assertion_id in assertion_ids}
    for row in rows:
        result.setdefault(row.fact_assertion_id, []).append(row)
    return result


async def _rows(db: Any, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _lock_key(library_id: uuid.UUID, *, scope: str, payload: Any) -> int:
    encoded = json.dumps({"library_id": str(library_id), "scope": scope, "payload": payload}, sort_keys=True).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], byteorder="big", signed=True)


async def _lock(db: Any, key: int) -> None:
    bind = getattr(db, "get_bind", lambda: None)()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": key})


async def _lock_fact(db: Any, library_id: uuid.UUID, identity_fingerprint: str) -> None:
    await _lock(db, _logical_fact_lock_key(library_id, identity_fingerprint))


async def _lock_functional_scope(db: Any, library_id: uuid.UUID, fact: LogicalFact) -> None:
    await _lock(
        db,
        _lock_key(
            library_id,
            scope="functional-fact",
            payload={
                "subject": str(fact.subject_canonical_entity_id),
                "predicate": str(fact.stable_predicate_identity_id),
                "qualifiers": fact.identity_qualifiers,
                "policy": fact.identity_policy_version,
            },
        ),
    )
