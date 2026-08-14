from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import (
    ATTRIBUTE_OWNER_ENTITY_TYPE,
    ATTRIBUTE_OWNER_RELATION_TYPE,
    AttributeDefinition,
)
from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import (
    ONTOLOGY_STATUS_ACTIVE,
    ONTOLOGY_STATUS_DELETED,
    ONTOLOGY_STATUS_DRAFT,
    OntologyVersion,
)
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint


SCHEMA_STATUS_ACTIVE = "active"
SCHEMA_STATUS_DELETED = "deleted"
SCHEMA_STATUS_DRAFT = "draft"


async def _get(db: AsyncSession, model, object_id: uuid.UUID, label: str):
    row = await db.get(model, object_id)
    if row is None:
        raise LookupError(f"{label} not found")
    return row


def _require_library(row, library: Library, label: str) -> None:
    if row.library_id != library.id:
        raise LookupError(f"{label} not found")


def _require_same_library(row, library_id: uuid.UUID, label: str) -> None:
    if row.library_id != library_id:
        raise ValueError(f"{label} must belong to the same library")


def _require_same_ontology(row, ontology_version_id: uuid.UUID, label: str) -> None:
    if row.ontology_version_id != ontology_version_id:
        raise ValueError(f"{label} must belong to the same ontology")


def _require_same_scope(row, library_id: uuid.UUID, ontology_version_id: uuid.UUID, label: str) -> None:
    _require_same_library(row, library_id, label)
    _require_same_ontology(row, ontology_version_id, label)


def require_ontology_mutable(ontology: OntologyVersion) -> None:
    if ontology.status == ONTOLOGY_STATUS_ACTIVE:
        raise ValueError("active ontology cannot be modified in place")
    if ontology.status != ONTOLOGY_STATUS_DRAFT:
        raise ValueError("only draft ontology can be modified")


def require_schema_row_mutable(ontology: OntologyVersion, row) -> None:
    require_ontology_mutable(ontology)
    if row.status == SCHEMA_STATUS_ACTIVE:
        raise ValueError("active schema row cannot be modified in place")


async def get_ontology_version(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
) -> OntologyVersion:
    ontology = await _get(db, OntologyVersion, ontology_version_id, "ontology version")
    _require_library(ontology, library, "ontology version")
    if ontology.status == ONTOLOGY_STATUS_DELETED:
        raise LookupError("ontology version not found")
    return ontology


async def find_ontology_version(
    db: AsyncSession,
    library: Library,
    *,
    version_key: str,
    version_no: int,
) -> OntologyVersion | None:
    result = await db.execute(
        select(OntologyVersion)
        .where(
            OntologyVersion.library_id == library.id,
            OntologyVersion.version_key == version_key,
            OntologyVersion.version_no == version_no,
        )
        .limit(1)
    )
    return result.scalars().first()


async def create_ontology_version(
    db: AsyncSession,
    library: Library,
    *,
    version_key: str,
    version_no: int,
    status: str = ONTOLOGY_STATUS_DRAFT,
    description: str | None = None,
    parent_version_id: uuid.UUID | None = None,
    origin: str = "user",
    confirmed: bool = True,
) -> OntologyVersion:
    if parent_version_id is not None:
        parent = await get_ontology_version(db, library, parent_version_id)
        _require_same_library(parent, library.id, "parent ontology version")

    ontology = OntologyVersion(
        library_id=library.id,
        version_key=version_key,
        version_no=version_no,
        status=status,
        description=description,
        origin=origin,
        confirmed=confirmed,
        parent_version_id=parent_version_id,
    )
    db.add(ontology)
    await db.flush()
    return ontology


async def update_ontology_version(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    **changes: Any,
) -> OntologyVersion:
    ontology = await get_ontology_version(db, library, ontology_version_id)
    require_ontology_mutable(ontology)
    _apply_changes(ontology, changes)
    await db.flush()
    return ontology


async def _child_with_ontology(
    db: AsyncSession,
    library: Library,
    model,
    object_id: uuid.UUID,
    label: str,
):
    row = await _get(db, model, object_id, label)
    _require_library(row, library, label)
    ontology = await get_ontology_version(db, library, row.ontology_version_id)
    _require_same_scope(row, library.id, ontology.id, label)
    return row, ontology


def _apply_changes(row, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        setattr(row, key, value)


async def create_entity_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    key: str,
    label: str,
    description: str | None = None,
    properties_schema: dict[str, Any] | None = None,
    is_seeded: bool = False,
    status: str = SCHEMA_STATUS_DRAFT,
) -> EntityType:
    ontology = await get_ontology_version(db, library, ontology_version_id)
    require_ontology_mutable(ontology)
    row = EntityType(
        id=object_id or uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        key=key,
        label=label,
        description=description,
        properties_schema=properties_schema,
        is_seeded=is_seeded,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def find_entity_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    key: str,
) -> EntityType | None:
    result = await db.execute(
        select(EntityType)
        .where(
            EntityType.library_id == library.id,
            EntityType.ontology_version_id == ontology_version_id,
            EntityType.key == key,
        )
        .limit(1)
    )
    return result.scalars().first()


async def update_entity_type(
    db: AsyncSession,
    library: Library,
    entity_type_id: uuid.UUID,
    **changes: Any,
) -> EntityType:
    row, ontology = await _child_with_ontology(db, library, EntityType, entity_type_id, "entity type")
    require_schema_row_mutable(ontology, row)
    _apply_changes(row, changes)
    await db.flush()
    return row


async def delete_entity_type(db: AsyncSession, library: Library, entity_type_id: uuid.UUID) -> EntityType:
    row, ontology = await _child_with_ontology(db, library, EntityType, entity_type_id, "entity type")
    require_schema_row_mutable(ontology, row)
    row.status = SCHEMA_STATUS_DELETED
    await db.flush()
    return row


async def create_relation_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    key: str,
    label: str,
    direction: str,
    default_review_policy: str,
    description: str | None = None,
    requires_evidence: bool = True,
    properties_schema: dict[str, Any] | None = None,
    is_seeded: bool = False,
    status: str = SCHEMA_STATUS_DRAFT,
) -> RelationType:
    ontology = await get_ontology_version(db, library, ontology_version_id)
    require_ontology_mutable(ontology)
    row = RelationType(
        id=object_id or uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        key=key,
        label=label,
        description=description,
        direction=direction,
        requires_evidence=requires_evidence,
        default_review_policy=default_review_policy,
        properties_schema=properties_schema,
        is_seeded=is_seeded,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def find_relation_type(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    key: str,
) -> RelationType | None:
    result = await db.execute(
        select(RelationType)
        .where(
            RelationType.library_id == library.id,
            RelationType.ontology_version_id == ontology_version_id,
            RelationType.key == key,
        )
        .limit(1)
    )
    return result.scalars().first()


async def update_relation_type(
    db: AsyncSession,
    library: Library,
    relation_type_id: uuid.UUID,
    **changes: Any,
) -> RelationType:
    row, ontology = await _child_with_ontology(db, library, RelationType, relation_type_id, "relation type")
    require_schema_row_mutable(ontology, row)
    _apply_changes(row, changes)
    await db.flush()
    return row


async def delete_relation_type(db: AsyncSession, library: Library, relation_type_id: uuid.UUID) -> RelationType:
    row, ontology = await _child_with_ontology(db, library, RelationType, relation_type_id, "relation type")
    require_schema_row_mutable(ontology, row)
    row.status = SCHEMA_STATUS_DELETED
    await db.flush()
    return row


async def create_relation_type_constraint(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    relation_type_id: uuid.UUID,
    source_entity_type_id: uuid.UUID,
    target_entity_type_id: uuid.UUID,
    cardinality: str | None = None,
    requires_review: bool = False,
    status: str = SCHEMA_STATUS_DRAFT,
) -> RelationTypeConstraint:
    ontology = await get_ontology_version(db, library, ontology_version_id)
    require_ontology_mutable(ontology)
    relation_type = await _get(db, RelationType, relation_type_id, "relation type")
    source_entity_type = await _get(db, EntityType, source_entity_type_id, "source entity type")
    target_entity_type = await _get(db, EntityType, target_entity_type_id, "target entity type")
    _require_same_scope(relation_type, library.id, ontology.id, "relation type")
    _require_same_scope(source_entity_type, library.id, ontology.id, "source entity type")
    _require_same_scope(target_entity_type, library.id, ontology.id, "target entity type")

    row = RelationTypeConstraint(
        id=object_id or uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        relation_type_id=relation_type.id,
        source_entity_type_id=source_entity_type.id,
        target_entity_type_id=target_entity_type.id,
        cardinality=cardinality,
        requires_review=requires_review,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def find_relation_type_constraint(
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
        )
        .limit(1)
    )
    return result.scalars().first()


async def update_relation_type_constraint(
    db: AsyncSession,
    library: Library,
    constraint_id: uuid.UUID,
    **changes: Any,
) -> RelationTypeConstraint:
    row, ontology = await _child_with_ontology(
        db,
        library,
        RelationTypeConstraint,
        constraint_id,
        "relation type constraint",
    )
    require_schema_row_mutable(ontology, row)
    _apply_changes(row, changes)
    await db.flush()
    return row


async def delete_relation_type_constraint(
    db: AsyncSession,
    library: Library,
    constraint_id: uuid.UUID,
) -> RelationTypeConstraint:
    row, ontology = await _child_with_ontology(
        db,
        library,
        RelationTypeConstraint,
        constraint_id,
        "relation type constraint",
    )
    require_schema_row_mutable(ontology, row)
    row.status = SCHEMA_STATUS_DELETED
    await db.flush()
    return row


async def create_attribute_definition(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    owner_kind: str,
    owner_type_id: uuid.UUID,
    key: str,
    label: str,
    value_type: str,
    required: bool = False,
    enum_values: list[Any] | None = None,
    validation_schema: dict[str, Any] | None = None,
    indexed: bool = False,
    status: str = SCHEMA_STATUS_DRAFT,
) -> AttributeDefinition:
    ontology = await get_ontology_version(db, library, ontology_version_id)
    require_ontology_mutable(ontology)
    await _require_attribute_owner_scope(db, library.id, ontology.id, owner_kind, owner_type_id)
    if value_type == "enum" and not enum_values:
        raise ValueError("enum attribute definitions require enum_values")

    row = AttributeDefinition(
        id=object_id or uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        owner_kind=owner_kind,
        owner_type_id=owner_type_id,
        key=key,
        label=label,
        value_type=value_type,
        required=required,
        enum_values=enum_values,
        validation_schema=validation_schema,
        indexed=indexed,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def find_attribute_definition(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    owner_kind: str,
    owner_type_id: uuid.UUID,
    key: str,
) -> AttributeDefinition | None:
    result = await db.execute(
        select(AttributeDefinition)
        .where(
            AttributeDefinition.library_id == library.id,
            AttributeDefinition.ontology_version_id == ontology_version_id,
            AttributeDefinition.owner_kind == owner_kind,
            AttributeDefinition.owner_type_id == owner_type_id,
            AttributeDefinition.key == key,
        )
        .limit(1)
    )
    return result.scalars().first()


async def update_attribute_definition(
    db: AsyncSession,
    library: Library,
    attribute_definition_id: uuid.UUID,
    **changes: Any,
) -> AttributeDefinition:
    row, ontology = await _child_with_ontology(
        db,
        library,
        AttributeDefinition,
        attribute_definition_id,
        "attribute definition",
    )
    require_schema_row_mutable(ontology, row)
    owner_kind = changes.get("owner_kind", row.owner_kind)
    owner_type_id = changes.get("owner_type_id", row.owner_type_id)
    await _require_attribute_owner_scope(db, library.id, ontology.id, owner_kind, owner_type_id)
    if changes.get("value_type", row.value_type) == "enum" and changes.get("enum_values", row.enum_values) is None:
        raise ValueError("enum attribute definitions require enum_values")
    _apply_changes(row, changes)
    await db.flush()
    return row


async def delete_attribute_definition(
    db: AsyncSession,
    library: Library,
    attribute_definition_id: uuid.UUID,
) -> AttributeDefinition:
    row, ontology = await _child_with_ontology(
        db,
        library,
        AttributeDefinition,
        attribute_definition_id,
        "attribute definition",
    )
    require_schema_row_mutable(ontology, row)
    row.status = SCHEMA_STATUS_DELETED
    await db.flush()
    return row


async def _require_attribute_owner_scope(
    db: AsyncSession,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    owner_kind: str,
    owner_type_id: uuid.UUID,
) -> None:
    if owner_kind == ATTRIBUTE_OWNER_ENTITY_TYPE:
        owner = await _get(db, EntityType, owner_type_id, "attribute owner entity type")
    elif owner_kind == ATTRIBUTE_OWNER_RELATION_TYPE:
        owner = await _get(db, RelationType, owner_type_id, "attribute owner relation type")
    else:
        raise ValueError("attribute owner_kind must be entity_type or relation_type")
    _require_same_scope(owner, library_id, ontology_version_id, "attribute owner")
