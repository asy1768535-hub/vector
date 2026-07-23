from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.schemas.schema_lifecycle import (
    SchemaAttributeRead,
    SchemaConstraintRead,
    SchemaEntityTypeRead,
    SchemaRelationTypeRead,
    SchemaVersionDetailRead,
    SchemaVersionListRead,
    SchemaVersionSummaryRead,
)
from app.services.schema_lifecycle_contracts import (
    SchemaLifecycleError,
    canonical_schema_lifecycle_state_hash,
)


_HIDDEN = "deleted"
_MAX_VERSIONS = 100


@dataclass(frozen=True, slots=True)
class SchemaVersionBundle:
    version: OntologyVersion
    entity_types: tuple[EntityType, ...]
    relation_types: tuple[RelationType, ...]
    attributes: tuple[AttributeDefinition, ...]
    constraints: tuple[RelationTypeConstraint, ...]


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _entity_payload(row: EntityType) -> dict[str, Any]:
    return {
        "description": row.description,
        "id": str(row.id),
        "is_seeded": row.is_seeded,
        "key": row.key,
        "label": row.label,
        "library_id": str(row.library_id),
        "ontology_version_id": str(row.ontology_version_id),
        "properties_schema": row.properties_schema,
        "status": row.status,
    }


def _relation_payload(row: RelationType) -> dict[str, Any]:
    return {
        **_entity_payload(row),
        "default_review_policy": row.default_review_policy,
        "direction": row.direction,
        "requires_evidence": row.requires_evidence,
    }


def _attribute_payload(row: AttributeDefinition) -> dict[str, Any]:
    return {
        "enum_values": row.enum_values,
        "id": str(row.id),
        "indexed": row.indexed,
        "key": row.key,
        "label": row.label,
        "library_id": str(row.library_id),
        "ontology_version_id": str(row.ontology_version_id),
        "owner_kind": row.owner_kind,
        "owner_type_id": str(row.owner_type_id),
        "required": row.required,
        "status": row.status,
        "validation_schema": row.validation_schema,
        "value_type": row.value_type,
    }


def _constraint_payload(row: RelationTypeConstraint) -> dict[str, Any]:
    return {
        "cardinality": row.cardinality,
        "id": str(row.id),
        "library_id": str(row.library_id),
        "ontology_version_id": str(row.ontology_version_id),
        "relation_type_id": str(row.relation_type_id),
        "requires_review": row.requires_review,
        "source_entity_type_id": str(row.source_entity_type_id),
        "status": row.status,
        "target_entity_type_id": str(row.target_entity_type_id),
    }


def _row_hash(payload: dict[str, Any]) -> str:
    return canonical_schema_lifecycle_state_hash(payload)


def schema_version_state_payload(bundle: SchemaVersionBundle) -> dict[str, Any]:
    version = bundle.version
    return {
        "attributes": sorted(
            (_attribute_payload(row) for row in bundle.attributes),
            key=lambda value: (
                value["owner_kind"],
                value["owner_type_id"],
                value["key"],
                value["id"],
            ),
        ),
        "constraints": sorted(
            (_constraint_payload(row) for row in bundle.constraints),
            key=lambda value: (
                value["relation_type_id"],
                value["source_entity_type_id"],
                value["target_entity_type_id"],
                value["id"],
            ),
        ),
        "entity_types": sorted(
            (_entity_payload(row) for row in bundle.entity_types),
            key=lambda value: (value["key"], value["id"]),
        ),
        "relation_types": sorted(
            (_relation_payload(row) for row in bundle.relation_types),
            key=lambda value: (value["key"], value["id"]),
        ),
        "version": {
            "description": version.description,
            "id": str(version.id),
            "library_id": str(version.library_id),
            "parent_version_id": (
                str(version.parent_version_id)
                if version.parent_version_id is not None
                else None
            ),
            "published_at": _time(version.published_at),
            "status": version.status,
            "version_key": version.version_key,
            "version_no": version.version_no,
        },
    }


def schema_version_state_hash(bundle: SchemaVersionBundle) -> str:
    return canonical_schema_lifecycle_state_hash(schema_version_state_payload(bundle))


def _assert_bundle_scope(bundle: SchemaVersionBundle) -> None:
    version = bundle.version
    for row in (
        *bundle.entity_types,
        *bundle.relation_types,
        *bundle.attributes,
        *bundle.constraints,
    ):
        if (
            row.library_id != version.library_id
            or row.ontology_version_id != version.id
        ):
            raise SchemaLifecycleError(
                "schema_lifecycle_unavailable", "Stored Schema scope is invalid"
            )


def schema_version_detail(
    library: Library,
    bundle: SchemaVersionBundle,
) -> SchemaVersionDetailRead:
    _assert_bundle_scope(bundle)
    version = bundle.version
    if version.library_id != library.id or version.status == _HIDDEN:
        raise SchemaLifecycleError(
            "schema_lifecycle_not_found", "Schema version not found"
        )
    entity_by_id = {row.id: row for row in bundle.entity_types}
    relation_by_id = {row.id: row for row in bundle.relation_types}
    try:
        constraints = [
            SchemaConstraintRead(
                **_constraint_payload(row),
                relation_type_key=relation_by_id[row.relation_type_id].key,
                source_entity_type_key=entity_by_id[row.source_entity_type_id].key,
                target_entity_type_key=entity_by_id[row.target_entity_type_id].key,
                state_hash=_row_hash(_constraint_payload(row)),
            )
            for row in sorted(
                bundle.constraints,
                key=lambda item: (
                    relation_by_id[item.relation_type_id].key,
                    entity_by_id[item.source_entity_type_id].key,
                    entity_by_id[item.target_entity_type_id].key,
                    str(item.id),
                ),
            )
        ]
    except KeyError as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_unavailable", "Stored Schema references are invalid"
        ) from exc
    entities = [
        SchemaEntityTypeRead(
            **_entity_payload(row), state_hash=_row_hash(_entity_payload(row))
        )
        for row in sorted(bundle.entity_types, key=lambda item: (item.key, str(item.id)))
    ]
    relations = [
        SchemaRelationTypeRead(
            **_relation_payload(row), state_hash=_row_hash(_relation_payload(row))
        )
        for row in sorted(
            bundle.relation_types, key=lambda item: (item.key, str(item.id))
        )
    ]
    attributes = [
        SchemaAttributeRead(
            **_attribute_payload(row), state_hash=_row_hash(_attribute_payload(row))
        )
        for row in sorted(
            bundle.attributes,
            key=lambda item: (
                item.owner_kind,
                str(item.owner_type_id),
                item.key,
                str(item.id),
            ),
        )
    ]
    return SchemaVersionDetailRead(
        id=version.id,
        library_id=version.library_id,
        library_slug=library.slug,
        version_key=version.version_key,
        version_no=version.version_no,
        status=version.status,
        description=version.description,
        parent_version_id=version.parent_version_id,
        published_at=version.published_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
        state_hash=schema_version_state_hash(bundle),
        entity_type_count=len(entities),
        relation_type_count=len(relations),
        attribute_count=len(attributes),
        constraint_count=len(constraints),
        entity_types=entities,
        relation_types=relations,
        attributes=attributes,
        constraints=constraints,
    )


def schema_version_summary(
    library: Library,
    bundle: SchemaVersionBundle,
) -> SchemaVersionSummaryRead:
    detail = schema_version_detail(library, bundle)
    return SchemaVersionSummaryRead(**detail.model_dump(exclude={
        "entity_types", "relation_types", "attributes", "constraints"
    }))


def _visible(rows: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(row for row in rows if row.status != _HIDDEN)


def _group(rows: Iterable[Any]) -> dict[uuid.UUID, list[Any]]:
    grouped: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row.ontology_version_id].append(row)
    return grouped


def build_schema_version_bundles(
    versions: Sequence[OntologyVersion],
    entity_types: Iterable[EntityType],
    relation_types: Iterable[RelationType],
    attributes: Iterable[AttributeDefinition],
    constraints: Iterable[RelationTypeConstraint],
) -> tuple[SchemaVersionBundle, ...]:
    entities_by_version = _group(entity_types)
    relations_by_version = _group(relation_types)
    attributes_by_version = _group(attributes)
    constraints_by_version = _group(constraints)
    return tuple(
        SchemaVersionBundle(
            version=version,
            entity_types=_visible(entities_by_version.get(version.id, ())),
            relation_types=_visible(relations_by_version.get(version.id, ())),
            attributes=_visible(attributes_by_version.get(version.id, ())),
            constraints=_visible(constraints_by_version.get(version.id, ())),
        )
        for version in versions
    )


async def load_schema_version_bundle(
    db: AsyncSession,
    library: Library,
    version_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SchemaVersionBundle:
    statement = select(OntologyVersion).where(
        OntologyVersion.id == version_id,
        OntologyVersion.library_id == library.id,
        OntologyVersion.status != _HIDDEN,
    )
    if for_update:
        statement = statement.with_for_update()
    version = (await db.execute(statement)).scalars().first()
    if version is None:
        raise SchemaLifecycleError(
            "schema_lifecycle_not_found", "Schema version not found"
        )
    results = []
    for model in (
        EntityType,
        RelationType,
        AttributeDefinition,
        RelationTypeConstraint,
    ):
        result = await db.execute(
            select(model).where(
                model.library_id == library.id,
                model.ontology_version_id == version.id,
                model.status != _HIDDEN,
            )
        )
        results.append(result.scalars().all())
    return build_schema_version_bundles((version,), *results)[0]


async def list_schema_versions(
    db: AsyncSession,
    library: Library,
) -> SchemaVersionListRead:
    versions = (
        await db.execute(
            select(OntologyVersion)
            .where(
                OntologyVersion.library_id == library.id,
                OntologyVersion.status != _HIDDEN,
            )
            .order_by(
                OntologyVersion.version_key,
                OntologyVersion.version_no.desc(),
                OntologyVersion.id,
            )
            .limit(_MAX_VERSIONS + 1)
        )
    ).scalars().all()
    if len(versions) > _MAX_VERSIONS:
        raise SchemaLifecycleError(
            "schema_lifecycle_unavailable", "Schema version limit exceeded"
        )
    version_ids = tuple(row.id for row in versions)
    results: list[Sequence[Any]] = []
    for model in (
        EntityType,
        RelationType,
        AttributeDefinition,
        RelationTypeConstraint,
    ):
        if not version_ids:
            results.append(())
            continue
        result = await db.execute(
            select(model).where(
                model.library_id == library.id,
                model.ontology_version_id.in_(version_ids),
                model.status != _HIDDEN,
            )
        )
        results.append(result.scalars().all())
    bundles = build_schema_version_bundles(versions, *results)
    return SchemaVersionListRead(
        library_id=library.id,
        library_slug=library.slug,
        versions=[schema_version_summary(library, bundle) for bundle in bundles],
    )
