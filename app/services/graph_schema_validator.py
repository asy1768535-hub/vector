from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import (
    ATTRIBUTE_OWNER_ENTITY_TYPE,
    ATTRIBUTE_OWNER_RELATION_TYPE,
    AttributeDefinition,
)
from app.models.entity import (
    GRAPH_FACT_STATUS_ACTIVE,
    GRAPH_FACT_STATUS_DRAFT,
    GRAPH_FACT_STATUS_PENDING_REVIEW,
    GRAPH_FACT_STATUS_REJECTED,
    GRAPH_SOURCE_EXTRACTED,
    GRAPH_SOURCE_IMPORTED,
    GRAPH_SOURCE_MANUAL,
    Entity,
)
from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import (
    REVIEW_POLICY_AUTO_ACTIVE,
    REVIEW_POLICY_MANUAL_ONLY,
    REVIEW_POLICY_PENDING_REVIEW,
    RelationType,
)
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_normalization import normalize_graph_name_v1


SCHEMA_STATUS_ACTIVE = "active"
RELATION_TYPE_RELATED_TO = "related_to"
RELATION_EVIDENCE_STATUS_ACTIVE = "active"
REVIEW_STATUS_PENDING_REVIEW = "pending_review"
REVIEW_STATUS_NOT_REQUIRED = "not_required"
ACTIVE_CONFIDENCE_THRESHOLD = 0.8

GRAPH_FACT_WRITE_STATUSES = {
    GRAPH_FACT_STATUS_DRAFT,
    GRAPH_FACT_STATUS_PENDING_REVIEW,
    GRAPH_FACT_STATUS_ACTIVE,
    GRAPH_FACT_STATUS_REJECTED,
}
GRAPH_SOURCE_TYPES = {
    GRAPH_SOURCE_MANUAL,
    GRAPH_SOURCE_IMPORTED,
    GRAPH_SOURCE_EXTRACTED,
}


@dataclass(frozen=True)
class AttributeDefinitionRule:
    key: str
    value_type: str
    required: bool
    enum_values: tuple[Any, ...] | None = None
    validation_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class EntityTypeRule:
    id: uuid.UUID
    ontology_version_id: uuid.UUID
    key: str
    properties_schema: dict[str, Any] | None
    active_attribute_definitions: tuple[AttributeDefinitionRule, ...] = field(
        default_factory=tuple
    )


@dataclass(frozen=True)
class RelationTypeRule:
    id: uuid.UUID
    ontology_version_id: uuid.UUID
    key: str
    direction: str
    requires_evidence: bool
    default_review_policy: str
    properties_schema: dict[str, Any] | None
    active_attribute_definitions: tuple[AttributeDefinitionRule, ...] = field(
        default_factory=tuple
    )


@dataclass(frozen=True)
class RelationConstraintRule:
    source_entity_type_id: uuid.UUID
    target_entity_type_id: uuid.UUID
    cardinality: str | None
    requires_review: bool


@dataclass(frozen=True)
class EntityShapeValidation:
    normalized_name: str
    validated_properties: dict[str, Any]
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RelationShapeValidation:
    valid: bool
    schema_boundary_clear: bool
    requires_review: bool
    validated_properties: dict[str, Any]
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EntityWriteValidation:
    ontology_version: OntologyVersion
    entity_type: EntityType
    canonical_name: str
    normalized_name: str
    properties: dict[str, Any]
    status: str
    source_type: str
    confidence: float | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RelationWriteValidation:
    relation_type: RelationType
    source_entity: Entity
    target_entity: Entity
    constraint: RelationTypeConstraint | None
    properties: dict[str, Any]
    status: str
    review_status: str | None
    source_type: str
    confidence: float | None = None
    active_evidence_count: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)


def normalize_graph_name(value: str) -> str:
    return normalize_graph_name_v1(value)


def validate_entity_shape(
    *,
    entity_type: EntityTypeRule,
    canonical_name: str,
    properties: dict[str, Any],
) -> EntityShapeValidation:
    normalized_name = normalize_graph_name_v1(canonical_name)
    if not normalized_name:
        raise ValueError("canonical_name must be non-empty")
    props = _coerce_properties(properties)
    _validate_properties_schema(props, entity_type.properties_schema, "entity properties")
    _validate_attribute_definitions(props, entity_type.active_attribute_definitions)
    return EntityShapeValidation(
        normalized_name=normalized_name,
        validated_properties=deepcopy(props),
    )


def validate_relation_shape(
    *,
    relation_type: RelationTypeRule,
    constraint: RelationConstraintRule | None,
    source_entity_type_id: uuid.UUID,
    target_entity_type_id: uuid.UUID,
    properties: dict[str, Any],
) -> RelationShapeValidation:
    props = _coerce_properties(properties)
    _validate_properties_schema(
        props, relation_type.properties_schema, "relation properties"
    )
    _validate_attribute_definitions(props, relation_type.active_attribute_definitions)

    if constraint is None:
        if relation_type.key == RELATION_TYPE_RELATED_TO:
            return RelationShapeValidation(
                valid=True,
                schema_boundary_clear=False,
                requires_review=True,
                validated_properties=deepcopy(props),
                reasons=("related_to schema boundary is unclear",),
            )
        return RelationShapeValidation(
            valid=False,
            schema_boundary_clear=False,
            requires_review=True,
            validated_properties=deepcopy(props),
            reasons=("active relation_type_constraint not found",),
        )

    if (
        constraint.source_entity_type_id != source_entity_type_id
        or constraint.target_entity_type_id != target_entity_type_id
    ):
        return RelationShapeValidation(
            valid=False,
            schema_boundary_clear=False,
            requires_review=True,
            validated_properties=deepcopy(props),
            reasons=("relation endpoint types do not match constraint",),
        )

    requires_review = (
        constraint.requires_review
        or relation_type.key == RELATION_TYPE_RELATED_TO
        or relation_type.default_review_policy != REVIEW_POLICY_AUTO_ACTIVE
    )
    reasons: list[str] = []
    if constraint.requires_review:
        reasons.append("relation type constraint requires review")
    if relation_type.key == RELATION_TYPE_RELATED_TO:
        reasons.append("related_to requires review")
    if relation_type.default_review_policy != REVIEW_POLICY_AUTO_ACTIVE:
        reasons.append("relation type review policy requires review")
    return RelationShapeValidation(
        valid=True,
        schema_boundary_clear=True,
        requires_review=requires_review,
        validated_properties=deepcopy(props),
        reasons=tuple(reasons),
    )


async def validate_entity_write(
    db: AsyncSession,
    library: Library,
    *,
    ontology_version_id: uuid.UUID,
    entity_type_id: uuid.UUID,
    canonical_name: str,
    properties: dict[str, Any] | None = None,
    requested_status: str = GRAPH_FACT_STATUS_DRAFT,
    source_type: str = GRAPH_SOURCE_MANUAL,
    confidence: float | None = None,
    exclude_entity_id: uuid.UUID | None = None,
) -> EntityWriteValidation:
    ontology_version = await _get_scoped_active_ontology(db, library, ontology_version_id)
    entity_type = await _get_active_entity_type(db, library, ontology_version.id, entity_type_id)
    _require_graph_fact_status(requested_status)
    _require_source_type(source_type)
    props = _coerce_properties(properties)
    attribute_definitions = await _list_active_attribute_definitions(
        db,
        library,
        ontology_version.id,
        owner_kind=ATTRIBUTE_OWNER_ENTITY_TYPE,
        owner_type_id=entity_type.id,
    )
    shape = validate_entity_shape(
        entity_type=_entity_type_rule(entity_type, attribute_definitions),
        canonical_name=canonical_name,
        properties=props,
    )
    await _reject_duplicate_active_entity(
        db,
        library,
        ontology_version.id,
        entity_type.id,
        shape.normalized_name,
        exclude_entity_id=exclude_entity_id,
    )

    return EntityWriteValidation(
        ontology_version=ontology_version,
        entity_type=entity_type,
        canonical_name=canonical_name.strip(),
        normalized_name=shape.normalized_name,
        properties=shape.validated_properties,
        status=requested_status,
        source_type=source_type,
        confidence=confidence,
    )


async def validate_relation_write(
    db: AsyncSession,
    library: Library,
    *,
    relation_type_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
    properties: dict[str, Any] | None = None,
    requested_status: str = GRAPH_FACT_STATUS_DRAFT,
    source_type: str = GRAPH_SOURCE_MANUAL,
    confidence: float | None = None,
    schema_boundary_clear: bool = True,
    active_evidence_count: int | None = None,
    write_mode: Literal["create", "activate"] = "create",
    relation_id: uuid.UUID | None = None,
) -> RelationWriteValidation:
    _require_graph_fact_status(requested_status)
    _require_source_type(source_type)
    if write_mode not in {"create", "activate"}:
        raise ValueError("write_mode must be create or activate")

    source_entity = await _get_scoped_entity(db, library, source_entity_id, "source entity")
    target_entity = await _get_scoped_entity(db, library, target_entity_id, "target entity")
    if source_entity.ontology_version_id != target_entity.ontology_version_id:
        raise ValueError("source and target entities must belong to the same ontology")

    ontology_version = await _get_scoped_active_ontology(db, library, source_entity.ontology_version_id)
    relation_type = await _get_active_relation_type(db, library, ontology_version.id, relation_type_id)
    props = _coerce_properties(properties)
    attribute_definitions = await _list_active_attribute_definitions(
        db,
        library,
        ontology_version.id,
        owner_kind=ATTRIBUTE_OWNER_RELATION_TYPE,
        owner_type_id=relation_type.id,
    )
    constraint = await _find_active_relation_type_constraint(
        db,
        library,
        ontology_version.id,
        relation_type_id=relation_type.id,
        source_entity_type_id=source_entity.entity_type_id,
        target_entity_type_id=target_entity.entity_type_id,
    )
    shape = validate_relation_shape(
        relation_type=_relation_type_rule(relation_type, attribute_definitions),
        constraint=_relation_constraint_rule(constraint),
        source_entity_type_id=source_entity.entity_type_id,
        target_entity_type_id=target_entity.entity_type_id,
        properties=props,
    )
    if not shape.valid:
        raise ValueError(shape.reasons[0])

    evidence_count = await _resolve_active_relation_evidence_count(
        db,
        library,
        write_mode=write_mode,
        relation_id=relation_id,
        active_evidence_count=active_evidence_count,
        must_have_queryable_relation=relation_type.requires_evidence and requested_status == GRAPH_FACT_STATUS_ACTIVE,
    )
    status, review_status, reasons = _decide_relation_status(
        requested_status=requested_status,
        relation_type=relation_type,
        constraint=constraint,
        source_entity=source_entity,
        target_entity=target_entity,
        confidence=confidence,
        schema_boundary_clear=schema_boundary_clear,
        active_evidence_count=evidence_count,
    )

    return RelationWriteValidation(
        relation_type=relation_type,
        source_entity=source_entity,
        target_entity=target_entity,
        constraint=constraint,
        properties=shape.validated_properties,
        status=status,
        review_status=review_status,
        source_type=source_type,
        confidence=confidence,
        active_evidence_count=evidence_count,
        reasons=tuple(reasons),
    )


async def count_active_relation_evidence(
    db: AsyncSession,
    library: Library,
    relation_id: uuid.UUID,
) -> int:
    result = await db.execute(
        select(RelationEvidence).where(
            RelationEvidence.library_id == library.id,
            RelationEvidence.relation_id == relation_id,
            RelationEvidence.status == RELATION_EVIDENCE_STATUS_ACTIVE,
        )
    )
    return len(result.scalars().all())


async def _resolve_active_relation_evidence_count(
    db: AsyncSession,
    library: Library,
    *,
    write_mode: str,
    relation_id: uuid.UUID | None,
    active_evidence_count: int | None,
    must_have_queryable_relation: bool,
) -> int:
    if active_evidence_count is not None:
        if active_evidence_count < 0:
            raise ValueError("active_evidence_count cannot be negative")
        return active_evidence_count
    if relation_id is not None:
        return await count_active_relation_evidence(db, library, relation_id)
    if write_mode == "activate" and must_have_queryable_relation:
        raise ValueError("relation_id is required to validate relation activation evidence")
    return 0


def _decide_relation_status(
    *,
    requested_status: str,
    relation_type: RelationType,
    constraint: RelationTypeConstraint | None,
    source_entity: Entity,
    target_entity: Entity,
    confidence: float | None,
    schema_boundary_clear: bool,
    active_evidence_count: int,
) -> tuple[str, str | None, list[str]]:
    if requested_status != GRAPH_FACT_STATUS_ACTIVE:
        return requested_status, _review_status_for(requested_status), []

    reasons: list[str] = []
    if source_entity.status != GRAPH_FACT_STATUS_ACTIVE or target_entity.status != GRAPH_FACT_STATUS_ACTIVE:
        reasons.append("non-active entity cannot produce active relation")
    if relation_type.requires_evidence and active_evidence_count < 1:
        reasons.append("requires active evidence")
    if relation_type.key == RELATION_TYPE_RELATED_TO:
        reasons.append("related_to cannot be automatically active")
    if relation_type.default_review_policy == REVIEW_POLICY_PENDING_REVIEW:
        reasons.append("relation type review policy requires pending_review")
    if relation_type.default_review_policy == REVIEW_POLICY_MANUAL_ONLY:
        reasons.append("relation type review policy requires manual review")
    if constraint is not None and constraint.requires_review:
        reasons.append("relation type constraint requires review")
    if confidence is not None and confidence < ACTIVE_CONFIDENCE_THRESHOLD:
        reasons.append("low confidence cannot be active")
    if not schema_boundary_clear:
        reasons.append("schema boundary is unclear")

    if reasons:
        return GRAPH_FACT_STATUS_PENDING_REVIEW, REVIEW_STATUS_PENDING_REVIEW, reasons
    if relation_type.default_review_policy != REVIEW_POLICY_AUTO_ACTIVE:
        return GRAPH_FACT_STATUS_PENDING_REVIEW, REVIEW_STATUS_PENDING_REVIEW, [
            "relation type review policy does not allow auto_active"
        ]
    return GRAPH_FACT_STATUS_ACTIVE, REVIEW_STATUS_NOT_REQUIRED, []


def _review_status_for(status: str) -> str | None:
    if status == GRAPH_FACT_STATUS_PENDING_REVIEW:
        return REVIEW_STATUS_PENDING_REVIEW
    if status == GRAPH_FACT_STATUS_ACTIVE:
        return REVIEW_STATUS_NOT_REQUIRED
    return None


async def _get_scoped_active_ontology(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
) -> OntologyVersion:
    ontology_version = await db.get(OntologyVersion, ontology_version_id)
    if ontology_version is None or ontology_version.library_id != library.id:
        raise LookupError("ontology version not found")
    if ontology_version.status != ONTOLOGY_STATUS_ACTIVE:
        raise ValueError("ontology version must be active")
    return ontology_version


async def _get_active_entity_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    entity_type_id: uuid.UUID,
) -> EntityType:
    entity_type = await db.get(EntityType, entity_type_id)
    if entity_type is None or entity_type.library_id != library.id:
        raise LookupError("entity type not found")
    _require_same_ontology(entity_type, ontology_version_id, "entity type")
    if entity_type.status != SCHEMA_STATUS_ACTIVE:
        raise ValueError("entity type must be active")
    return entity_type


async def _get_active_relation_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    relation_type_id: uuid.UUID,
) -> RelationType:
    relation_type = await db.get(RelationType, relation_type_id)
    if relation_type is None or relation_type.library_id != library.id:
        raise LookupError("relation type not found")
    _require_same_ontology(relation_type, ontology_version_id, "relation type")
    if relation_type.status != SCHEMA_STATUS_ACTIVE:
        raise ValueError("relation type must be active")
    return relation_type


async def _get_scoped_entity(
    db: AsyncSession,
    library: Library,
    entity_id: uuid.UUID,
    label: str,
) -> Entity:
    entity = await db.get(Entity, entity_id)
    if entity is None or entity.library_id != library.id:
        raise LookupError(f"{label} not found")
    return entity


async def _find_active_relation_type_constraint(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    relation_type_id: uuid.UUID,
    source_entity_type_id: uuid.UUID,
    target_entity_type_id: uuid.UUID,
) -> RelationTypeConstraint | None:
    result = await db.execute(
        select(RelationTypeConstraint)
        .where(
            RelationTypeConstraint.library_id == library.id,
            RelationTypeConstraint.ontology_version_id == ontology_version_id,
            RelationTypeConstraint.relation_type_id == relation_type_id,
            RelationTypeConstraint.source_entity_type_id == source_entity_type_id,
            RelationTypeConstraint.target_entity_type_id == target_entity_type_id,
            RelationTypeConstraint.status == SCHEMA_STATUS_ACTIVE,
        )
        .limit(1)
    )
    return result.scalars().first()


async def _list_active_attribute_definitions(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    owner_kind: str,
    owner_type_id: uuid.UUID,
) -> list[AttributeDefinition]:
    result = await db.execute(
        select(AttributeDefinition).where(
            AttributeDefinition.library_id == library.id,
            AttributeDefinition.ontology_version_id == ontology_version_id,
            AttributeDefinition.owner_kind == owner_kind,
            AttributeDefinition.owner_type_id == owner_type_id,
            AttributeDefinition.status == SCHEMA_STATUS_ACTIVE,
        )
    )
    return list(result.scalars().all())


def _attribute_definition_rule(row: AttributeDefinition) -> AttributeDefinitionRule:
    enum_values = None
    if row.enum_values is not None:
        enum_values = tuple(deepcopy(row.enum_values))
    return AttributeDefinitionRule(
        key=row.key,
        value_type=row.value_type,
        required=row.required,
        enum_values=enum_values,
        validation_schema=deepcopy(row.validation_schema),
    )


def _entity_type_rule(
    row: EntityType,
    attribute_definitions: list[AttributeDefinition],
) -> EntityTypeRule:
    rules = tuple(
        sorted(
            (_attribute_definition_rule(item) for item in attribute_definitions),
            key=lambda item: item.key,
        )
    )
    return EntityTypeRule(
        id=row.id,
        ontology_version_id=row.ontology_version_id,
        key=row.key,
        properties_schema=deepcopy(row.properties_schema),
        active_attribute_definitions=rules,
    )


def _relation_type_rule(
    row: RelationType,
    attribute_definitions: list[AttributeDefinition],
) -> RelationTypeRule:
    rules = tuple(
        sorted(
            (_attribute_definition_rule(item) for item in attribute_definitions),
            key=lambda item: item.key,
        )
    )
    return RelationTypeRule(
        id=row.id,
        ontology_version_id=row.ontology_version_id,
        key=row.key,
        direction=row.direction,
        requires_evidence=row.requires_evidence,
        default_review_policy=row.default_review_policy,
        properties_schema=deepcopy(row.properties_schema),
        active_attribute_definitions=rules,
    )


def _relation_constraint_rule(
    row: RelationTypeConstraint | None,
) -> RelationConstraintRule | None:
    if row is None:
        return None
    return RelationConstraintRule(
        source_entity_type_id=row.source_entity_type_id,
        target_entity_type_id=row.target_entity_type_id,
        cardinality=row.cardinality,
        requires_review=row.requires_review,
    )


async def _reject_duplicate_active_entity(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    entity_type_id: uuid.UUID,
    normalized_name: str,
    *,
    exclude_entity_id: uuid.UUID | None = None,
) -> None:
    statement = select(Entity).where(
            Entity.library_id == library.id,
            Entity.ontology_version_id == ontology_version_id,
            Entity.entity_type_id == entity_type_id,
            Entity.normalized_name == normalized_name,
            Entity.status == GRAPH_FACT_STATUS_ACTIVE,
        )
    if exclude_entity_id is not None:
        statement = statement.where(Entity.id != exclude_entity_id)
    result = await db.execute(statement.limit(1))
    if result.scalars().first() is not None:
        raise ValueError("duplicate active entity")


def _require_same_ontology(row, ontology_version_id: uuid.UUID, label: str) -> None:
    if row.ontology_version_id != ontology_version_id:
        raise ValueError(f"{label} must belong to the same ontology")


def _require_graph_fact_status(status: str) -> None:
    if status not in GRAPH_FACT_WRITE_STATUSES:
        raise ValueError("status is not valid for graph fact writes")


def _require_source_type(source_type: str) -> None:
    if source_type not in GRAPH_SOURCE_TYPES:
        raise ValueError("source_type must be manual, imported, or extracted")


def _coerce_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ValueError("properties must be an object")
    return properties


def _validate_properties_schema(
    properties: dict[str, Any],
    schema: dict[str, Any] | None,
    label: str,
) -> None:
    if schema is None:
        return
    if not isinstance(schema, dict):
        raise ValueError(f"{label} schema must be an object")

    required = schema.get("required", [])
    if required is None:
        required = []
    if not isinstance(required, list):
        raise ValueError(f"{label} schema required must be a list")
    for key in required:
        if key not in properties:
            raise ValueError(f"missing required property: {key}")

    property_specs = schema.get("properties", {})
    if property_specs is None:
        property_specs = {}
    if not isinstance(property_specs, dict):
        raise ValueError(f"{label} schema properties must be an object")
    for key, property_schema in property_specs.items():
        if key not in properties:
            continue
        if not isinstance(property_schema, dict):
            raise ValueError(f"{label} schema for {key} must be an object")
        expected_type = property_schema.get("type")
        if expected_type is not None:
            _validate_json_schema_type(key, properties[key], expected_type)
        enum_values = property_schema.get("enum")
        if enum_values is not None and properties[key] not in enum_values:
            raise ValueError(f"property {key} must be one of {enum_values}")


def _validate_json_schema_type(key: str, value: Any, expected_type: str | list[str]) -> None:
    expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
    if any(_matches_json_schema_type(value, type_name) for type_name in expected_types):
        return
    raise ValueError(f"property {key} must match schema type {expected_type}")


def _matches_json_schema_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "null":
        return value is None
    return True


def _validate_attribute_definitions(
    properties: dict[str, Any],
    attribute_definitions: tuple[AttributeDefinitionRule, ...],
) -> None:
    for definition in attribute_definitions:
        if definition.required and definition.key not in properties:
            raise ValueError(f"missing required attribute: {definition.key}")
        if definition.key not in properties:
            continue
        _validate_attribute_value(definition, properties[definition.key])


def _validate_attribute_value(definition: AttributeDefinitionRule, value: Any) -> None:
    value_type = definition.value_type
    key = definition.key
    if value_type in {"string", "text"}:
        if not isinstance(value, str):
            raise ValueError(f"attribute {key} must be {value_type}")
        return
    if value_type == "integer":
        if not (isinstance(value, int) and not isinstance(value, bool)):
            raise ValueError(f"attribute {key} must be integer")
        return
    if value_type == "number":
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            raise ValueError(f"attribute {key} must be number")
        return
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"attribute {key} must be boolean")
        return
    if value_type == "date":
        if not _is_date_value(value):
            raise ValueError(f"attribute {key} must be date")
        return
    if value_type == "datetime":
        if not _is_datetime_value(value):
            raise ValueError(f"attribute {key} must be datetime")
        return
    if value_type == "enum":
        if not definition.enum_values or value not in definition.enum_values:
            enum_values = (
                list(definition.enum_values)
                if definition.enum_values is not None
                else None
            )
            raise ValueError(f"attribute {key} must be one of {enum_values}")
        return
    if value_type == "json":
        return
    raise ValueError(f"unsupported attribute value_type: {value_type}")


def _is_date_value(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def _is_datetime_value(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
