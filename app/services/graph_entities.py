from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.models.library import Library
from app.schemas.v03_graph import GraphEntityCreate
from app.services import graph_schema_validator


async def create_entity(
    db: AsyncSession,
    library: Library,
    payload: GraphEntityCreate,
) -> Entity:
    validation = await graph_schema_validator.validate_entity_write(
        db,
        library,
        ontology_version_id=payload.ontology_version_id,
        entity_type_id=payload.entity_type_id,
        canonical_name=payload.canonical_name,
        properties=payload.properties,
        requested_status=payload.status,
        source_type=payload.source_type,
        confidence=payload.confidence,
    )
    row = Entity(
        library_id=library.id,
        ontology_version_id=validation.ontology_version.id,
        entity_type_id=validation.entity_type.id,
        canonical_name=validation.canonical_name,
        normalized_name=validation.normalized_name,
        properties=validation.properties,
        status=validation.status,
        source_type=validation.source_type,
        confidence=validation.confidence,
    )
    db.add(row)
    await db.flush()
    return row
