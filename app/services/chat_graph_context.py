from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity_mention import EntityMention
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.schemas.chat_graph_context import ChatGraphContextResponse
from app.schemas.v06_graph_retrieval import (
    GraphRetrievalQueryRequest,
    GraphRetrievalQueryResponse,
    GraphRetrievalSeed,
)
from app.services import graph_retrieval


MAX_CONTEXT_NODES = 30
MAX_CONTEXT_RELATIONS = 50
MAX_CONTEXT_HOPS = 1
MAX_CONTEXT_EVIDENCE_IDS = 200
MAX_CONTEXT_FACT_ROWS = 500


class ChatGraphContextServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ChatGraphQueryResult:
    graph: GraphRetrievalQueryResponse | None
    exact_fact_count: int = 0
    exact_seed_count: int = 0
    exact_seeds_truncated: bool = False


@dataclass(slots=True)
class _PublicationCandidate:
    publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    publication_status: str
    activated_at: datetime | None
    direct_entity_ids: set[uuid.UUID] = field(default_factory=set)
    endpoint_entity_ids: set[uuid.UUID] = field(default_factory=set)
    fact_ids: set[tuple[str, uuid.UUID]] = field(default_factory=set)


def _candidate_sort_key(candidate: _PublicationCandidate) -> tuple:
    activated_at = candidate.activated_at
    activated_rank = activated_at.timestamp() if activated_at is not None else float("-inf")
    return (-len(candidate.fact_ids), -activated_rank, str(candidate.publication_id))


def _select_candidate(rows: Sequence[object]) -> _PublicationCandidate | None:
    candidates: dict[uuid.UUID, _PublicationCandidate] = {}
    for row in rows:
        candidate = candidates.setdefault(
            row.publication_id,
            _PublicationCandidate(
                publication_id=row.publication_id,
                ontology_version_id=row.ontology_version_id,
                publication_status=row.publication_status,
                activated_at=row.activated_at,
            ),
        )
        if (
            candidate.ontology_version_id != row.ontology_version_id
            or candidate.publication_status != row.publication_status
            or candidate.activated_at != row.activated_at
        ):
            raise ChatGraphContextServiceError("graph_publication_invariant_failed")
        if row.item_kind == "entity" and row.entity_id is not None and row.relation_id is None:
            candidate.direct_entity_ids.add(row.entity_id)
            candidate.fact_ids.add(("entity", row.entity_id))
        elif row.item_kind == "relation" and row.relation_id is not None and row.entity_id is None:
            if row.source_entity_id is None or row.target_entity_id is None:
                raise ChatGraphContextServiceError("graph_publication_invariant_failed")
            candidate.endpoint_entity_ids.update((row.source_entity_id, row.target_entity_id))
            candidate.fact_ids.add(("relation", row.relation_id))
        else:
            raise ChatGraphContextServiceError("graph_publication_invariant_failed")
    if not candidates:
        return None
    return min(candidates.values(), key=_candidate_sort_key)


def _bounded_evidence_ids(*groups: Sequence[uuid.UUID | None]) -> tuple[uuid.UUID, ...]:
    evidence_ids = {
        evidence_id
        for group in groups
        for evidence_id in group
        if evidence_id is not None
    }
    if len(evidence_ids) > MAX_CONTEXT_EVIDENCE_IDS:
        raise ChatGraphContextServiceError("graph_retrieval_limit_exceeded")
    return tuple(sorted(evidence_ids, key=str))


async def _load_visible_chunk(
    db: AsyncSession,
    library: Library,
    chunk_id: uuid.UUID,
) -> Chunk:
    chunk = (
        await db.execute(
            select(Chunk)
            .join(
                Document,
                and_(
                    Document.id == Chunk.document_id,
                    Document.library_id == library.id,
                    Document.deleted_at.is_(None),
                    Document.status == "ready",
                    Document.current_revision_id == Chunk.document_revision_id,
                ),
            )
            .join(
                DocumentRevision,
                and_(
                    DocumentRevision.id == Chunk.document_revision_id,
                    DocumentRevision.document_id == Chunk.document_id,
                    DocumentRevision.library_id == library.id,
                    DocumentRevision.status == "ready",
                ),
            )
            .where(
                Chunk.id == chunk_id,
                Chunk.library_id == library.id,
                Chunk.document_revision_id.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if chunk is None:
        raise ChatGraphContextServiceError("citation_chunk_not_found")
    return chunk


async def _chunk_evidence_ids(db: AsyncSession, chunk: Chunk) -> tuple[uuid.UUID, ...]:
    mention_ids = (
        await db.execute(
            select(EntityMention.evidence_id).where(
                EntityMention.library_id == chunk.library_id,
                EntityMention.document_id == chunk.document_id,
                EntityMention.document_revision_id == chunk.document_revision_id,
                EntityMention.chunk_id == chunk.id,
                EntityMention.status == "active",
            ).limit(MAX_CONTEXT_EVIDENCE_IDS + 1)
        )
    ).scalars().all()
    relation_ids = (
        await db.execute(
            select(RelationEvidence.evidence_id).where(
                RelationEvidence.library_id == chunk.library_id,
                RelationEvidence.document_id == chunk.document_id,
                RelationEvidence.document_revision_id == chunk.document_revision_id,
                RelationEvidence.chunk_id == chunk.id,
                RelationEvidence.status == "active",
            ).limit(MAX_CONTEXT_EVIDENCE_IDS + 1)
        )
    ).scalars().all()
    return _bounded_evidence_ids((chunk.evidence_id,), mention_ids, relation_ids)


def _matching_publication_items(library: Library, evidence_ids: Sequence[uuid.UUID]):
    evidence_predicate = or_(
        *(
            GraphPublicationItem.support_evidence_ids.contains([str(evidence_id)])
            for evidence_id in evidence_ids
        )
    )
    return (
        select(
            GraphPublication.id.label("publication_id"),
            GraphPublication.ontology_version_id.label("ontology_version_id"),
            GraphPublication.status.label("publication_status"),
            GraphPublication.activated_at.label("activated_at"),
            GraphPublicationItem.item_kind.label("item_kind"),
            GraphPublicationItem.entity_id.label("entity_id"),
            GraphPublicationItem.relation_id.label("relation_id"),
            KnowledgeRelation.source_entity_id.label("source_entity_id"),
            KnowledgeRelation.target_entity_id.label("target_entity_id"),
        )
        .select_from(GraphPublicationItem)
        .join(
            GraphPublication,
            and_(
                GraphPublication.id == GraphPublicationItem.publication_id,
                GraphPublication.library_id == library.id,
                GraphPublication.status.in_(("active", "degraded")),
            ),
        )
        .join(
            OntologyVersion,
            and_(
                OntologyVersion.id == GraphPublication.ontology_version_id,
                OntologyVersion.library_id == library.id,
                OntologyVersion.status == "active",
            ),
        )
        .outerjoin(
            KnowledgeRelation,
            and_(
                GraphPublicationItem.item_kind == "relation",
                KnowledgeRelation.id == GraphPublicationItem.relation_id,
                KnowledgeRelation.library_id == library.id,
                KnowledgeRelation.ontology_version_id == GraphPublication.ontology_version_id,
            ),
        )
        .where(
            GraphPublicationItem.library_id == library.id,
            GraphPublicationItem.ontology_version_id == GraphPublication.ontology_version_id,
            GraphPublicationItem.status == "active",
            evidence_predicate,
        )
        .order_by(
            GraphPublication.id,
            GraphPublicationItem.item_kind,
            GraphPublicationItem.entity_id,
            GraphPublicationItem.relation_id,
        )
        .limit(MAX_CONTEXT_FACT_ROWS + 1)
    )


async def query_chat_graph_for_chunks(
    db: AsyncSession,
    library: Library,
    chunk_ids: Sequence[uuid.UUID],
    *,
    config: Settings = settings,
) -> ChatGraphQueryResult:
    evidence_groups: list[tuple[uuid.UUID, ...]] = []
    seen: set[uuid.UUID] = set()
    for chunk_id in chunk_ids:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunk = await _load_visible_chunk(db, library, chunk_id)
        evidence_groups.append(await _chunk_evidence_ids(db, chunk))
    evidence_ids = _bounded_evidence_ids(*evidence_groups)
    if not evidence_ids:
        return ChatGraphQueryResult(graph=None)

    rows = (await db.execute(_matching_publication_items(library, evidence_ids))).all()
    if len(rows) > MAX_CONTEXT_FACT_ROWS:
        raise ChatGraphContextServiceError("graph_retrieval_limit_exceeded")
    candidate = _select_candidate(rows)
    if candidate is None:
        return ChatGraphQueryResult(graph=None)
    if candidate.publication_status == "degraded":
        raise ChatGraphContextServiceError("graph_publication_unavailable")

    ordered_seed_ids = sorted(candidate.direct_entity_ids, key=str)
    ordered_seed_ids.extend(
        sorted(candidate.endpoint_entity_ids - candidate.direct_entity_ids, key=str)
    )
    seed_limit = min(10, config.graph_retrieval_max_seeds, MAX_CONTEXT_NODES)
    selected_seed_ids = ordered_seed_ids[:seed_limit]
    if not selected_seed_ids:
        raise ChatGraphContextServiceError("graph_publication_invariant_failed")

    request = GraphRetrievalQueryRequest(
        ontology_version_id=candidate.ontology_version_id,
        expected_publication_id=candidate.publication_id,
        seeds=[GraphRetrievalSeed(entity_id=entity_id) for entity_id in selected_seed_ids],
        direction="both",
        relation_type_keys=[],
        max_hops=MAX_CONTEXT_HOPS,
        max_nodes=MAX_CONTEXT_NODES,
        max_relations=MAX_CONTEXT_RELATIONS,
        include_evidence_locators=True,
    )
    try:
        graph = await graph_retrieval.execute_graph_retrieval_query(
            db,
            library,
            request,
            config=config,
        )
    except graph_retrieval.GraphRetrievalServiceError as exc:
        raise ChatGraphContextServiceError(exc.code) from exc
    return ChatGraphQueryResult(
        graph=graph,
        exact_fact_count=len(candidate.fact_ids),
        exact_seed_count=len(selected_seed_ids),
        exact_seeds_truncated=len(ordered_seed_ids) > len(selected_seed_ids),
    )


async def load_chat_graph_context(
    db: AsyncSession,
    library: Library,
    chunk_id: uuid.UUID,
    *,
    config: Settings = settings,
) -> ChatGraphContextResponse:
    result = await query_chat_graph_for_chunks(db, library, [chunk_id], config=config)
    return ChatGraphContextResponse(
        contract_version="v1",
        chunk_id=chunk_id,
        exact_fact_count=result.exact_fact_count,
        exact_seed_count=result.exact_seed_count,
        exact_seeds_truncated=result.exact_seeds_truncated,
        graph=result.graph,
    )
