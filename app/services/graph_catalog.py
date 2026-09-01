from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models.entity import Entity, GRAPH_FACT_STATUS_ACTIVE, GRAPH_FACT_STATUS_DELETED
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.user import User
from app.schemas.graph_catalog import (
    GraphCatalogEntityListItemRead,
    GraphCatalogEntityPageRead,
    GraphCatalogEvidenceCountsRead,
    GraphCatalogLibraryRead,
    GraphCatalogPublicationRead,
    GraphCatalogRelationEndpointRead,
    GraphCatalogRelationListItemRead,
    GraphCatalogRelationPageRead,
    GraphCatalogTypeRead,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogResolvedScope,
    GraphEntityCatalogCursor,
    GraphEntityCatalogQuery,
    GraphRelationCatalogCursor,
    GraphRelationCatalogQuery,
    decode_entity_cursor,
    decode_relation_cursor,
    encode_entity_cursor,
    encode_relation_cursor,
    graph_catalog_filter_fingerprint,
    graph_catalog_invariant_boundary,
)
from app.services.graph_catalog_scope import resolve_graph_catalog_scope
from app.services.graph_governance_actions import (
    entity_governance_state_hash,
    relation_governance_state_hash,
)


CURRENT_PUBLICATION_STATUSES = ("active", "degraded")
CURRENT_ITEM_STATUSES = ("active", "degraded")


def _publication_projection(
    *,
    item_kind: str,
    library_ids: tuple[uuid.UUID, ...],
):
    fact_column = (
        GraphPublicationItem.entity_id
        if item_kind == "entity"
        else GraphPublicationItem.relation_id
    )
    return (
        select(
            fact_column.label("fact_id"),
            GraphPublicationItem.library_id.label("library_id"),
            GraphPublicationItem.ontology_version_id.label("ontology_version_id"),
            GraphPublication.id.label("publication_id"),
            GraphPublication.status.label("publication_status"),
            GraphPublicationItem.item_hash.label("publication_item_hash"),
        )
        .join(
            GraphPublication,
            and_(
                GraphPublication.id == GraphPublicationItem.publication_id,
                GraphPublication.library_id == GraphPublicationItem.library_id,
                GraphPublication.ontology_version_id
                == GraphPublicationItem.ontology_version_id,
            ),
        )
        .where(
            GraphPublication.library_id.in_(library_ids),
            GraphPublication.status.in_(CURRENT_PUBLICATION_STATUSES),
            GraphPublicationItem.item_kind == item_kind,
            GraphPublicationItem.status.in_(CURRENT_ITEM_STATUSES),
            fact_column.is_not(None),
        )
        .subquery(f"graph_catalog_{item_kind}_publication")
    )


def _entity_evidence_counts(library_ids: tuple[uuid.UUID, ...]):
    return (
        select(
            EntityMention.entity_id.label("fact_id"),
            EntityMention.library_id.label("library_id"),
            func.count(func.distinct(EntityMention.evidence_id)).label("evidence_count"),
            func.count(func.distinct(EntityMention.document_id)).label("document_count"),
        )
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.id == EntityMention.evidence_id,
                EvidenceUnit.library_id == EntityMention.library_id,
                EvidenceUnit.document_id == EntityMention.document_id,
                EvidenceUnit.document_revision_id == EntityMention.document_revision_id,
            ),
        )
        .where(
            EntityMention.library_id.in_(library_ids),
            EntityMention.status == "active",
            EvidenceUnit.status == "active",
        )
        .group_by(EntityMention.library_id, EntityMention.entity_id)
        .subquery("graph_catalog_entity_evidence_counts")
    )


def _relation_evidence_counts(library_ids: tuple[uuid.UUID, ...]):
    return (
        select(
            RelationEvidence.relation_id.label("fact_id"),
            RelationEvidence.library_id.label("library_id"),
            func.count(func.distinct(RelationEvidence.evidence_id)).label("evidence_count"),
            func.count(func.distinct(RelationEvidence.document_id)).label("document_count"),
        )
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.id == RelationEvidence.evidence_id,
                EvidenceUnit.library_id == RelationEvidence.library_id,
                EvidenceUnit.document_id == RelationEvidence.document_id,
                EvidenceUnit.document_revision_id == RelationEvidence.document_revision_id,
            ),
        )
        .where(
            RelationEvidence.library_id.in_(library_ids),
            RelationEvidence.status == "active",
            EvidenceUnit.status == "active",
        )
        .group_by(RelationEvidence.library_id, RelationEvidence.relation_id)
        .subquery("graph_catalog_relation_evidence_counts")
    )


def _entity_statement(
    scope: GraphCatalogResolvedScope,
    query: GraphEntityCatalogQuery,
    cursor: GraphEntityCatalogCursor | None,
):
    library_ids = tuple(library.id for library in scope.libraries)
    publication = _publication_projection(item_kind="entity", library_ids=library_ids)
    counts = _entity_evidence_counts(library_ids)
    filters = [
        Entity.library_id.in_(library_ids),
        Entity.status != GRAPH_FACT_STATUS_DELETED,
    ]
    if query.query_text is not None:
        alias_match = (
            select(EntityAlias.id)
            .where(
                EntityAlias.entity_id == Entity.id,
                EntityAlias.library_id == Entity.library_id,
                EntityAlias.status != "deleted",
                EntityAlias.alias.icontains(query.query_text, autoescape=True),
            )
            .exists()
        )
        filters.append(
            or_(
                Entity.canonical_name.icontains(query.query_text, autoescape=True),
                Entity.normalized_name.icontains(query.query_text, autoescape=True),
                alias_match,
            )
        )
    if query.ontology_version_ids:
        filters.append(Entity.ontology_version_id.in_(query.ontology_version_ids))
    if query.type_keys:
        filters.append(EntityType.key.in_(query.type_keys))
    if query.statuses:
        filters.append(Entity.status.in_(query.statuses))
    if query.source_types:
        filters.append(Entity.source_type.in_(query.source_types))
    if query.publication_state == "published":
        filters.append(publication.c.fact_id.is_not(None))
    elif query.publication_state == "staged":
        filters.append(publication.c.fact_id.is_(None))
    if cursor is not None:
        filters.append(
            or_(
                Entity.normalized_name > cursor.normalized_name,
                and_(
                    Entity.normalized_name == cursor.normalized_name,
                    Library.slug > cursor.library_slug,
                ),
                and_(
                    Entity.normalized_name == cursor.normalized_name,
                    Library.slug == cursor.library_slug,
                    Entity.id > cursor.entity_id,
                ),
            )
        )
    return (
        select(
            Entity,
            Library.slug.label("library_slug"),
            Library.name.label("library_name"),
            EntityType.key.label("type_key"),
            EntityType.label.label("type_label"),
            publication.c.publication_id,
            publication.c.publication_status,
            publication.c.publication_item_hash,
            func.coalesce(counts.c.evidence_count, 0).label("evidence_count"),
            func.coalesce(counts.c.document_count, 0).label("document_count"),
        )
        .join(Library, Library.id == Entity.library_id)
        .join(
            EntityType,
            and_(
                EntityType.id == Entity.entity_type_id,
                EntityType.library_id == Entity.library_id,
                EntityType.ontology_version_id == Entity.ontology_version_id,
            ),
        )
        .outerjoin(
            publication,
            and_(
                publication.c.fact_id == Entity.id,
                publication.c.library_id == Entity.library_id,
                publication.c.ontology_version_id == Entity.ontology_version_id,
                Entity.status == GRAPH_FACT_STATUS_ACTIVE,
            ),
        )
        .outerjoin(
            counts,
            and_(
                counts.c.fact_id == Entity.id,
                counts.c.library_id == Entity.library_id,
            ),
        )
        .where(*filters)
        .order_by(Entity.normalized_name, Library.slug, Entity.id)
        .limit(query.limit + 1)
    )


def _relation_statement(
    scope: GraphCatalogResolvedScope,
    query: GraphRelationCatalogQuery,
    cursor: GraphRelationCatalogCursor | None,
    *,
    relation_id: uuid.UUID | None = None,
    related_entity_id: uuid.UUID | None = None,
    limit: int | None = None,
):
    library_ids = tuple(library.id for library in scope.libraries)
    publication = _publication_projection(item_kind="relation", library_ids=library_ids)
    counts = _relation_evidence_counts(library_ids)
    source = aliased(Entity, name="graph_catalog_source_entity")
    target = aliased(Entity, name="graph_catalog_target_entity")
    source_type = aliased(EntityType, name="graph_catalog_source_type")
    target_type = aliased(EntityType, name="graph_catalog_target_type")
    filters = [
        KnowledgeRelation.library_id.in_(library_ids),
        KnowledgeRelation.status != GRAPH_FACT_STATUS_DELETED,
    ]
    if relation_id is not None:
        filters.append(KnowledgeRelation.id == relation_id)
    if related_entity_id is not None:
        filters.append(
            or_(
                KnowledgeRelation.source_entity_id == related_entity_id,
                KnowledgeRelation.target_entity_id == related_entity_id,
            )
        )
    if query.query_text is not None:
        filters.append(
            or_(
                RelationType.key.icontains(query.query_text, autoescape=True),
                RelationType.label.icontains(query.query_text, autoescape=True),
                source.canonical_name.icontains(query.query_text, autoescape=True),
                target.canonical_name.icontains(query.query_text, autoescape=True),
            )
        )
    if query.ontology_version_ids:
        filters.append(KnowledgeRelation.ontology_version_id.in_(query.ontology_version_ids))
    if query.type_keys:
        filters.append(RelationType.key.in_(query.type_keys))
    if query.statuses:
        filters.append(KnowledgeRelation.status.in_(query.statuses))
    if query.review_statuses:
        filters.append(KnowledgeRelation.review_status.in_(query.review_statuses))
    if query.source_types:
        filters.append(KnowledgeRelation.source_type.in_(query.source_types))
    if query.publication_state == "published":
        filters.append(publication.c.fact_id.is_not(None))
    elif query.publication_state == "staged":
        filters.append(publication.c.fact_id.is_(None))
    if cursor is not None:
        filters.append(
            or_(
                RelationType.key > cursor.relation_type_key,
                and_(
                    RelationType.key == cursor.relation_type_key,
                    source.normalized_name > cursor.source_normalized_name,
                ),
                and_(
                    RelationType.key == cursor.relation_type_key,
                    source.normalized_name == cursor.source_normalized_name,
                    target.normalized_name > cursor.target_normalized_name,
                ),
                and_(
                    RelationType.key == cursor.relation_type_key,
                    source.normalized_name == cursor.source_normalized_name,
                    target.normalized_name == cursor.target_normalized_name,
                    Library.slug > cursor.library_slug,
                ),
                and_(
                    RelationType.key == cursor.relation_type_key,
                    source.normalized_name == cursor.source_normalized_name,
                    target.normalized_name == cursor.target_normalized_name,
                    Library.slug == cursor.library_slug,
                    KnowledgeRelation.id > cursor.relation_id,
                ),
            )
        )
    statement = (
        select(
            KnowledgeRelation,
            Library.slug.label("library_slug"),
            Library.name.label("library_name"),
            RelationType.key.label("type_key"),
            RelationType.label.label("type_label"),
            RelationType.direction.label("direction"),
            source.id.label("source_id"),
            source.canonical_name.label("source_name"),
            source.normalized_name.label("source_normalized_name"),
            source_type.id.label("source_type_id"),
            source_type.key.label("source_type_key"),
            source_type.label.label("source_type_label"),
            target.id.label("target_id"),
            target.canonical_name.label("target_name"),
            target.normalized_name.label("target_normalized_name"),
            target_type.id.label("target_type_id"),
            target_type.key.label("target_type_key"),
            target_type.label.label("target_type_label"),
            publication.c.publication_id,
            publication.c.publication_status,
            publication.c.publication_item_hash,
            func.coalesce(counts.c.evidence_count, 0).label("evidence_count"),
            func.coalesce(counts.c.document_count, 0).label("document_count"),
        )
        .join(Library, Library.id == KnowledgeRelation.library_id)
        .join(
            RelationType,
            and_(
                RelationType.id == KnowledgeRelation.relation_type_id,
                RelationType.library_id == KnowledgeRelation.library_id,
                RelationType.ontology_version_id == KnowledgeRelation.ontology_version_id,
            ),
        )
        .join(
            source,
            and_(
                source.id == KnowledgeRelation.source_entity_id,
                source.library_id == KnowledgeRelation.library_id,
                source.ontology_version_id == KnowledgeRelation.ontology_version_id,
            ),
        )
        .join(
            source_type,
            and_(
                source_type.id == source.entity_type_id,
                source_type.library_id == source.library_id,
                source_type.ontology_version_id == source.ontology_version_id,
            ),
        )
        .join(
            target,
            and_(
                target.id == KnowledgeRelation.target_entity_id,
                target.library_id == KnowledgeRelation.library_id,
                target.ontology_version_id == KnowledgeRelation.ontology_version_id,
            ),
        )
        .join(
            target_type,
            and_(
                target_type.id == target.entity_type_id,
                target_type.library_id == target.library_id,
                target_type.ontology_version_id == target.ontology_version_id,
            ),
        )
        .outerjoin(
            publication,
            and_(
                publication.c.fact_id == KnowledgeRelation.id,
                publication.c.library_id == KnowledgeRelation.library_id,
                publication.c.ontology_version_id == KnowledgeRelation.ontology_version_id,
                KnowledgeRelation.status == GRAPH_FACT_STATUS_ACTIVE,
            ),
        )
        .outerjoin(
            counts,
            and_(
                counts.c.fact_id == KnowledgeRelation.id,
                counts.c.library_id == KnowledgeRelation.library_id,
            ),
        )
        .where(*filters)
        .order_by(
            RelationType.key,
            source.normalized_name,
            target.normalized_name,
            Library.slug,
            KnowledgeRelation.id,
        )
    )
    return statement.limit(limit if limit is not None else query.limit + 1)


def _publication(row) -> GraphCatalogPublicationRead | None:
    if row.publication_id is None:
        return None
    return GraphCatalogPublicationRead(
        id=row.publication_id,
        status=row.publication_status,
        item_hash=row.publication_item_hash,
    )


def _entity_item(row) -> GraphCatalogEntityListItemRead:
    entity = row.Entity
    publication = _publication(row)
    return GraphCatalogEntityListItemRead(
        id=entity.id,
        library=GraphCatalogLibraryRead(
            id=entity.library_id,
            slug=row.library_slug,
            name=row.library_name,
        ),
        ontology_version_id=entity.ontology_version_id,
        entity_type=GraphCatalogTypeRead(
            id=entity.entity_type_id,
            key=row.type_key,
            label=row.type_label,
        ),
        canonical_name=entity.canonical_name,
        normalized_name=entity.normalized_name,
        status=entity.status,
        source_type=entity.source_type,
        authority_level=entity.authority_level,
        confidence=entity.confidence,
        governance_state_hash=entity_governance_state_hash(entity),
        publication_state="published" if publication is not None else "staged",
        publication=publication,
        counts=GraphCatalogEvidenceCountsRead(
            evidence=int(row.evidence_count),
            documents=int(row.document_count),
        ),
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def _relation_item(row) -> GraphCatalogRelationListItemRead:
    relation = row.KnowledgeRelation
    publication = _publication(row)
    return GraphCatalogRelationListItemRead(
        id=relation.id,
        library=GraphCatalogLibraryRead(
            id=relation.library_id,
            slug=row.library_slug,
            name=row.library_name,
        ),
        ontology_version_id=relation.ontology_version_id,
        relation_type=GraphCatalogTypeRead(
            id=relation.relation_type_id,
            key=row.type_key,
            label=row.type_label,
        ),
        direction=row.direction,
        source=GraphCatalogRelationEndpointRead(
            id=row.source_id,
            canonical_name=row.source_name,
            normalized_name=row.source_normalized_name,
            entity_type=GraphCatalogTypeRead(
                id=row.source_type_id,
                key=row.source_type_key,
                label=row.source_type_label,
            ),
        ),
        target=GraphCatalogRelationEndpointRead(
            id=row.target_id,
            canonical_name=row.target_name,
            normalized_name=row.target_normalized_name,
            entity_type=GraphCatalogTypeRead(
                id=row.target_type_id,
                key=row.target_type_key,
                label=row.target_type_label,
            ),
        ),
        status=relation.status,
        review_status=relation.review_status,
        source_type=relation.source_type,
        authority_level=relation.authority_level,
        confidence=relation.confidence,
        governance_state_hash=relation_governance_state_hash(relation),
        publication_state="published" if publication is not None else "staged",
        publication=publication,
        counts=GraphCatalogEvidenceCountsRead(
            evidence=int(row.evidence_count),
            documents=int(row.document_count),
        ),
        created_at=relation.created_at,
        updated_at=relation.updated_at,
    )


@graph_catalog_invariant_boundary
async def search_graph_catalog_entities(
    db,
    *,
    user: User,
    query: GraphEntityCatalogQuery,
    cursor_value: str | None = None,
) -> GraphCatalogEntityPageRead:
    scope = await resolve_graph_catalog_scope(db, user=user, selection=query.selection)
    fingerprint = graph_catalog_filter_fingerprint(
        kind="entity",
        organization_id=scope.organization_id,
        library_ids=tuple(library.id for library in scope.libraries),
        filters=query.filter_payload(),
    )
    cursor = decode_entity_cursor(
        cursor_value,
        expected_filter_fingerprint=fingerprint,
    )
    rows = (await db.execute(_entity_statement(scope, query, cursor))).all()
    has_more = len(rows) > query.limit
    visible = rows[: query.limit]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = encode_entity_cursor(
            GraphEntityCatalogCursor(
                normalized_name=last.Entity.normalized_name,
                library_slug=last.library_slug,
                entity_id=last.Entity.id,
                filter_fingerprint=fingerprint,
            )
        )
    return GraphCatalogEntityPageRead(
        items=[_entity_item(row) for row in visible],
        next_cursor=next_cursor,
    )


@graph_catalog_invariant_boundary
async def search_graph_catalog_relations(
    db,
    *,
    user: User,
    query: GraphRelationCatalogQuery,
    cursor_value: str | None = None,
) -> GraphCatalogRelationPageRead:
    scope = await resolve_graph_catalog_scope(db, user=user, selection=query.selection)
    fingerprint = graph_catalog_filter_fingerprint(
        kind="relation",
        organization_id=scope.organization_id,
        library_ids=tuple(library.id for library in scope.libraries),
        filters=query.filter_payload(),
    )
    cursor = decode_relation_cursor(
        cursor_value,
        expected_filter_fingerprint=fingerprint,
    )
    rows = (await db.execute(_relation_statement(scope, query, cursor))).all()
    has_more = len(rows) > query.limit
    visible = rows[: query.limit]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = encode_relation_cursor(
            GraphRelationCatalogCursor(
                relation_type_key=last.type_key,
                source_normalized_name=last.source_normalized_name,
                target_normalized_name=last.target_normalized_name,
                library_slug=last.library_slug,
                relation_id=last.KnowledgeRelation.id,
                filter_fingerprint=fingerprint,
            )
        )
    return GraphCatalogRelationPageRead(
        items=[_relation_item(row) for row in visible],
        next_cursor=next_cursor,
    )
