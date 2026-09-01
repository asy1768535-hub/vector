from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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
EXPLORATION_ONTOLOGY_VERSION_KEY = "ai-exploration"
EXPLORATION_ONTOLOGY_VERSION_NO = 2
EXPLORATION_ONTOLOGY_DESCRIPTION = "Minimal AI exploration ontology used before a user schema is uploaded"

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
        "contains",
        "Contains",
        description="A page, document, product, or project explicitly contains a component, artifact, or process.",
    ),
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
        "contains",
        ("document", "product", "project"),
        ("document", "process", "product"),
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
    AttributeDefinitionSeed(
        ATTRIBUTE_OWNER_ENTITY_TYPE, "person", "employee_id", "Employee ID", "string", indexed=True
    ),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "person", "email", "Email", "string", indexed=True),
    AttributeDefinitionSeed(
        ATTRIBUTE_OWNER_ENTITY_TYPE, "department", "code", "Code", "string", indexed=True
    ),
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
    AttributeDefinitionSeed(
        ATTRIBUTE_OWNER_ENTITY_TYPE, "document", "source_uri", "Source URI", "string", indexed=True
    ),
    AttributeDefinitionSeed(ATTRIBUTE_OWNER_ENTITY_TYPE, "term", "definition", "Definition", "text"),
)

EXPLORATION_ENTITY_TYPES: tuple[EntityTypeSeed, ...] = (
    EntityTypeSeed("approval", "批复", "批复、批准、审批文件、批复编号。"),
    EntityTypeSeed("company", "公司", "公司、企业、有限公司、子公司、法人组织。"),
    EntityTypeSeed("contract", "合同", "合同、协议、采购合同、合同编号。"),
    EntityTypeSeed("equipment", "设备", "设备、型号、机器、变流器、保护系统、技术系统、继电保护系统。"),
    EntityTypeSeed("location", "地点", "地点、地址、位置、省、市、区、县、行政区。"),
    EntityTypeSeed("person", "人员", "人名、姓名、人员、法定代表人、经理、总监、负责人。"),
    EntityTypeSeed("power_grid", "电网", "电网、供电网络、输配电网、并网系统。"),
    EntityTypeSeed("project", "项目", "项目、工程、电站、建设项目、施工项目。"),
)

EXPLORATION_RELATION_TYPES: tuple[RelationTypeSeed, ...] = (
    RelationTypeSeed("approves_connection", "批准接入", description="批准、批准接入、同意并网、批复接入。"),
    RelationTypeSeed("connects_to", "接入", description="接入、并入、并网、连接到电网。"),
    RelationTypeSeed("constructs", "施工", description="施工、承建、建设项目。"),
    RelationTypeSeed(
        "cooperates_with",
        "合作",
        direction=RELATION_DIRECTION_UNDIRECTED,
        description="合作、联合、协作。",
    ),
    RelationTypeSeed("general_manager_of", "担任总经理", description="总经理、担任总经理、任命为总经理。"),
    RelationTypeSeed("launches", "启动", description="启动、发起、项目开工。"),
    RelationTypeSeed("legal_representative", "法定代表人", description="法定代表人、法人代表、企业法人。"),
    RelationTypeSeed("located_in", "位于", description="位于、坐落、地址、项目位置。"),
    RelationTypeSeed("procures", "采购", description="采购、购买、订购设备。"),
    RelationTypeSeed(
        "project_manager_of", "担任项目经理", description="项目经理、担任项目经理、任命为项目经理。"
    ),
    RelationTypeSeed("reports_to", "汇报给", description="汇报给、汇报工作、向上级汇报、直属上级。"),
    RelationTypeSeed(
        "safety_officer_of", "担任安全负责人", description="安全负责人、担任安全负责人、负责项目安全。"
    ),
    RelationTypeSeed("sales_director_of", "担任销售总监", description="销售总监、担任销售总监、负责销售。"),
    RelationTypeSeed("supervises", "监理", description="监理、监督、项目监管。"),
    RelationTypeSeed("supplies", "供应", description="供应、供货、提供设备。"),
    RelationTypeSeed("uses", "采用", description="采用、使用、配备设备。"),
    RelationTypeSeed("wholly_owns", "全资拥有", description="全资拥有、旗下拥有、全资子公司、百分之百持有。"),
)

EXPLORATION_RELATION_CONSTRAINTS: tuple[RelationConstraintSeed, ...] = (
    RelationConstraintSeed("approves_connection", "company", "project", "many_to_many", False),
    RelationConstraintSeed("connects_to", "project", "power_grid", "many_to_many", False),
    RelationConstraintSeed("constructs", "company", "project", "many_to_many", False),
    RelationConstraintSeed("cooperates_with", "company", "company", "many_to_many", False),
    RelationConstraintSeed("general_manager_of", "person", "company", "many_to_one", False),
    RelationConstraintSeed("launches", "company", "project", "many_to_many", False),
    RelationConstraintSeed("legal_representative", "company", "person", "one_to_one", False),
    RelationConstraintSeed("located_in", "project", "location", "many_to_one", False),
    RelationConstraintSeed("procures", "company", "equipment", "many_to_many", False),
    RelationConstraintSeed("project_manager_of", "person", "project", "many_to_many", False),
    RelationConstraintSeed("reports_to", "person", "person", "many_to_one", False),
    RelationConstraintSeed("safety_officer_of", "person", "project", "many_to_many", False),
    RelationConstraintSeed("sales_director_of", "person", "company", "many_to_one", False),
    RelationConstraintSeed("supervises", "company", "project", "many_to_many", False),
    RelationConstraintSeed("supplies", "company", "equipment", "many_to_many", False),
    RelationConstraintSeed("uses", "project", "equipment", "many_to_many", False),
    RelationConstraintSeed("wholly_owns", "company", "company", "one_to_many", False),
)

EXPLORATION_ATTRIBUTE_DEFINITIONS: tuple[AttributeDefinitionSeed, ...] = ()


async def ensure_ai_draft_ontology(
    db: AsyncSession,
    library: Library,
    *,
    rotate_active: bool = False,
) -> OntologyVersion:
    """Return a non-business, empty draft used until corpus discovery finishes.

    This is a runtime coordination row only.  It deliberately creates no
    entity/relation allowlist and is never the historical exploration ontology.
    """

    result = await db.execute(
        select(OntologyVersion)
        .where(
            OntologyVersion.library_id == library.id,
            OntologyVersion.version_key == "ai-draft",
        )
        .order_by(OntologyVersion.version_no.desc())
        .limit(1)
    )
    existing = result.scalars().first()
    if existing is not None and existing.status == ONTOLOGY_STATUS_DRAFT:
        return existing
    if existing is not None and existing.status == ONTOLOGY_STATUS_ACTIVE and not rotate_active:
        return existing
    next_version_no = (existing.version_no + 1) if existing is not None else 1
    if existing is not None and existing.status == ONTOLOGY_STATUS_ACTIVE:
        # Keep the historical draft addressable by old job snapshots while a
        # new upload gets an independent, immutable discovery candidate.
        existing.status = "disabled"
        await db.flush()
    elif existing is not None and existing.status not in {"disabled", "deleted"}:
        raise ValueError("AI draft ontology is not reusable")
    return await ontology.create_ontology_version(
        db,
        library,
        version_key="ai-draft",
        version_no=next_version_no,
        status=ONTOLOGY_STATUS_DRAFT,
        description="AI Schema discovery pending; no business allowlist",
        parent_version_id=existing.id if existing is not None else None,
        origin="ai_discovery",
        confirmed=False,
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
    return await _seed_ontology(
        db,
        library,
        version_key=DEFAULT_ONTOLOGY_VERSION_KEY,
        version_no=DEFAULT_ONTOLOGY_VERSION_NO,
        description=DEFAULT_ONTOLOGY_DESCRIPTION,
        entity_specs=DEFAULT_ENTITY_TYPES,
        relation_specs=DEFAULT_RELATION_TYPES,
        relation_constraint_specs=expanded_default_relation_constraints(),
        attribute_specs=DEFAULT_ATTRIBUTE_DEFINITIONS,
        label="enterprise",
    )


async def seed_exploration_ontology(
    db: AsyncSession,
    library: Library,
) -> SeedEnterpriseOntologyResult:
    return await _seed_ontology(
        db,
        library,
        version_key=EXPLORATION_ONTOLOGY_VERSION_KEY,
        version_no=EXPLORATION_ONTOLOGY_VERSION_NO,
        description=EXPLORATION_ONTOLOGY_DESCRIPTION,
        entity_specs=EXPLORATION_ENTITY_TYPES,
        relation_specs=EXPLORATION_RELATION_TYPES,
        relation_constraint_specs=EXPLORATION_RELATION_CONSTRAINTS,
        attribute_specs=EXPLORATION_ATTRIBUTE_DEFINITIONS,
        label="exploration",
    )


async def _seed_ontology(
    db: AsyncSession,
    library: Library,
    *,
    version_key: str,
    version_no: int,
    description: str,
    entity_specs: tuple[EntityTypeSeed, ...],
    relation_specs: tuple[RelationTypeSeed, ...],
    relation_constraint_specs: tuple[RelationConstraintSeed, ...],
    attribute_specs: tuple[AttributeDefinitionSeed, ...],
    label: str,
) -> SeedEnterpriseOntologyResult:
    created_counts = _zero_counts()
    existing_counts = _zero_counts()
    ontology_version = await ontology.find_ontology_version(
        db,
        library,
        version_key=version_key,
        version_no=version_no,
    )

    if ontology_version is None:
        ontology_version = await ontology.create_ontology_version(
            db,
            library,
            version_key=version_key,
            version_no=version_no,
            status=ONTOLOGY_STATUS_DRAFT,
            description=description,
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
            entity_specs=entity_specs,
            relation_specs=relation_specs,
            relation_constraint_specs=relation_constraint_specs,
            attribute_specs=attribute_specs,
        )
        return SeedEnterpriseOntologyResult(ontology_version, created_counts, existing_counts)

    if ontology_version.status != ONTOLOGY_STATUS_DRAFT:
        raise ValueError(f"{label} ontology must be draft or active to seed")

    await _ensure_seed_rows(
        db,
        library,
        ontology_version,
        allow_create=True,
        created_counts=created_counts,
        existing_counts=existing_counts,
        entity_specs=entity_specs,
        relation_specs=relation_specs,
        relation_constraint_specs=relation_constraint_specs,
        attribute_specs=attribute_specs,
    )
    ontology_version = await ontology.update_ontology_version(
        db,
        library,
        ontology_version.id,
        status=ONTOLOGY_STATUS_ACTIVE,
        published_at=datetime.now(timezone.utc),
    )
    if label == "enterprise":
        library.current_ontology_version_id = ontology_version.id
    return SeedEnterpriseOntologyResult(ontology_version, created_counts, existing_counts)


async def _ensure_seed_rows(
    db: AsyncSession,
    library: Library,
    ontology_version: OntologyVersion,
    *,
    allow_create: bool,
    created_counts: dict[str, int],
    existing_counts: dict[str, int],
    entity_specs: tuple[EntityTypeSeed, ...] = DEFAULT_ENTITY_TYPES,
    relation_specs: tuple[RelationTypeSeed, ...] = DEFAULT_RELATION_TYPES,
    relation_constraint_specs: tuple[RelationConstraintSeed, ...] | None = None,
    attribute_specs: tuple[AttributeDefinitionSeed, ...] = DEFAULT_ATTRIBUTE_DEFINITIONS,
) -> None:
    entity_types: dict[str, EntityType] = {}
    relation_types: dict[str, RelationType] = {}
    constraint_specs = (
        expanded_default_relation_constraints()
        if relation_constraint_specs is None
        else relation_constraint_specs
    )

    for spec in entity_specs:
        entity_types[spec.key] = await _ensure_entity_type(
            db,
            library,
            ontology_version,
            spec,
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )

    for spec in relation_specs:
        relation_types[spec.key] = await _ensure_relation_type(
            db,
            library,
            ontology_version,
            spec,
            allow_create=allow_create,
            created_counts=created_counts,
            existing_counts=existing_counts,
        )

    for spec in constraint_specs:
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

    for spec in attribute_specs:
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
    _validate_expected_fields(
        row, expected, f"seeded attribute definition conflict: {spec.owner_type_key}.{spec.key}"
    )


def _validate_expected_fields(row, expected: dict[str, Any], message: str) -> None:
    for field_name, expected_value in expected.items():
        if getattr(row, field_name) != expected_value:
            raise ValueError(message)


def _zero_counts() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}
