from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import ATTRIBUTE_OWNER_ENTITY_TYPE
from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, ONTOLOGY_STATUS_DRAFT, OntologyVersion
from app.models.relation_type import (
    RELATION_DIRECTION_DIRECTED,
    RELATION_DIRECTION_UNDIRECTED,
    REVIEW_POLICY_AUTO_ACTIVE,
    REVIEW_POLICY_PENDING_REVIEW,
    RelationType,
)
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services import ontology


DEFAULT_ONTOLOGY_VERSION_KEY = "enterprise"
DEFAULT_ONTOLOGY_VERSION_NO = 1
DEFAULT_ONTOLOGY_DESCRIPTION = "Default enterprise ontology"

_COUNT_KEYS = (
    "ontology_versions",
    "entity_types",
    "relation_types",
    "relation_type_constraints",
    "attribute_definitions",
)


@dataclass(frozen=True)
class EntityTypeSeed:
    key: str
    label: str
    description: str | None = None
    properties_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class RelationTypeSeed:
    key: str
    label: str
    direction: str = RELATION_DIRECTION_DIRECTED
    requires_evidence: bool = True
    default_review_policy: str = REVIEW_POLICY_AUTO_ACTIVE
    description: str | None = None
    properties_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class RelationConstraintSeedGroup:
    relation_type_key: str
    source_entity_type_keys: tuple[str, ...]
    target_entity_type_keys: tuple[str, ...]
    cardinality: str | None = "many_to_many"
    requires_review: bool = False


@dataclass(frozen=True)
class RelationConstraintSeed:
    relation_type_key: str
    source_entity_type_key: str
    target_entity_type_key: str
    cardinality: str | None
    requires_review: bool


@dataclass(frozen=True)
class AttributeDefinitionSeed:
    owner_kind: str
    owner_type_key: str
    key: str
    label: str
    value_type: str
    required: bool = False
    enum_values: list[Any] | None = None
    validation_schema: dict[str, Any] | None = None
    indexed: bool = False


@dataclass(frozen=True)
class SeedEnterpriseOntologyResult:
    ontology_version: OntologyVersion
    created_counts: dict[str, int]
    existing_counts: dict[str, int]


DEFAULT_ENTITY_TYPES: tuple[EntityTypeSeed, ...] = (
    EntityTypeSeed("person", "Person", "An individual employee, user, or contact."),
    EntityTypeSeed("department", "Department", "An organizational unit."),
    EntityTypeSeed("position", "Position", "A role or job position."),
    EntityTypeSeed("policy", "Policy", "A rule, policy, or governance document."),
    EntityTypeSeed("process", "Process", "A business process or workflow."),
    EntityTypeSeed("project", "Project", "A project or initiative."),
    EntityTypeSeed("product", "Product", "A product or service offering."),
    EntityTypeSeed("customer", "Customer", "A customer, client, or account."),
    EntityTypeSeed("document", "Document", "A document-like business artifact."),
    EntityTypeSeed("term", "Term", "A domain term or glossary entry."),
)

DEFAULT_RELATION_TYPES: tuple[RelationTypeSeed, ...] = (
    RelationTypeSeed("belongs_to", "Belongs To"),
    RelationTypeSeed("reports_to", "Reports To"),
    RelationTypeSeed("responsible_for", "Responsible For"),
    RelationTypeSeed("applies_to", "Applies To"),
    RelationTypeSeed("constrains", "Constrains"),
    RelationTypeSeed("references", "References"),
    RelationTypeSeed("approves", "Approves"),
    RelationTypeSeed("depends_on", "Depends On"),
    RelationTypeSeed("owns", "Owns"),
    RelationTypeSeed(
        "related_to",
        "Related To",
        direction=RELATION_DIRECTION_UNDIRECTED,
        requires_evidence=True,
        default_review_policy=REVIEW_POLICY_PENDING_REVIEW,
        description="Fallback relation for unclear schema boundaries; always requires evidence and review.",
    ),
)

_ALL_ENTITY_KEYS = tuple(spec.key for spec in DEFAULT_ENTITY_TYPES)

DEFAULT_RELATION_CONSTRAINT_GROUPS: tuple[RelationConstraintSeedGroup, ...] = (
    RelationConstraintSeedGroup(
        "belongs_to",
        ("person",),
        ("department",),
        cardinality="many_to_one",
    ),
    RelationConstraintSeedGroup(
        "belongs_to",
        ("position",),
        ("department",),
        cardinality="many_to_one",
    ),
    RelationConstraintSeedGroup(
        "reports_to",
        ("person",),
        ("person",),
        cardinality="many_to_one",
    ),
    RelationConstraintSeedGroup(
        "reports_to",
        ("position",),
        ("position",),
        cardinality="many_to_one",
    ),
    RelationConstraintSeedGroup(
        "responsible_for",
        ("person", "department"),
        ("project", "process", "policy", "product"),
    ),
    RelationConstraintSeedGroup(
        "applies_to",
        ("policy",),
        ("department", "position", "person"),
        cardinality="one_to_many",
    ),
    RelationConstraintSeedGroup("constrains", ("policy",), ("process", "project")),
    RelationConstraintSeedGroup("depends_on", ("process",), ("policy", "process")),
    RelationConstraintSeedGroup("references", ("policy", "document"), ("policy", "document")),
    RelationConstraintSeedGroup("approves", ("person", "position", "department"), ("process",)),
    RelationConstraintSeedGroup(
        "owns",
        ("department",),
        ("product", "project"),
        cardinality="one_to_many",
    ),
    RelationConstraintSeedGroup(
        "related_to",
        _ALL_ENTITY_KEYS,
        _ALL_ENTITY_KEYS,
        requires_review=True,
    ),
)

DEFAULT_ATTRIBUTE_DEFINITIONS: tuple[AttributeDefinitionSeed, ...] = (
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "person", "employee_id", "Employee ID", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "person", "email", "Email", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "department", "code", "Code", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "process", "code", "Code", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "project", "code", "Code", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "product", "code", "Code", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "customer", "code", "Code", "string", indexed=True),
    AttributeDefinitionSeed(
        ATTRIBUTE_OWNER_ENTITY_TYPE,
        "policy",
        "effective_date",
        "Effective Date",
        "date",
        indexed=True,
    ),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "document", "source_uri", "Source URI", "string", indexed=True),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "term", "definition", "Definition", "text"),
)


def expanded_default_relation_constraints() -> tuple[RelationConstraintSeed, ...]:
    return tuple(
        RelationConstraintSeed(
            relation_type_key=group.relation_type_key,
            source_entity_type_key=source_key,
            target_entity_type_key=target_key,
            cardinality=group.cardinality,
            requires_review=group.requires_review,
        )
        for group in DEFAULT_RELATION_CONSTRAINT_GROUPS
        for source_key in group.source_entity_type_keys
        for target_key in group.target_entity_type_keys
    )


async def seed_enterprise_ontology(
    db: AsyncSession,
    library: Library,
) -> SeedEnterpriseOntologyResult:
    created_counts = _zero_counts()
    existing_counts = _zero_counts()
    ontology_version = await ontology.find_ontology_version(
        db,
        library,
        version_key=DEFAULT_ONTOLOGY_VERSION_KEY,
        version_no=DEFAULT_ONTOLOGY_VERSION_NO,
    )

    if ontology_version is None:
        ontology_version = await ontology.create_ontology_version(
            db,
            library,
            version_key=DEFAULT_ONTOLOGY_VERSION_KEY,
            version_no=DEFAULT_ONTOLOGY_VERSION_NO,
            status=ONTOLOGY_STATUS_DRAFT,
            description=DEFAULT_ONTOLOGY_DESCRIPTION,
        )
        created_counts["ontology_versions"] += 1

    if ontology_version.status == ONTOLOGY_STATUS_ACTIVE:
        await _ensure_seed_rows(
            db,
            library,
            ontology_version,
            allow_create=False,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )
        return SeedEnterpriseOntologyResult(ontology_version, created_counts, existing_counts)

    if ontology_version.status != ONTOLOGY_STATUS_DRAFT:
        raise ValueError("enterprise ontology must be draft or active to seed")

    await _ensure_seed_rows(
        db,
        library,
        ontology_version,
        allow_create=True,
        created_counts=created_counts,
        existing_counts=existing_counts,
    )
    ontology_version = await ontology.update_ontology_version(
        db,
        library,
        ontology_version.id,
        status=ONTOLOGY_STATUS_ACTIVE,
        published_at=datetime.now(timezone.utc),
    )
    return SeedEnterpriseOntologyResult(ontology_version, created_counts, existing_counts)


async def _ensure_seed_rows(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    *,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
) -> None:
    entity_types: dict[str, EntityType] = {}
    relation_types: dict[str, RelationType] = {}

    for spec in DEFAULT_ENTITY_TYPES:
        entity_types[spec.key] = await _ensure_entity_type(
            db,
            library,
            ontology_version,
            spec,
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )

    for spec in DEFAULT_RELATION_TYPES:
        relation_types[spec.key] = await _ensure_relation_type(
            db,
            library,
            ontology_version,
            spec,
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )

    for spec in expanded_default_relation_constraints():
        await _ensure_relation_type_constraint(
            db,
            library,
            ontology_version,
            spec,
            relation_type=relation_types[spec.relation_type_key],
            source_entity_type=entity_types[spec.source_entity_type_key],
            target_entity_type=entity_types[spec.target_entity_type_key],
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )

    for spec in DEFAULT_ATTRIBUTE_DEFINITIONS:
        await _ensure_attribute_definition(
            db,
            library,
            ontology_version,
            spec,
            owner_type=entity_types[spec.owner_type_key],
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )


async def _ensure_entity_type(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    spec: EntityTypeSeed,
    *,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
) -> EntityType:
    row = await ontology.find_entity_type(db, library, ontology_version.id, spec.key)
    if row is None:
        _require_can_create(allow_create, f"missing entity type {spec.key}")
        created_counts["entity_types"] += 1
        return await ontology.create_entity_type(
            db,
            library,
            ontology_version.id,
            key=spec.key,
            label=spec.label,
            description=spec.description,
            properties_schema=spec.properties_schema,
            is_seeded=True,
            status=ontology.SCHEMA_STATUS_ACTIVE,
        )

    _validate_entity_type(row, spec)
    existing_counts["entity_types"] += 1
    return row


async def _ensure_relation_type(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    spec: RelationTypeSeed,
    *,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
) -> RelationType:
    row = await ontology.find_relation_type(db, library, ontology_version.id, spec.key)
    if row is None:
        _require_can_create(allow_create, f"missing relation type {spec.key}")
        created_counts["relation_types"] += 1
        return await ontology.create_relation_type(
            db,
            library,
            ontology_version.id,
            key=spec.key,
            label=spec.label,
            description=spec.description,
            direction=spec.direction,
            requires_evidence=spec.requires_evidence,
            default_review_policy=spec.default_review_policy,
            properties_schema=spec.properties_schema,
            is_seeded=True,
            status=ontology.SCHEMA_STATUS_ACTIVE,
        )

    _validate_relation_type(row, spec)
    existing_counts["relation_types"] += 1
    return row


async def _ensure_relation_type_constraint(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    spec: RelationConstraintSeed,
    *,
    relation_type: RelationType,
    source_entity_type: EntityType,
    target_entity_type: EntityType,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
) -> RelationTypeConstraint:
    row = await ontology.find_relation_type_constraint(
        db,
        library,
        ontology_version.id,
        relation_type_id=relation_type.id,
        source_entity_type_id=source_entity_type.id,
        target_entity_type_id=target_entity_type.id,
    )
    if row is None:
        _require_can_create(
            allow_create,
            f"missing relation constraint {spec.source_entity_type_key}->{spec.relation_type_key}->{spec.target_entity_type_key}",
        )
        created_counts["relation_type_constraints"] += 1
        return await ontology.create_relation_type_constraint(
            db,
            library,
            ontology_version.id,
            relation_type_id=relation_type.id,
            source_entity_type_id=source_entity_type.id,
            target_entity_type_id=target_entity_type.id,
            cardinality=spec.cardinality,
            requires_review=spec.requires_review,
            status=ontology.SCHEMA_STATUS_ACTIVE,
        )

    _validate_relation_type_constraint(row, spec)
    existing_counts["relation_type_constraints"] += 1
    return row


async def _ensure_attribute_definition(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    spec: AttributeDefinitionSeed,
    *,
    owner_type: EntityType,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
) -> AttributeDefinition:
    row = await ontology.find_attribute_definition(
        db,
        library,
        ontology_version.id,
        owner_kind=spec.owner_kind,
        owner_type_id=owner_type.id,
        key=spec.key,
    )
    if row is None:
        _require_can_create(allow_create, f"missing attribute definition {spec.owner_type_key}.{spec.key}")
        created_counts["attribute_definitions"] += 1
        return await ontology.create_attribute_definition(
            db,
            library,
            ontology_version.id,
            owner_kind=spec.owner_kind,
            owner_type_id=owner_type.id,
            key=spec.key,
            label=spec.label,
            value_type=spec.value_type,
            required=spec.required,
            enum_values=spec.enum_values,
            validation_schema=spec.validation_schema,
            indexed=spec.indexed,
            status=ontology.SCHEMA_STATUS_ACTIVE,
        )

    _validate_attribute_definition(row, spec, owner_type)
    existing_counts["attribute_definitions"] += 1
    return row


def _require_can_create(allow_create: bool, detail: str) -> None:
    if not allow_create:
        raise ValueError(f"active enterprise ontology is incomplete: {detail}")


def _validate_entity_type(row: EntityType, spec: EntityTypeSeed) -> None:
    expected = {
        "label": spec.label,
        "description": spec.description,
        "properties_schema": spec.properties_schema,
        "is_seeded": True,
        "status": ontology.SCHEMA_STATUS_ACTIVE,
    }
    _validate_expected_fields(row, expected, f"seeded entity type conflict: {spec.key}")


def _validate_relation_type(row: RelationType, spec: RelationTypeSeed) -> None:
    expected = {
        "label": spec.label,
        "description": spec.description,
        "direction": spec.direction,
        "requires_evidence": spec.requires_evidence,
        "default_review_policy": spec.default_review_policy,
        "properties_schema": spec.properties_schema,
        "is_seeded": True,
        "status": ontology.SCHEMA_STATUS_ACTIVE,
    }
    _validate_expected_fields(row, expected, f"seeded relation type conflict: {spec.key}")


def _validate_relation_type_constraint(row: RelationTypeConstraint, spec: RelationConstraintSeed) -> None:
    expected = {
        "cardinality": spec.cardinality,
        "requires_review": spec.requires_review,
        "status": ontology.SCHEMA_STATUS_ACTIVE,
    }
    _validate_expected_fields(
        row,
        expected,
        f"seeded relation constraint conflict: {spec.source_entity_type_key}->{spec.relation_type_key}->{spec.target_entity_type_key}",
    )


def _validate_attribute_definition(
    row: AttributeDefinition,
    spec: AttributeDefinitionSeed,
    owner_type: EntityType,
) -> None:
    expected = {
        "owner_kind": spec.owner_kind,
        "owner_type_id": owner_type.id,
        "label": spec.label,
        "value_type": spec.value_type,
        "required": spec.required,
        "enum_values": spec.enum_values,
        "validation_schema": spec.validation_schema,
        "indexed": spec.indexed,
        "status": ontology.SCHEMA_STATUS_ACTIVE,
    }
    _validate_expected_fields(row, expected, f"seeded attribute definition conflict: {spec.owner_type_key}.{spec.key}")


def _validate_expected_fields(row, expected: dict[str, Any], message: str) -> None:
    for field_name, expected_value in expected.items():
        if getattr(row, field_name) != expected_value:
            raise ValueError(message)


def _zero_counts() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}
