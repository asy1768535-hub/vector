from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.services.graph_schema_validator import count_active_relation_evidence


GRAPH_BINDING_STATUS_ACTIVE = "active"
GRAPH_BINDING_STATUS_STALE = "stale"
GRAPH_RELATION_STATUS_ACTIVE = "active"
GRAPH_RELATION_STATUS_STALE = "stale"


@dataclass(frozen=True)
class GraphStaleLifecycleResult:
    stale_entity_mentions: int
    stale_relation_evidence: int
    stale_relations: int


async def _reconcile_publications(db: AsyncSession, library: Library) -> None:
    if not settings.graph_publication_enabled:
        return
    from app.services import graph_publication_reconcile

    await graph_publication_reconcile.reconcile_library_current_publications(db, library)


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
    if support_type == "contradicts" and status == GRAPH_BINDING_STATUS_ACTIVE:
        await _reconcile_publications(db, library)
    return row


async def mark_document_revision_graph_evidence_stale(
    db: AsyncSession,
    library: Library,
    *,
    document_revision_id: uuid.UUID,
) -> GraphStaleLifecycleResult:
    mentions = await _list_entity_mentions(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        document_revision_id=document_revision_id,
    )
    relation_evidence_rows = await _list_relation_evidence(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        document_revision_id=document_revision_id,
    )
    stale_mentions = _mark_rows_stale(mentions)
    affected_relation_ids = {row.relation_id for row in relation_evidence_rows}
    stale_relation_evidence = _mark_rows_stale(relation_evidence_rows)
    stale_relations = await _mark_relations_without_active_evidence_stale(
        db,
        library,
        affected_relation_ids,
        flush=False,
    )
    if stale_mentions or stale_relation_evidence or stale_relations:
        await db.flush()
        await _reconcile_publications(db, library)
    return GraphStaleLifecycleResult(
        stale_entity_mentions=stale_mentions,
        stale_relation_evidence=stale_relation_evidence,
        stale_relations=stale_relations,
    )


async def mark_document_graph_evidence_stale(
    db: AsyncSession,
    library: Library,
    *,
    document_id: uuid.UUID,
) -> GraphStaleLifecycleResult:
    mentions = await _list_entity_mentions(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        document_id=document_id,
    )
    relation_evidence_rows = await _list_relation_evidence(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        document_id=document_id,
    )
    stale_mentions = _mark_rows_stale(mentions)
    affected_relation_ids = {row.relation_id for row in relation_evidence_rows}
    stale_relation_evidence = _mark_rows_stale(relation_evidence_rows)
    stale_relations = await _mark_relations_without_active_evidence_stale(
        db,
        library,
        affected_relation_ids,
        flush=False,
    )
    if stale_mentions or stale_relation_evidence or stale_relations:
        await db.flush()
        await _reconcile_publications(db, library)
    return GraphStaleLifecycleResult(
        stale_entity_mentions=stale_mentions,
        stale_relation_evidence=stale_relation_evidence,
        stale_relations=stale_relations,
    )


# called by: none yet - future evidence invalidation path
async def mark_evidence_unit_graph_evidence_stale(
    db: AsyncSession,
    library: Library,
    *,
    evidence_id: uuid.UUID,
) -> GraphStaleLifecycleResult:
    evidence = await db.get(EvidenceUnit, evidence_id)
    if evidence is None or evidence.library_id != library.id:
        raise LookupError("evidence not found")

    mentions = await _list_entity_mentions(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        evidence_id=evidence_id,
    )
    relation_evidence_rows = await _list_relation_evidence(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        evidence_id=evidence_id,
    )
    stale_mentions = _mark_rows_stale(mentions)
    affected_relation_ids = {row.relation_id for row in relation_evidence_rows}
    stale_relation_evidence = _mark_rows_stale(relation_evidence_rows)
    stale_relations = await _mark_relations_without_active_evidence_stale(
        db,
        library,
        affected_relation_ids,
        flush=False,
    )
    if stale_mentions or stale_relation_evidence or stale_relations:
        await db.flush()
        await _reconcile_publications(db, library)
    return GraphStaleLifecycleResult(
        stale_entity_mentions=stale_mentions,
        stale_relation_evidence=stale_relation_evidence,
        stale_relations=stale_relations,
    )


async def mark_relations_without_active_evidence_stale(
    db: AsyncSession,
    library: Library,
    relation_ids: Iterable[uuid.UUID],
) -> int:
    stale_count = await _mark_relations_without_active_evidence_stale(
        db,
        library,
        relation_ids,
        flush=True,
    )
    if stale_count:
        await _reconcile_publications(db, library)
    return stale_count


async def list_active_entity_mentions(
    db: AsyncSession,
    library: Library,
    *,
    entity_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    document_revision_id: uuid.UUID | None = None,
) -> list[EntityMention]:
    return await _list_entity_mentions(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        entity_id=entity_id,
        evidence_id=evidence_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
    )


async def list_active_relation_evidence(
    db: AsyncSession,
    library: Library,
    *,
    relation_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    document_revision_id: uuid.UUID | None = None,
) -> list[RelationEvidence]:
    return await _list_relation_evidence(
        db,
        library,
        status=GRAPH_BINDING_STATUS_ACTIVE,
        relation_id=relation_id,
        evidence_id=evidence_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
    )


async def list_active_knowledge_relations(
    db: AsyncSession,
    library: Library,
    *,
    relation_type_id: uuid.UUID | None = None,
    source_entity_id: uuid.UUID | None = None,
    target_entity_id: uuid.UUID | None = None,
) -> list[KnowledgeRelation]:
    """Return lifecycle-active rows; publication-accurate reads must use v0.5 helpers."""
    stmt = select(KnowledgeRelation).where(
        KnowledgeRelation.library_id == library.id,
        KnowledgeRelation.status == GRAPH_RELATION_STATUS_ACTIVE,
    )
    if relation_type_id is not None:
        stmt = stmt.where(KnowledgeRelation.relation_type_id == relation_type_id)
    if source_entity_id is not None:
        stmt = stmt.where(KnowledgeRelation.source_entity_id == source_entity_id)
    if target_entity_id is not None:
        stmt = stmt.where(KnowledgeRelation.target_entity_id == target_entity_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _mark_relations_without_active_evidence_stale(
    db: AsyncSession,
    library: Library,
    relation_ids: Iterable[uuid.UUID],
    *,
    flush: bool,
) -> int:
    stale_count = 0
    for relation_id in set(relation_ids):
        relation = await db.get(KnowledgeRelation, relation_id)
        if relation is None or relation.library_id != library.id or relation.status != GRAPH_RELATION_STATUS_ACTIVE:
            continue
        active_evidence_count = await count_active_relation_evidence(db, library, relation_id)
        if active_evidence_count == 0:
            relation.status = GRAPH_RELATION_STATUS_STALE
            stale_count += 1
    if flush and stale_count:
        await db.flush()
    return stale_count


async def _list_entity_mentions(
    db: AsyncSession,
    library: Library,
    *,
    status: str,
    entity_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    document_revision_id: uuid.UUID | None = None,
) -> list[EntityMention]:
    stmt = select(EntityMention).where(
        EntityMention.library_id == library.id,
        EntityMention.status == status,
    )
    if entity_id is not None:
        stmt = stmt.where(EntityMention.entity_id == entity_id)
    if evidence_id is not None:
        stmt = stmt.where(EntityMention.evidence_id == evidence_id)
    if document_id is not None:
        stmt = stmt.where(EntityMention.document_id == document_id)
    if document_revision_id is not None:
        stmt = stmt.where(EntityMention.document_revision_id == document_revision_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _list_relation_evidence(
    db: AsyncSession,
    library: Library,
    *,
    status: str,
    relation_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    document_revision_id: uuid.UUID | None = None,
) -> list[RelationEvidence]:
    stmt = select(RelationEvidence).where(
        RelationEvidence.library_id == library.id,
        RelationEvidence.status == status,
    )
    if relation_id is not None:
        stmt = stmt.where(RelationEvidence.relation_id == relation_id)
    if evidence_id is not None:
        stmt = stmt.where(RelationEvidence.evidence_id == evidence_id)
    if document_id is not None:
        stmt = stmt.where(RelationEvidence.document_id == document_id)
    if document_revision_id is not None:
        stmt = stmt.where(RelationEvidence.document_revision_id == document_revision_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _mark_rows_stale(rows: Iterable[Any]) -> int:
    count = 0
    for row in rows:
        row.status = GRAPH_BINDING_STATUS_STALE
        count += 1
    return count
