from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence


async def require_active_graph_evidence(
    db: AsyncSession,
    library: Library,
    evidence_id: uuid.UUID,
) -> EvidenceUnit:
    evidence = await db.get(EvidenceUnit, evidence_id)
    if evidence is None or evidence.library_id != library.id or evidence.status != EVIDENCE_STATUS_ACTIVE:
        raise LookupError("evidence not found")
    return evidence


async def create_entity_mention(
    db: AsyncSession,
    library: Library,
    *,
    entity_id: uuid.UUID,
    evidence_id: uuid.UUID,
    mention_text: str,
    normalized_text: str | None = None,
    chunk_id: uuid.UUID | None = None,
    source_span: dict[str, Any] | None = None,
    confidence: float | None = None,
    source_type: str = "manual",
    status: str = "active",
) -> EntityMention:
    evidence = await require_active_graph_evidence(db, library, evidence_id)
    entity = await db.get(Entity, entity_id)
    if entity is None or entity.library_id != library.id:
        raise LookupError("entity not found")

    evidence_text = evidence.text_quote
    row = EntityMention(
        library_id=library.id,
        entity_id=entity.id,
        evidence_id=evidence.id,
        document_id=evidence.document_id,
        document_revision_id=evidence.document_revision_id,
        chunk_id=chunk_id,
        mention_text=mention_text,
        normalized_text=normalized_text,
        quote_text=evidence_text,
        evidence_text_snapshot=evidence_text,
        source_span=source_span,
        confidence=confidence,
        source_type=source_type,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def create_relation_evidence(
    db: AsyncSession,
    library: Library,
    *,
    relation_id: uuid.UUID,
    evidence_id: uuid.UUID,
    support_type: str = "supports",
    chunk_id: uuid.UUID | None = None,
    source_span: dict[str, Any] | None = None,
    confidence: float | None = None,
    status: str = "active",
) -> RelationEvidence:
    evidence = await require_active_graph_evidence(db, library, evidence_id)
    relation = await db.get(KnowledgeRelation, relation_id)
    if relation is None or relation.library_id != library.id:
        raise LookupError("relation not found")

    evidence_text = evidence.text_quote
    row = RelationEvidence(
        library_id=library.id,
        relation_id=relation.id,
        evidence_id=evidence.id,
        document_id=evidence.document_id,
        document_revision_id=evidence.document_revision_id,
        chunk_id=chunk_id,
        support_type=support_type,
        quote_text=evidence_text,
        evidence_text_snapshot=evidence_text,
        source_span=source_span,
        confidence=confidence,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row
