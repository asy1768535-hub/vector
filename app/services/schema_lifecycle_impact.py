from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.models.user import User
from app.schemas.schema_lifecycle import (
    SchemaCompatibilityPreviewRead,
    SchemaDiffGroupRead,
    SchemaImpactRead,
    SchemaReferenceCountsRead,
)
from app.services.graph_canonical import canonical_graph_value_hash_v1
from app.services.organization_authorization import list_accessible_libraries
from app.services.schema_lifecycle_contracts import SchemaLifecycleError
from app.services.schema_lifecycle_read import (
    SchemaVersionBundle,
    build_schema_version_bundles,
    load_schema_version_bundle,
    schema_version_state_hash,
)


def _enabled_status(bundle: SchemaVersionBundle) -> str:
    return "draft" if bundle.version.status == "draft" else "active"


def _semantic_maps(bundle: SchemaVersionBundle) -> dict[str, dict[str, dict[str, Any]]]:
    enabled = _enabled_status(bundle)
    entities = {row.id: row for row in bundle.entity_types if row.status == enabled}
    relations = {row.id: row for row in bundle.relation_types if row.status == enabled}
    entity_values = {
        row.key: {
            "description": row.description,
            "key": row.key,
            "label": row.label,
            "properties_schema": row.properties_schema,
        }
        for row in entities.values()
    }
    relation_values = {
        row.key: {
            "default_review_policy": row.default_review_policy,
            "description": row.description,
            "direction": row.direction,
            "key": row.key,
            "label": row.label,
            "properties_schema": row.properties_schema,
            "requires_evidence": row.requires_evidence,
        }
        for row in relations.values()
    }
    attributes: dict[str, dict[str, Any]] = {}
    constraints: dict[str, dict[str, Any]] = {}
    try:
        for row in bundle.attributes:
            if row.status != enabled:
                continue
            owners = entities if row.owner_kind == "entity_type" else relations
            owner_key = owners[row.owner_type_id].key
            key = f"{row.owner_kind}:{owner_key}:{row.key}"
            attributes[key] = {
                "enum_values": row.enum_values,
                "indexed": row.indexed,
                "key": row.key,
                "label": row.label,
                "owner_kind": row.owner_kind,
                "owner_type_key": owner_key,
                "required": row.required,
                "validation_schema": row.validation_schema,
                "value_type": row.value_type,
            }
        for row in bundle.constraints:
            if row.status != enabled:
                continue
            relation_key = relations[row.relation_type_id].key
            source_key = entities[row.source_entity_type_id].key
            target_key = entities[row.target_entity_type_id].key
            key = f"{relation_key}:{source_key}:{target_key}"
            constraints[key] = {
                "cardinality": row.cardinality,
                "relation_type_key": relation_key,
                "requires_review": row.requires_review,
                "source_entity_type_key": source_key,
                "target_entity_type_key": target_key,
            }
    except KeyError as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_invalid_draft", "Schema references are invalid"
        ) from exc
    return {
        "entity_types": entity_values,
        "relation_types": relation_values,
        "attributes": attributes,
        "constraints": constraints,
    }


def schema_bundle_semantic_hash(bundle: SchemaVersionBundle) -> str:
    return canonical_graph_value_hash_v1(_semantic_maps(bundle))


def schema_bundle_diff(
    draft: SchemaVersionBundle,
    parent: SchemaVersionBundle | None,
) -> dict[str, SchemaDiffGroupRead]:
    draft_maps = _semantic_maps(draft)
    parent_maps = _semantic_maps(parent) if parent is not None else {
        kind: {} for kind in draft_maps
    }
    result: dict[str, SchemaDiffGroupRead] = {}
    for kind in ("entity_types", "relation_types", "attributes", "constraints"):
        current = draft_maps[kind]
        previous = parent_maps[kind]
        current_keys = set(current)
        previous_keys = set(previous)
        result[kind] = SchemaDiffGroupRead(
            added=sorted(current_keys - previous_keys),
            removed=sorted(previous_keys - current_keys),
            changed=sorted(
                key
                for key in current_keys & previous_keys
                if canonical_graph_value_hash_v1(current[key])
                != canonical_graph_value_hash_v1(previous[key])
            ),
        )
    return result


async def _reference_counts(
    db: AsyncSession,
    model,
    library_id: uuid.UUID,
    source_id: uuid.UUID | None,
    draft_id: uuid.UUID,
) -> dict[uuid.UUID, int]:
    ids = tuple(item for item in (source_id, draft_id) if item is not None)
    rows = (
        await db.execute(
            select(model.ontology_version_id, func.count(model.id))
            .where(
                model.library_id == library_id,
                model.ontology_version_id.in_(ids),
            )
            .group_by(model.ontology_version_id)
        )
    ).all()
    return {version_id: count for version_id, count in rows}


async def _active_bundles(
    db: AsyncSession,
    libraries: tuple[Library, ...],
) -> dict[uuid.UUID, SchemaVersionBundle | None]:
    if not libraries:
        return {}
    library_ids = tuple(row.id for row in libraries)
    versions = (
        await db.execute(
            select(OntologyVersion).where(
                OntologyVersion.library_id.in_(library_ids),
                OntologyVersion.status == "active",
            ).order_by(
                OntologyVersion.library_id,
                OntologyVersion.version_key,
                OntologyVersion.version_no.desc(),
                OntologyVersion.id,
            )
        )
    ).scalars().all()
    by_library: dict[uuid.UUID, list[OntologyVersion]] = defaultdict(list)
    for version in versions:
        by_library[version.library_id].append(version)
    selected = [rows[0] for rows in by_library.values() if len(rows) == 1]
    version_ids = tuple(row.id for row in selected)
    collections = []
    for model in (
        EntityType,
        RelationType,
        AttributeDefinition,
        RelationTypeConstraint,
    ):
        rows = (
            await db.execute(
                select(model).where(
                    model.library_id.in_(library_ids),
                    model.ontology_version_id.in_(version_ids),
                    model.status == "active",
                )
            )
        ).scalars().all() if version_ids else []
        collections.append(rows)
    bundles = build_schema_version_bundles(selected, *collections)
    bundle_by_library = {bundle.version.library_id: bundle for bundle in bundles}
    return {
        library.id: (
            bundle_by_library.get(library.id)
            if len(by_library.get(library.id, ())) == 1
            else None
        )
        for library in libraries
    }


async def preview_schema_impact(
    db: AsyncSession,
    *,
    user: User,
    library: Library,
    draft: SchemaVersionBundle,
) -> SchemaImpactRead:
    if draft.version.library_id != library.id or draft.version.status != "draft":
        raise SchemaLifecycleError(
            "schema_lifecycle_invalid_draft", "Schema impact requires a draft"
        )
    parent = None
    if draft.version.parent_version_id is not None:
        parent = await load_schema_version_bundle(
            db, library, draft.version.parent_version_id
        )
    diff = schema_bundle_diff(draft, parent)
    source_id = parent.version.id if parent is not None else None
    job_counts = await _reference_counts(
        db, GraphExtractionJob, library.id, source_id, draft.version.id
    )
    publication_counts = await _reference_counts(
        db, GraphPublication, library.id, source_id, draft.version.id
    )
    current_rows = (
        await db.execute(
            select(GraphPublication.id, GraphPublication.ontology_version_id).where(
                GraphPublication.library_id == library.id,
                GraphPublication.ontology_version_id.in_(
                    tuple(
                        item
                        for item in (source_id, draft.version.id)
                        if item is not None
                    )
                ),
                GraphPublication.status.in_(("active", "degraded")),
            )
            .order_by(GraphPublication.id)
        )
    ).all()
    current_by_version: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for publication_id, version_id in current_rows:
        current_by_version[version_id].append(publication_id)

    accessible = await list_accessible_libraries(db, user=user, action="read")
    by_id = {
        row.id: row
        for row in accessible
        if row.organization_id == library.organization_id
    }
    by_id[library.id] = library
    comparison_libraries = tuple(
        sorted(by_id.values(), key=lambda row: (row.slug, str(row.id)))[:20]
    )
    active = await _active_bundles(db, comparison_libraries)
    draft_hash = schema_bundle_semantic_hash(draft)
    compatibility = []
    for row in comparison_libraries:
        bundle = active.get(row.id)
        if bundle is None:
            result = "unavailable"
        else:
            result = (
                "match"
                if schema_bundle_semantic_hash(bundle) == draft_hash
                else "mismatch"
            )
        compatibility.append(
            SchemaCompatibilityPreviewRead(
                library_id=row.id,
                library_slug=row.slug,
                result=result,
            )
        )
    return SchemaImpactRead(
        library_id=library.id,
        ontology_version_id=draft.version.id,
        parent_version_id=source_id,
        version_state_hash=schema_version_state_hash(draft),
        entity_types=diff["entity_types"],
        relation_types=diff["relation_types"],
        attributes=diff["attributes"],
        constraints=diff["constraints"],
        extraction_jobs=SchemaReferenceCountsRead(
            source_version_count=job_counts.get(source_id, 0) if source_id else 0,
            draft_version_count=job_counts.get(draft.version.id, 0),
            source_current_ids=[],
            draft_current_ids=[],
        ),
        publications=SchemaReferenceCountsRead(
            source_version_count=(
                publication_counts.get(source_id, 0) if source_id else 0
            ),
            draft_version_count=publication_counts.get(draft.version.id, 0),
            source_current_ids=(current_by_version.get(source_id, []) if source_id else []),
            draft_current_ids=current_by_version.get(draft.version.id, []),
        ),
        compatibility=compatibility,
        historical_rows_migrated=False,
        retrieval_scope_changed=False,
    )
