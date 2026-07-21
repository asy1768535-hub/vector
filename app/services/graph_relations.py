from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import GRAPH_FACT_STATUS_ACTIVE
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.schemas.v03_graph import GraphRelationCreate, RelationEvidenceCreate
from app.services import graph_evidence, graph_schema_validator


async def create_relation(
    db: AsyncSession,
    library: Library,
    payload: GraphRelationCreate,
) -> KnowledgeRelation:
    validation = await graph_schema_validator.validate_relation_write(
        db,
        library,
        relation_type_id=payload.relation_type_id,
        source_entity_id=payload.source_entity_id,
        target_entity_id=payload.target_entity_id,
        properties=payload.properties,
        requested_status=payload.status,
        source_type=payload.source_type,
        confidence=payload.confidence,
        schema_boundary_clear=payload.schema_boundary_clear,
    )
    if payload.status == GRAPH_FACT_STATUS_ACTIVE:
        if validation.relation_type.requires_evidence and validation.active_evidence_count < 1:
            raise ValueError("active relation requires active evidence before creation")
        raise ValueError("active relation creation is not supported in v0.3 M6")
    if validation.status == GRAPH_FACT_STATUS_ACTIVE:
        raise ValueError("active relation creation is not supported in v0.3 M6")

    row = KnowledgeRelation(
        library_id=library.id,
        ontology_version_id=validation.source_entity.ontology_version_id,
        relation_type_id=validation.relation_type.id,
        source_entity_id=validation.source_entity.id,
        target_entity_id=validation.target_entity.id,
        properties=validation.properties,
        status=validation.status,
        review_status=validation.review_status,
        source_type=validation.source_type,
        confidence=validation.confidence,
    )
    db.add(row)
    await db.flush()
    return row


async def get_relation(
    db: AsyncSession,
    library: Library,
    relation_id: uuid.UUID,
) -> KnowledgeRelation:
    relation = await db.get(KnowledgeRelation, relation_id)
    if relation is None or relation.library_id != library.id or relation.status == "deleted":
        raise LookupError("relation not found")
    return relation


async def bind_relation_evidence(
    db: AsyncSession,
    library: Library,
    relation_id: uuid.UUID,
    payload: RelationEvidenceCreate,
) -> RelationEvidence:
    return await graph_evidence.create_relation_evidence(
        db,
        library,
        relation_id=relation_id,
        evidence_id=payload.evidence_id,
        support_type=payload.support_type,
        chunk_id=payload.chunk_id,
        source_span=payload.source_span,
        confidence=payload.confidence,
    )
