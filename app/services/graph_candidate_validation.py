from __future__ import annotations

import uuid
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_candidate_evidence import GraphEntityCandidateEvidence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict
from app.services.graph_candidate_aggregation import (
    canonical_graph_value_hash_v1,
    conflict_key_v1,
    merge_candidate_key_v1,
)
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.graph_schema_validator import (
    AttributeDefinitionRule,
    EntityTypeRule,
    RelationConstraintRule,
    RelationTypeRule,
    validate_entity_shape,
    validate_relation_shape,
)


_ATTRIBUTE_VALUE_TYPES = frozenset(
    {"string", "text", "integer", "number", "boolean", "date", "datetime", "enum", "json"}
)
_RELATION_DIRECTIONS = frozenset({"directed", "undirected"})
_REVIEW_POLICIES = frozenset({"auto_active", "pending_review", "manual_only"})
_CARDINALITIES = frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"})
_REUSABLE_ENTITY_STATUSES = frozenset({"draft", "pending_review", "active"})
_NORMALIZATION_SCORES = {
    "exact_normalized_match": 1.0,
    "exact_alias_match": 0.95,
    "new_entity": 0.9,
    "ambiguous": 0.0,
}


class OntologySnapshotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class OntologyRuleSet:
    ontology_version_id: uuid.UUID
    entity_types_by_key: Mapping[str, EntityTypeRule]
    entity_types_by_id: Mapping[uuid.UUID, EntityTypeRule]
    relation_types_by_key: Mapping[str, RelationTypeRule]
    constraints: Mapping[tuple[uuid.UUID, uuid.UUID, uuid.UUID], RelationConstraintRule]


@dataclass(frozen=True)
class EntityMatchInput:
    entity_id: uuid.UUID
    entity_type_id: uuid.UUID
    status: str
    via_normalized_name: bool = False
    via_active_alias: bool = False


@dataclass(frozen=True)
class EntityMatchDecision:
    matched_entity_id: uuid.UUID | None
    normalization_method: str
    normalization_score: float
    ambiguity_reason: str | None
    suggested_target_ids: tuple[uuid.UUID, ...]
    ineligible_statuses: tuple[tuple[uuid.UUID, str], ...] = ()


@dataclass(frozen=True)
class JobCandidateValidationResult:
    entity_candidate_count: int
    relation_candidate_count: int
    rejected_count: int
    pending_review_count: int
    merge_candidate_count: int
    conflict_count: int


def _expect_object(
    value: Any,
    *,
    label: str,
    keys: set[str],
    optional_keys: set[str] | None = None,
) -> dict[str, Any]:
    allowed_keys = keys | (optional_keys or set())
    actual_keys = set(value) if isinstance(value, dict) else set()
    if not isinstance(value, dict) or not keys <= actual_keys <= allowed_keys:
        if optional_keys:
            detail = f"exactly required keys {sorted(keys)} and only optional keys {sorted(allowed_keys - keys)}"
        else:
            detail = f"exactly {sorted(keys)}"
        raise OntologySnapshotError(
            "invalid_ontology_snapshot",
            f"{label} must contain {detail}",
        )
    return value


def _expect_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be an array")
    return value


def _parse_uuid(value: Any, *, label: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be a UUID") from exc


def _expect_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be a non-empty string")
    return value


def _expect_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be a boolean")
    return value


def _expect_optional_object(value: Any, *, label: str) -> dict[str, Any] | None:
    if value is not None and not isinstance(value, dict):
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be an object or null")
    return value


def _parse_attribute_rules(value: Any, *, label: str) -> tuple[AttributeDefinitionRule, ...]:
    rules: list[AttributeDefinitionRule] = []
    seen: set[str] = set()
    for index, raw in enumerate(_expect_list(value, label=label)):
        item = _expect_object(
            raw,
            label=f"{label}[{index}]",
            keys={"key", "value_type", "required", "enum_values", "validation_schema"},
        )
        key = _expect_string(item["key"], label=f"{label}[{index}].key")
        if key in seen:
            raise OntologySnapshotError("invalid_ontology_snapshot", f"duplicate Attribute key: {key}")
        seen.add(key)
        value_type = _expect_string(item["value_type"], label=f"{label}[{index}].value_type")
        if value_type not in _ATTRIBUTE_VALUE_TYPES:
            raise OntologySnapshotError(
                "invalid_ontology_snapshot", f"unsupported Attribute value_type: {value_type}"
            )
        enum_values = item["enum_values"]
        if enum_values is not None and not isinstance(enum_values, list):
            raise OntologySnapshotError(
                "invalid_ontology_snapshot", f"{label}[{index}].enum_values must be an array or null"
            )
        validation_schema = _expect_optional_object(
            item["validation_schema"], label=f"{label}[{index}].validation_schema"
        )
        rules.append(
            AttributeDefinitionRule(
                key=key,
                value_type=value_type,
                required=_expect_bool(item["required"], label=f"{label}[{index}].required"),
                enum_values=tuple(enum_values) if enum_values is not None else None,
                validation_schema=validation_schema,
            )
        )
    if [rule.key for rule in rules] != sorted(rule.key for rule in rules):
        raise OntologySnapshotError("invalid_ontology_snapshot", f"{label} must be sorted by key")
    return tuple(rules)


def load_ontology_rule_set_v1(job: Any) -> OntologyRuleSet:
    if getattr(job, "normalization_rule_version", None) != "normalization_v1":
        raise OntologySnapshotError(
            "unsupported_normalization_rule", "Job normalization rule must be normalization_v1"
        )
    if getattr(job, "confidence_policy_version", None) != "v1":
        raise OntologySnapshotError("unsupported_confidence_policy", "Job confidence policy must be v1")
    snapshot = _expect_object(
        getattr(job, "ontology_snapshot", None),
        label="ontology_snapshot",
        keys={"ontology_version_id", "entity_types", "relation_types", "relation_constraints"},
        optional_keys={"schema_state", "confirmed", "source_hash", "origin"},
    )
    expected_hash = canonical_graph_value_hash_v1(snapshot)
    if getattr(job, "ontology_snapshot_hash", None) != expected_hash:
        raise OntologySnapshotError(
            "ontology_snapshot_hash_mismatch", "Ontology Snapshot hash does not match its payload"
        )
    ontology_version_id = _parse_uuid(snapshot["ontology_version_id"], label="ontology_version_id")
    if ontology_version_id != getattr(job, "ontology_version_id", None):
        raise OntologySnapshotError(
            "ontology_snapshot_scope_mismatch",
            "Ontology Snapshot version does not match the Job",
        )

    entity_types_by_key: dict[str, EntityTypeRule] = {}
    entity_types_by_id: dict[uuid.UUID, EntityTypeRule] = {}
    entity_sort_keys: list[tuple[str, str]] = []
    for index, raw in enumerate(_expect_list(snapshot["entity_types"], label="entity_types")):
        item = _expect_object(
            raw,
            label=f"entity_types[{index}]",
            keys={"id", "key", "properties_schema", "active_attribute_definitions"},
            optional_keys={"label", "description"},
        )
        for metadata_key in ("label", "description"):
            if item.get(metadata_key) is not None:
                _expect_string(
                    item[metadata_key],
                    label=f"entity_types[{index}].{metadata_key}",
                )
        type_id = _parse_uuid(item["id"], label=f"entity_types[{index}].id")
        key = _expect_string(item["key"], label=f"entity_types[{index}].key")
        if type_id in entity_types_by_id or key in entity_types_by_key:
            raise OntologySnapshotError(
                "invalid_ontology_snapshot", "Entity Type IDs and keys must be unique"
            )
        rule = EntityTypeRule(
            id=type_id,
            ontology_version_id=ontology_version_id,
            key=key,
            properties_schema=_expect_optional_object(
                item["properties_schema"], label=f"entity_types[{index}].properties_schema"
            ),
            active_attribute_definitions=_parse_attribute_rules(
                item["active_attribute_definitions"],
                label=f"entity_types[{index}].active_attribute_definitions",
            ),
        )
        entity_types_by_key[key] = rule
        entity_types_by_id[type_id] = rule
        entity_sort_keys.append((key, str(type_id)))
    if entity_sort_keys != sorted(entity_sort_keys):
        raise OntologySnapshotError("invalid_ontology_snapshot", "entity_types must be sorted by key and id")

    relation_types_by_key: dict[str, RelationTypeRule] = {}
    relation_types_by_id: dict[uuid.UUID, RelationTypeRule] = {}
    relation_sort_keys: list[tuple[str, str]] = []
    for index, raw in enumerate(_expect_list(snapshot["relation_types"], label="relation_types")):
        item = _expect_object(
            raw,
            label=f"relation_types[{index}]",
            keys={
                "id",
                "key",
                "direction",
                "requires_evidence",
                "default_review_policy",
                "properties_schema",
                "active_attribute_definitions",
            },
            optional_keys={"label", "description"},
        )
        for metadata_key in ("label", "description"):
            if item.get(metadata_key) is not None:
                _expect_string(
                    item[metadata_key],
                    label=f"relation_types[{index}].{metadata_key}",
                )
        type_id = _parse_uuid(item["id"], label=f"relation_types[{index}].id")
        key = _expect_string(item["key"], label=f"relation_types[{index}].key")
        if type_id in relation_types_by_id or key in relation_types_by_key:
            raise OntologySnapshotError(
                "invalid_ontology_snapshot", "Relation Type IDs and keys must be unique"
            )
        direction = _expect_string(item["direction"], label=f"relation_types[{index}].direction")
        review_policy = _expect_string(
            item["default_review_policy"],
            label=f"relation_types[{index}].default_review_policy",
        )
        if direction not in _RELATION_DIRECTIONS or review_policy not in _REVIEW_POLICIES:
            raise OntologySnapshotError("invalid_ontology_snapshot", "Relation Type enum value is invalid")
        rule = RelationTypeRule(
            id=type_id,
            ontology_version_id=ontology_version_id,
            key=key,
            direction=direction,
            requires_evidence=_expect_bool(
                item["requires_evidence"],
                label=f"relation_types[{index}].requires_evidence",
            ),
            default_review_policy=review_policy,
            properties_schema=_expect_optional_object(
                item["properties_schema"],
                label=f"relation_types[{index}].properties_schema",
            ),
            active_attribute_definitions=_parse_attribute_rules(
                item["active_attribute_definitions"],
                label=f"relation_types[{index}].active_attribute_definitions",
            ),
        )
        relation_types_by_key[key] = rule
        relation_types_by_id[type_id] = rule
        relation_sort_keys.append((key, str(type_id)))
    if relation_sort_keys != sorted(relation_sort_keys):
        raise OntologySnapshotError(
            "invalid_ontology_snapshot", "relation_types must be sorted by key and id"
        )

    constraints: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], RelationConstraintRule] = {}
    constraint_sort_keys: list[tuple[str, str, str]] = []
    for index, raw in enumerate(_expect_list(snapshot["relation_constraints"], label="relation_constraints")):
        item = _expect_object(
            raw,
            label=f"relation_constraints[{index}]",
            keys={
                "relation_type_id",
                "source_entity_type_id",
                "target_entity_type_id",
                "cardinality",
                "requires_review",
            },
        )
        relation_type_id = _parse_uuid(
            item["relation_type_id"],
            label=f"relation_constraints[{index}].relation_type_id",
        )
        source_type_id = _parse_uuid(
            item["source_entity_type_id"],
            label=f"relation_constraints[{index}].source_entity_type_id",
        )
        target_type_id = _parse_uuid(
            item["target_entity_type_id"],
            label=f"relation_constraints[{index}].target_entity_type_id",
        )
        if (
            relation_type_id not in relation_types_by_id
            or source_type_id not in entity_types_by_id
            or target_type_id not in entity_types_by_id
        ):
            raise OntologySnapshotError("invalid_ontology_snapshot", "Constraint references an unknown Type")
        cardinality = item["cardinality"]
        if cardinality is not None and cardinality not in _CARDINALITIES:
            raise OntologySnapshotError("invalid_ontology_snapshot", "Constraint cardinality is invalid")
        constraint_key = (relation_type_id, source_type_id, target_type_id)
        if constraint_key in constraints:
            raise OntologySnapshotError("invalid_ontology_snapshot", "Constraint scope must be unique")
        constraints[constraint_key] = RelationConstraintRule(
            source_entity_type_id=source_type_id,
            target_entity_type_id=target_type_id,
            cardinality=cardinality,
            requires_review=_expect_bool(
                item["requires_review"],
                label=f"relation_constraints[{index}].requires_review",
            ),
        )
        constraint_sort_keys.append(tuple(str(value) for value in constraint_key))
    if constraint_sort_keys != sorted(constraint_sort_keys):
        raise OntologySnapshotError(
            "invalid_ontology_snapshot",
            "relation_constraints must be sorted by relation/source/target IDs",
        )
    return OntologyRuleSet(
        ontology_version_id=ontology_version_id,
        entity_types_by_key=MappingProxyType(entity_types_by_key),
        entity_types_by_id=MappingProxyType(entity_types_by_id),
        relation_types_by_key=MappingProxyType(relation_types_by_key),
        constraints=MappingProxyType(constraints),
    )


def classify_entity_matches(
    *,
    required_entity_type_id: uuid.UUID,
    matches: Iterable[EntityMatchInput],
) -> EntityMatchDecision:
    merged: dict[uuid.UUID, EntityMatchInput] = {}
    for item in matches:
        current = merged.get(item.entity_id)
        if current is None:
            merged[item.entity_id] = item
            continue
        merged[item.entity_id] = EntityMatchInput(
            entity_id=item.entity_id,
            entity_type_id=item.entity_type_id,
            status=item.status,
            via_normalized_name=current.via_normalized_name or item.via_normalized_name,
            via_active_alias=current.via_active_alias or item.via_active_alias,
        )
    ordered = sorted(merged.values(), key=lambda item: str(item.entity_id))
    if not ordered:
        return EntityMatchDecision(None, "new_entity", 0.9, None, ())

    wrong_type = [item for item in ordered if item.entity_type_id != required_entity_type_id]
    ineligible = [item for item in ordered if item.status not in _REUSABLE_ENTITY_STATUSES]
    eligible = [
        item
        for item in ordered
        if item.entity_type_id == required_entity_type_id and item.status in _REUSABLE_ENTITY_STATUSES
    ]
    if not wrong_type and not ineligible and len(eligible) == 1:
        match = eligible[0]
        method = "exact_normalized_match" if match.via_normalized_name else "exact_alias_match"
        return EntityMatchDecision(
            match.entity_id,
            method,
            _NORMALIZATION_SCORES[method],
            None,
            (match.entity_id,),
        )

    if ineligible:
        reason = "entity_status_conflict"
    elif wrong_type:
        reason = "entity_type_mismatch"
    else:
        reason = "entity_merge_ambiguity"
    return EntityMatchDecision(
        None,
        "ambiguous",
        0.0,
        reason,
        tuple(item.entity_id for item in ordered),
        tuple((item.entity_id, item.status) for item in ineligible),
    )


async def _load_entity_matches(
    db,
    *,
    job: Any,
    candidate: GraphEntityCandidate,
    entity_type: EntityTypeRule,
) -> list[EntityMatchInput]:
    short_identifier = re.fullmatch(
        r"[a-z]{1,8}-[a-z0-9]{1,16}", candidate.normalized_name
    )
    if short_identifier is not None:
        evidence_result = await db.execute(
            select(GraphEntityCandidateEvidence.id)
            .where(
                GraphEntityCandidateEvidence.candidate_id == candidate.id,
                GraphEntityCandidateEvidence.job_id == job.id,
                GraphEntityCandidateEvidence.resolved_document_id == job.document_id,
                GraphEntityCandidateEvidence.validation_status == "valid",
                GraphEntityCandidateEvidence.purged_at.is_(None),
            )
            .limit(1)
        )
        if evidence_result.scalar_one_or_none() is None:
            return []
    name_result = await db.execute(
        select(Entity).where(
            Entity.library_id == job.library_id,
            Entity.ontology_version_id == job.ontology_version_id,
            Entity.entity_type_id == entity_type.id,
            Entity.normalized_name == candidate.normalized_name,
        )
    )
    matches = [
        EntityMatchInput(
            entity_id=row.id,
            entity_type_id=row.entity_type_id,
            status=row.status,
            via_normalized_name=True,
        )
        for row in name_result.scalars().all()
    ]
    alias_result = await db.execute(
        select(EntityAlias, Entity)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(
            EntityAlias.library_id == job.library_id,
            EntityAlias.status == "active",
            EntityAlias.normalized_alias == candidate.normalized_name,
            Entity.library_id == job.library_id,
            Entity.ontology_version_id == job.ontology_version_id,
        )
    )
    for alias, entity in alias_result.all():
        if normalize_graph_name_v1(alias.alias) != candidate.normalized_name:
            continue
        matches.append(
            EntityMatchInput(
                entity_id=entity.id,
                entity_type_id=entity.entity_type_id,
                status=entity.status,
                via_active_alias=True,
            )
        )
    return matches


async def _upsert_merge_candidates(
    db,
    *,
    job: Any,
    candidate: GraphEntityCandidate,
    decision: EntityMatchDecision,
) -> int:
    reason = decision.ambiguity_reason or "entity_merge_ambiguity"
    expected = {
        merge_candidate_key_v1(
            job_id=job.id,
            entity_candidate_id=candidate.id,
            suggested_target_entity_id=target_id,
            reason=reason,
        ): target_id
        for target_id in decision.suggested_target_ids
        if decision.ambiguity_reason is not None
    }
    result = await db.execute(
        select(GraphEntityMergeCandidate)
        .where(
            GraphEntityMergeCandidate.job_id == job.id,
            GraphEntityMergeCandidate.entity_candidate_id == candidate.id,
        )
        .with_for_update()
    )
    existing = {row.merge_key: row for row in result.scalars().all()}
    for merge_key, row in existing.items():
        if merge_key not in expected and row.purged_at is None:
            row.status = "superseded"
    for merge_key, target_id in expected.items():
        row = existing.get(merge_key)
        if row is None:
            await db.execute(
                pg_insert(GraphEntityMergeCandidate)
                .values(
                    id=uuid.uuid4(),
                    job_id=job.id,
                    library_id=job.library_id,
                    entity_candidate_id=candidate.id,
                    suggested_target_entity_id=target_id,
                    merge_key=merge_key,
                    reason=reason,
                    status="pending_review",
                    details={"target_entity_id": str(target_id)},
                )
                .on_conflict_do_nothing(index_elements=["job_id", "merge_key"])
            )
        elif row.purged_at is None:
            if (
                row.library_id != job.library_id
                or row.suggested_target_entity_id != target_id
                or row.reason != reason
            ):
                raise ValueError("existing Merge Candidate does not match its stable identity")
            row.status = "pending_review"
            row.details = {"target_entity_id": str(target_id)}
    return len(expected)


async def _upsert_entity_match_conflict(
    db,
    *,
    job: Any,
    candidate: GraphEntityCandidate,
    decision: EntityMatchDecision,
) -> int:
    candidate_id = str(candidate.id)
    result = await db.execute(
        select(GraphExtractionConflict)
        .where(
            GraphExtractionConflict.job_id == job.id,
            GraphExtractionConflict.conflict_type.in_({"entity_merge_ambiguity", "entity_status_conflict"}),
        )
        .with_for_update()
    )
    existing_rows = [row for row in result.scalars().all() if candidate_id in row.entity_candidate_ids]

    expected: tuple[str, str, list[dict[str, Any]], dict[str, Any]] | None = None
    if decision.ambiguity_reason is not None:
        conflict_type = (
            "entity_status_conflict"
            if decision.ambiguity_reason == "entity_status_conflict"
            else "entity_merge_ambiguity"
        )
        if conflict_type == "entity_status_conflict":
            values = [status for _, status in decision.ineligible_statuses]
            field_name = "entity.status"
            details: dict[str, Any] = {
                "targets": [
                    {"entity_id": str(entity_id), "status": status}
                    for entity_id, status in decision.ineligible_statuses
                ]
            }
        else:
            values = [str(value) for value in decision.suggested_target_ids]
            field_name = "matched_entity_id"
            details = {"target_entity_ids": values}
        fields = [
            {
                "field": field_name,
                "value_hashes": sorted({canonical_graph_value_hash_v1(value) for value in values}),
            }
        ]
        key = conflict_key_v1(
            job_id=job.id,
            conflict_type=conflict_type,
            entity_candidate_ids=[candidate.id],
            relation_candidate_ids=[],
            conflicting_fields=fields,
        )
        expected = key, conflict_type, fields, details

    expected_key = expected[0] if expected is not None else None
    for row in existing_rows:
        if row.conflict_key != expected_key and row.purged_at is None:
            row.status = "superseded"
    if expected is None:
        return 0

    key, conflict_type, fields, details = expected
    existing = next((row for row in existing_rows if row.conflict_key == key), None)
    if existing is None:
        await db.execute(
            pg_insert(GraphExtractionConflict)
            .values(
                id=uuid.uuid4(),
                job_id=job.id,
                library_id=job.library_id,
                conflict_key=key,
                conflict_type=conflict_type,
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
            or existing.conflict_type != conflict_type
            or existing.entity_candidate_ids != [candidate_id]
            or existing.relation_candidate_ids != []
            or existing.conflicting_fields != fields
        ):
            raise ValueError("existing Conflict does not match its stable identity")
        existing.status = "open"
        existing.details = details
    return 1


async def _open_entity_conflicts(db, *, job: Any, candidate: GraphEntityCandidate):
    result = await db.execute(
        select(GraphExtractionConflict).where(
            GraphExtractionConflict.job_id == job.id,
            GraphExtractionConflict.status == "open",
            GraphExtractionConflict.purged_at.is_(None),
        )
    )
    candidate_id = str(candidate.id)
    return sorted(
        (row for row in result.scalars().all() if candidate_id in row.entity_candidate_ids),
        key=lambda row: (row.conflict_type, row.conflict_key),
    )


def _candidate_error(code: str, *, field: str | None = None, message: str | None = None):
    value = {"code": code}
    if field is not None:
        value["field"] = field
    if message is not None:
        value["message"] = message
    return value


async def validate_job_candidates(db, *, job: Any) -> JobCandidateValidationResult:
    rules = load_ontology_rule_set_v1(job)
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
    entity_by_id = {candidate.id: candidate for candidate in entity_candidates}
    merge_count = 0
    conflict_count = 0

    for candidate in entity_candidates:
        entity_type = rules.entity_types_by_key.get(candidate.entity_type_key)
        if entity_type is None:
            candidate.schema_validation_score = 0.0
            candidate.status = "rejected"
            candidate.review_reason = "schema_extension_candidate"
            candidate.validation_errors = [
                _candidate_error(
                    "schema_extension_candidate",
                    field="entity_type_key",
                    message=(
                        "The proposed entity type is not declared by the frozen Schema: "
                        f"{candidate.entity_type_key}"
                    ),
                )
            ]
            candidate.normalization_method = "ambiguous"
            continue
        try:
            shape = validate_entity_shape(
                entity_type=entity_type,
                canonical_name=candidate.canonical_name,
                properties=candidate.proposed_properties,
            )
            if shape.normalized_name != candidate.normalized_name:
                raise ValueError("Candidate normalized name does not match shape validation")
        except (TypeError, ValueError) as exc:
            candidate.schema_validation_score = 0.0
            candidate.status = "rejected"
            candidate.review_reason = "schema_invalid"
            candidate.validation_errors = [_candidate_error("entity_shape_invalid", message=str(exc))]
            candidate.normalization_method = "ambiguous"
            continue

        candidate.schema_validation_score = 1.0
        match_inputs = await _load_entity_matches(db, job=job, candidate=candidate, entity_type=entity_type)
        decision = classify_entity_matches(required_entity_type_id=entity_type.id, matches=match_inputs)
        candidate.matched_entity_id = decision.matched_entity_id
        candidate.normalization_method = decision.normalization_method
        merge_count += await _upsert_merge_candidates(db, job=job, candidate=candidate, decision=decision)
        conflict_count += await _upsert_entity_match_conflict(
            db, job=job, candidate=candidate, decision=decision
        )
        if decision.ambiguity_reason is not None:
            candidate.status = "pending_review"
            candidate.review_reason = "entity_match_ambiguous"
            candidate.validation_errors = [
                _candidate_error(decision.ambiguity_reason, field="matched_entity_id")
            ]
        else:
            open_conflicts = await _open_entity_conflicts(db, job=job, candidate=candidate)
            if open_conflicts:
                candidate.status = "pending_review"
                candidate.review_reason = (
                    "property_conflict"
                    if any(row.conflict_type == "property_conflict" for row in open_conflicts)
                    else "conflict_open"
                )
                candidate.validation_errors = sorted(
                    [
                        _candidate_error(
                            row.conflict_type,
                            field=field.get("field"),
                        )
                        for row in open_conflicts
                        for field in row.conflicting_fields
                    ],
                    key=lambda item: (item["code"], item.get("field") or ""),
                )
            else:
                candidate.status = "aggregated"
                candidate.review_reason = None
                candidate.validation_errors = []

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
        relation_type = rules.relation_types_by_key.get(candidate.relation_type_key)
        source = entity_by_id.get(candidate.source_candidate_id)
        target = entity_by_id.get(candidate.target_candidate_id)
        source_type = rules.entity_types_by_key.get(source.entity_type_key) if source is not None else None
        target_type = rules.entity_types_by_key.get(target.entity_type_key) if target is not None else None
        if relation_type is None or source_type is None or target_type is None:
            candidate.ontology_validation_status = "invalid"
            candidate.schema_validation_score = 0.0
            candidate.normalization_score = 0.0
            candidate.status = "rejected"
            candidate.review_reason = "schema_extension_candidate"
            candidate.validation_errors = [
                _candidate_error(
                    "schema_extension_candidate",
                    field="relation_type_key",
                    message="The proposed relation or endpoint type is not declared by the frozen Schema",
                )
            ]
            continue
        constraint = rules.constraints.get((relation_type.id, source_type.id, target_type.id))
        try:
            shape = validate_relation_shape(
                relation_type=relation_type,
                constraint=constraint,
                source_entity_type_id=source_type.id,
                target_entity_type_id=target_type.id,
                properties=candidate.proposed_properties,
            )
        except (TypeError, ValueError) as exc:
            candidate.ontology_validation_status = "invalid"
            candidate.schema_validation_score = 0.0
            candidate.status = "rejected"
            candidate.review_reason = "schema_invalid"
            candidate.validation_errors = [_candidate_error("relation_shape_invalid", message=str(exc))]
            continue
        source_score = _NORMALIZATION_SCORES.get(source.normalization_method or "ambiguous", 0.0)
        target_score = _NORMALIZATION_SCORES.get(target.normalization_method or "ambiguous", 0.0)
        candidate.normalization_score = min(source_score, target_score)
        if not shape.valid:
            candidate.ontology_validation_status = "invalid"
            candidate.schema_validation_score = 0.0
            candidate.status = "rejected"
            candidate.review_reason = "schema_invalid"
            candidate.validation_errors = [
                _candidate_error("relation_constraint_invalid", message=shape.reasons[0])
            ]
        elif not shape.schema_boundary_clear:
            candidate.ontology_validation_status = "boundary_unclear"
            candidate.schema_validation_score = 0.4
            candidate.status = "pending_review"
            candidate.review_reason = "schema_boundary_unclear"
            candidate.validation_errors = [_candidate_error("schema_boundary_unclear")]
        else:
            candidate.ontology_validation_status = "warning" if shape.requires_review else "valid"
            candidate.schema_validation_score = 0.6 if shape.requires_review else 1.0
            if not candidate.has_conflict:
                candidate.status = "aggregated"
                candidate.review_reason = None
                candidate.validation_errors = []

    await db.flush()
    all_candidates: list[Any] = [*entity_candidates, *relation_candidates]
    return JobCandidateValidationResult(
        entity_candidate_count=len(entity_candidates),
        relation_candidate_count=len(relation_candidates),
        rejected_count=sum(item.status == "rejected" for item in all_candidates),
        pending_review_count=sum(item.status == "pending_review" for item in all_candidates),
        merge_candidate_count=merge_count,
        conflict_count=conflict_count,
    )
