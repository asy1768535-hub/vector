from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence


T = TypeVar("T")
CURRENT_STATUSES = ("active", "degraded")


class PublicationReadInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PageResult(Generic[T]):
    rows: tuple[T, ...]
    total: int


@dataclass(frozen=True, slots=True)
class PublicationItemView:
    item: GraphPublicationItem
    source_job_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class PublishedGraphRows(Generic[T]):
    publication: GraphPublication | None
    rows: tuple[T, ...]
    healthy: bool


async def get_graph_publication(
    db: AsyncSession,
    library: Library,
    publication_id: uuid.UUID,
) -> GraphPublication | None:
    result = await db.execute(
        select(GraphPublication).where(
            GraphPublication.id == publication_id,
            GraphPublication.library_id == library.id,
        )
    )
    return result.scalars().first()


async def list_current_graph_publication(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
) -> GraphPublication | None:
    statement = select(GraphPublication).where(
        GraphPublication.library_id == library.id,
        GraphPublication.status.in_(CURRENT_STATUSES),
    )
    if ontology_version_id is not None:
        statement = statement.where(GraphPublication.ontology_version_id == ontology_version_id)
    result = await db.execute(
        statement.order_by(
            GraphPublication.activated_at.desc(),
            GraphPublication.id.desc(),
        ).limit(1)
    )
    return result.scalars().first()


async def list_graph_publications(
    db: AsyncSession,
    library: Library,
    *,
    publication_status: str | None,
    source_mode: str | None,
    ontology_version_id: uuid.UUID | None,
    page: int,
    page_size: int,
) -> PageResult[GraphPublication]:
    filters = [GraphPublication.library_id == library.id]
    if publication_status is not None:
        filters.append(GraphPublication.status == publication_status)
    if source_mode is not None:
        filters.append(GraphPublication.source_mode == source_mode)
    if ontology_version_id is not None:
        filters.append(GraphPublication.ontology_version_id == ontology_version_id)
    total = (
        await db.execute(select(func.count()).select_from(GraphPublication).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(GraphPublication)
            .where(*filters)
            .order_by(GraphPublication.planned_at.desc(), GraphPublication.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PageResult(rows=tuple(rows), total=total)


async def _source_job_ids_by_item(
    db: AsyncSession,
    items: list[GraphPublicationItem],
) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    jobs: dict[uuid.UUID, set[uuid.UUID]] = {item.id: set() for item in items}
    if not items:
        return {}
    library_id = items[0].library_id
    entity_items = {item.entity_id: item.id for item in items if item.entity_id is not None}
    relation_items = {item.relation_id: item.id for item in items if item.relation_id is not None}

    if entity_items:
        entity_rows = (
            await db.execute(
                select(Entity.id, Entity.created_by_job_id).where(
                    Entity.id.in_(entity_items),
                    Entity.library_id == library_id,
                )
            )
        ).all()
        mention_rows = (
            await db.execute(
                select(EntityMention.entity_id, EntityMention.created_by_job_id).where(
                    EntityMention.entity_id.in_(entity_items),
                    EntityMention.library_id == library_id,
                )
            )
        ).all()
        for entity_id, job_id in (*entity_rows, *mention_rows):
            if job_id is not None:
                jobs[entity_items[entity_id]].add(job_id)

    if relation_items:
        relation_rows = (
            await db.execute(
                select(KnowledgeRelation.id, KnowledgeRelation.created_by_job_id).where(
                    KnowledgeRelation.id.in_(relation_items),
                    KnowledgeRelation.library_id == library_id,
                )
            )
        ).all()
        evidence_rows = (
            await db.execute(
                select(RelationEvidence.relation_id, RelationEvidence.created_by_job_id).where(
                    RelationEvidence.relation_id.in_(relation_items),
                    RelationEvidence.library_id == library_id,
                )
            )
        ).all()
        for relation_id, job_id in (*relation_rows, *evidence_rows):
            if job_id is not None:
                jobs[relation_items[relation_id]].add(job_id)

    return {
        item_id: tuple(sorted(item_jobs, key=str))
        for item_id, item_jobs in jobs.items()
    }


async def list_graph_publication_items(
    db: AsyncSession,
    library: Library,
    publication_id: uuid.UUID,
    *,
    item_kind: str | None,
    item_status: str | None,
    page: int,
    page_size: int,
) -> PageResult[PublicationItemView] | None:
    publication = await get_graph_publication(db, library, publication_id)
    if publication is None:
        return None
    filters = [
        GraphPublicationItem.publication_id == publication.id,
        GraphPublicationItem.library_id == library.id,
        GraphPublicationItem.ontology_version_id == publication.ontology_version_id,
    ]
    if item_kind is not None:
        filters.append(GraphPublicationItem.item_kind == item_kind)
    if item_status is not None:
        filters.append(GraphPublicationItem.status == item_status)
    total = (
        await db.execute(select(func.count()).select_from(GraphPublicationItem).where(*filters))
    ).scalar_one()
    items = list(
        (
            await db.execute(
                select(GraphPublicationItem)
                .where(*filters)
                .order_by(
                    GraphPublicationItem.item_kind,
                    GraphPublicationItem.item_hash,
                    GraphPublicationItem.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    source_jobs = await _source_job_ids_by_item(db, items)
    return PageResult(
        rows=tuple(
            PublicationItemView(item=item, source_job_ids=source_jobs[item.id]) for item in items
        ),
        total=total,
    )


async def _list_published_rows(
    db: AsyncSession,
    library: Library,
    *,
    ontology_version_id: uuid.UUID | None,
    include_degraded: bool,
    item_kind: str,
    model,
    target_column,
) -> PublishedGraphRows:
    publication = await list_current_graph_publication(db, library, ontology_version_id)
    if publication is None:
        return PublishedGraphRows(publication=None, rows=(), healthy=False)
    healthy = publication.status == "active"
    if not healthy and not include_degraded:
        return PublishedGraphRows(publication=publication, rows=(), healthy=False)
    statement = (
        select(model)
        .join(GraphPublicationItem, target_column == model.id)
        .where(
            GraphPublicationItem.publication_id == publication.id,
            GraphPublicationItem.library_id == library.id,
            GraphPublicationItem.ontology_version_id == publication.ontology_version_id,
            GraphPublicationItem.item_kind == item_kind,
            model.library_id == library.id,
            model.ontology_version_id == publication.ontology_version_id,
        )
        .order_by(GraphPublicationItem.item_hash, GraphPublicationItem.id)
    )
    if healthy:
        statement = statement.where(
            GraphPublicationItem.status == "active",
            model.status == "active",
        )
    rows = (await db.execute(statement)).scalars().all()
    expected_count = publication.entity_count if item_kind == "entity" else publication.relation_count
    if healthy and len(rows) != expected_count:
        raise PublicationReadInvariantError("active publication membership is incomplete")
    return PublishedGraphRows(publication=publication, rows=tuple(rows), healthy=healthy)


async def list_published_entities(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
    *,
    include_degraded: bool = False,
) -> PublishedGraphRows[Entity]:
    return await _list_published_rows(
        db,
        library,
        ontology_version_id=ontology_version_id,
        include_degraded=include_degraded,
        item_kind="entity",
        model=Entity,
        target_column=GraphPublicationItem.entity_id,
    )


async def list_published_relations(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
    *,
    include_degraded: bool = False,
) -> PublishedGraphRows[KnowledgeRelation]:
    return await _list_published_rows(
        db,
        library,
        ontology_version_id=ontology_version_id,
        include_degraded=include_degraded,
        item_kind="relation",
        model=KnowledgeRelation,
        target_column=GraphPublicationItem.relation_id,
    )


async def list_healthy_published_entities(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
) -> list[Entity]:
    result = await list_published_entities(db, library, ontology_version_id)
    return list(result.rows)


async def list_healthy_published_relations(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
) -> list[KnowledgeRelation]:
    result = await list_published_relations(db, library, ontology_version_id)
    return list(result.rows)
