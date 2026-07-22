from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models.document import Document
from app.models.entity import Entity, GRAPH_FACT_STATUS_ACTIVE, GRAPH_FACT_STATUS_DELETED
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.user import User
from app.schemas.graph_catalog import (
    GraphCatalogAliasRead,
    GraphCatalogEntityDetailRead,
    GraphCatalogEntityListItemRead,
    GraphCatalogEvidenceCountsRead,
    GraphCatalogEvidenceLocatorRead,
    GraphCatalogExtractionRead,
    GraphCatalogLibraryRead,
    GraphCatalogPublicationRead,
    GraphCatalogRelatedDocumentRead,
    GraphCatalogRelationDetailRead,
    GraphCatalogRelationEndpointRead,
    GraphCatalogRelationListItemRead,
    GraphCatalogTypeRead,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogSelection,
    graph_catalog_invariant_boundary,
)
from app.services.graph_catalog_scope import resolve_graph_catalog_scope
from app.services.graph_governance_actions import (
    entity_governance_state_hash,
    relation_governance_state_hash,
)


DETAIL_LIMIT = 100
CURRENT_PUBLICATION_STATUSES = ("active", "degraded")
CURRENT_ITEM_STATUSES = ("active", "degraded")


def _publication_projection(item_kind: str, library_id: uuid.UUID):
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
            GraphPublication.library_id == library_id,
            GraphPublication.status.in_(CURRENT_PUBLICATION_STATUSES),
            GraphPublicationItem.item_kind == item_kind,
            GraphPublicationItem.status.in_(CURRENT_ITEM_STATUSES),
            fact_column.is_not(None),
        )
        .subquery(f"graph_catalog_detail_{item_kind}_publication")
    )


def _entity_core_statement(library: Library, entity_id: uuid.UUID):
    publication = _publication_projection("entity", library.id)
    evidence_count = (
        select(func.count(func.distinct(EntityMention.evidence_id)))
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
            EntityMention.entity_id == Entity.id,
            EntityMention.library_id == Entity.library_id,
            EntityMention.status == "active",
            EvidenceUnit.status == "active",
        )
        .correlate(Entity)
        .scalar_subquery()
    )
    document_count = (
        select(func.count(func.distinct(EntityMention.document_id)))
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
            EntityMention.entity_id == Entity.id,
            EntityMention.library_id == Entity.library_id,
            EntityMention.status == "active",
            EvidenceUnit.status == "active",
        )
        .correlate(Entity)
        .scalar_subquery()
    )
    return (
        select(
            Entity,
            EntityType.key.label("type_key"),
            EntityType.label.label("type_label"),
            publication.c.publication_id,
            publication.c.publication_status,
            publication.c.publication_item_hash,
            evidence_count.label("evidence_count"),
            document_count.label("document_count"),
        )
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
        .where(
            Entity.id == entity_id,
            Entity.library_id == library.id,
            Entity.status != GRAPH_FACT_STATUS_DELETED,
        )
    )


def _relation_core_statement(
    library: Library,
    *,
    relation_id: uuid.UUID | None = None,
    related_entity_id: uuid.UUID | None = None,
):
    publication = _publication_projection("relation", library.id)
    source = aliased(Entity, name="graph_catalog_detail_source")
    target = aliased(Entity, name="graph_catalog_detail_target")
    source_type = aliased(EntityType, name="graph_catalog_detail_source_type")
    target_type = aliased(EntityType, name="graph_catalog_detail_target_type")
    evidence_count = (
        select(func.count(func.distinct(RelationEvidence.evidence_id)))
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
            RelationEvidence.relation_id == KnowledgeRelation.id,
            RelationEvidence.library_id == KnowledgeRelation.library_id,
            RelationEvidence.status == "active",
            EvidenceUnit.status == "active",
        )
        .correlate(KnowledgeRelation)
        .scalar_subquery()
    )
    document_count = (
        select(func.count(func.distinct(RelationEvidence.document_id)))
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
            RelationEvidence.relation_id == KnowledgeRelation.id,
            RelationEvidence.library_id == KnowledgeRelation.library_id,
            RelationEvidence.status == "active",
            EvidenceUnit.status == "active",
        )
        .correlate(KnowledgeRelation)
        .scalar_subquery()
    )
    filters = [
        KnowledgeRelation.library_id == library.id,
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
    return (
        select(
            KnowledgeRelation,
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
            evidence_count.label("evidence_count"),
            document_count.label("document_count"),
        )
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
        .where(*filters)
        .order_by(
            RelationType.key,
            source.normalized_name,
            target.normalized_name,
            KnowledgeRelation.id,
        )
    )


def _publication(row) -> GraphCatalogPublicationRead | None:
    if row.publication_id is None:
        return None
    return GraphCatalogPublicationRead(
        id=row.publication_id,
        status=row.publication_status,
        item_hash=row.publication_item_hash,
    )


def _library_read(library: Library) -> GraphCatalogLibraryRead:
    return GraphCatalogLibraryRead(id=library.id, slug=library.slug, name=library.name)


def _entity_item(row, library: Library) -> GraphCatalogEntityListItemRead:
    entity = row.Entity
    publication = _publication(row)
    return GraphCatalogEntityListItemRead(
        id=entity.id,
        library=_library_read(library),
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


def _relation_item(row, library: Library) -> GraphCatalogRelationListItemRead:
    relation = row.KnowledgeRelation
    publication = _publication(row)
    return GraphCatalogRelationListItemRead(
        id=relation.id,
        library=_library_read(library),
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


def _title_path(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    values = [item.strip()[:512] for item in value[:16] if isinstance(item, str) and item.strip()]
    return values or None


def _evidence_locator(row, *, support_type: str | None) -> GraphCatalogEvidenceLocatorRead:
    evidence = row.EvidenceUnit
    return GraphCatalogEvidenceLocatorRead(
        evidence_id=evidence.id,
        document_id=evidence.document_id,
        document_revision_id=evidence.document_revision_id,
        chunk_id=row.chunk_id,
        evidence_kind=evidence.evidence_kind,
        support_type=support_type,
        page_start=evidence.page_start,
        page_end=evidence.page_end,
        source_start=evidence.source_start,
        source_end=evidence.source_end,
        title_path=_title_path(evidence.title_path),
    )


def _extraction(job: GraphExtractionJob | None) -> GraphCatalogExtractionRead | None:
    if job is None:
        return None
    return GraphCatalogExtractionRead(
        job_id=job.id,
        model_provider=job.model_provider,
        model_name=job.model_name,
        prompt_version=job.prompt_version,
    )


async def _entity_aliases(db, library: Library, entity_id: uuid.UUID):
    filters = [
        EntityAlias.library_id == library.id,
        EntityAlias.entity_id == entity_id,
        EntityAlias.status != "deleted",
    ]
    total = (await db.execute(select(func.count(EntityAlias.id)).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(EntityAlias)
            .where(*filters)
            .order_by(EntityAlias.normalized_alias, EntityAlias.id)
            .limit(DETAIL_LIMIT)
        )
    ).scalars().all()
    return [
        GraphCatalogAliasRead(
            id=row.id,
            alias=row.alias,
            normalized_alias=row.normalized_alias,
            source_type=row.source_type,
            confidence=row.confidence,
            status=row.status,
        )
        for row in rows
    ], int(total)


async def _entity_evidence(db, library: Library, entity_id: uuid.UUID):
    filters = [
        EntityMention.library_id == library.id,
        EntityMention.entity_id == entity_id,
        EntityMention.status == "active",
    ]
    total = (
        await db.execute(
            select(func.count(func.distinct(EntityMention.evidence_id)))
            .join(
                EvidenceUnit,
                and_(
                    EvidenceUnit.id == EntityMention.evidence_id,
                    EvidenceUnit.library_id == EntityMention.library_id,
                    EvidenceUnit.document_id == EntityMention.document_id,
                    EvidenceUnit.document_revision_id == EntityMention.document_revision_id,
                ),
            )
            .where(*filters, EvidenceUnit.status == "active")
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(EntityMention.chunk_id, EvidenceUnit)
            .join(
                EvidenceUnit,
                and_(
                    EvidenceUnit.id == EntityMention.evidence_id,
                    EvidenceUnit.library_id == EntityMention.library_id,
                    EvidenceUnit.document_id == EntityMention.document_id,
                    EvidenceUnit.document_revision_id == EntityMention.document_revision_id,
                ),
            )
            .where(*filters, EvidenceUnit.status == "active")
            .order_by(
                EntityMention.document_id,
                EntityMention.document_revision_id,
                EntityMention.evidence_id,
            )
            .limit(DETAIL_LIMIT)
        )
    ).all()
    return [_evidence_locator(row, support_type=None) for row in rows], int(total)


async def _relation_evidence(db, library: Library, relation_id: uuid.UUID):
    filters = [
        RelationEvidence.library_id == library.id,
        RelationEvidence.relation_id == relation_id,
        RelationEvidence.status == "active",
    ]
    total = (
        await db.execute(
            select(func.count(func.distinct(RelationEvidence.evidence_id)))
            .join(
                EvidenceUnit,
                and_(
                    EvidenceUnit.id == RelationEvidence.evidence_id,
                    EvidenceUnit.library_id == RelationEvidence.library_id,
                    EvidenceUnit.document_id == RelationEvidence.document_id,
                    EvidenceUnit.document_revision_id
                    == RelationEvidence.document_revision_id,
                ),
            )
            .where(*filters, EvidenceUnit.status == "active")
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(RelationEvidence.chunk_id, RelationEvidence.support_type, EvidenceUnit)
            .join(
                EvidenceUnit,
                and_(
                    EvidenceUnit.id == RelationEvidence.evidence_id,
                    EvidenceUnit.library_id == RelationEvidence.library_id,
                    EvidenceUnit.document_id == RelationEvidence.document_id,
                    EvidenceUnit.document_revision_id == RelationEvidence.document_revision_id,
                ),
            )
            .where(*filters, EvidenceUnit.status == "active")
            .order_by(
                RelationEvidence.document_id,
                RelationEvidence.document_revision_id,
                RelationEvidence.evidence_id,
            )
            .limit(DETAIL_LIMIT)
        )
    ).all()
    return [
        _evidence_locator(row, support_type=row.support_type)
        for row in rows
    ], int(total)


async def _documents(db, library: Library, fact_id: uuid.UUID, *, kind: str):
    link = EntityMention if kind == "entity" else RelationEvidence
    fact_column = link.entity_id if kind == "entity" else link.relation_id
    filters = [
        link.library_id == library.id,
        fact_column == fact_id,
        link.status == "active",
    ]
    total = (
        await db.execute(
            select(func.count())
            .select_from(
                select(link.document_id, link.document_revision_id)
                .join(
                    EvidenceUnit,
                    and_(
                        EvidenceUnit.id == link.evidence_id,
                        EvidenceUnit.library_id == link.library_id,
                        EvidenceUnit.document_id == link.document_id,
                        EvidenceUnit.document_revision_id == link.document_revision_id,
                    ),
                )
                .where(*filters, EvidenceUnit.status == "active")
                .distinct()
                .subquery()
            )
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                link.document_id,
                link.document_revision_id,
                Document.title,
                func.count(func.distinct(link.evidence_id)).label("evidence_count"),
            )
            .join(
                EvidenceUnit,
                and_(
                    EvidenceUnit.id == link.evidence_id,
                    EvidenceUnit.library_id == link.library_id,
                    EvidenceUnit.document_id == link.document_id,
                    EvidenceUnit.document_revision_id == link.document_revision_id,
                ),
            )
            .join(
                Document,
                and_(
                    Document.id == link.document_id,
                    Document.library_id == link.library_id,
                    Document.deleted_at.is_(None),
                ),
            )
            .where(*filters, EvidenceUnit.status == "active")
            .group_by(link.document_id, link.document_revision_id, Document.title)
            .order_by(Document.title, link.document_id, link.document_revision_id)
            .limit(DETAIL_LIMIT)
        )
    ).all()
    return [
        GraphCatalogRelatedDocumentRead(
            document_id=row.document_id,
            document_revision_id=row.document_revision_id,
            title=row.title,
            evidence_count=int(row.evidence_count),
        )
        for row in rows
    ], int(total)


async def _job(
    db,
    *,
    library: Library,
    ontology_version_id: uuid.UUID,
    job_id: uuid.UUID | None,
) -> GraphExtractionJob | None:
    if job_id is None:
        return None
    return (
        await db.execute(
            select(GraphExtractionJob).where(
                GraphExtractionJob.id == job_id,
                GraphExtractionJob.library_id == library.id,
                GraphExtractionJob.ontology_version_id == ontology_version_id,
            )
        )
    ).scalars().first()


@graph_catalog_invariant_boundary
async def get_graph_catalog_entity_detail(
    db,
    *,
    user: User,
    organization_id: uuid.UUID,
    library_slug: str,
    entity_id: uuid.UUID,
) -> GraphCatalogEntityDetailRead:
    scope = await resolve_graph_catalog_scope(
        db,
        user=user,
        selection=GraphCatalogSelection(
            organization_id=organization_id,
            library_slugs=(library_slug,),
        ),
    )
    library = scope.libraries[0]
    core = (await db.execute(_entity_core_statement(library, entity_id))).first()
    if core is None:
        raise GraphCatalogError("graph_catalog_not_found")
    aliases, alias_count = await _entity_aliases(db, library, entity_id)
    evidence, evidence_count = await _entity_evidence(db, library, entity_id)
    documents, document_count = await _documents(db, library, entity_id, kind="entity")
    relation_count = (
        await db.execute(
            select(func.count(KnowledgeRelation.id)).where(
                KnowledgeRelation.library_id == library.id,
                KnowledgeRelation.ontology_version_id == core.Entity.ontology_version_id,
                KnowledgeRelation.status != GRAPH_FACT_STATUS_DELETED,
                or_(
                    KnowledgeRelation.source_entity_id == entity_id,
                    KnowledgeRelation.target_entity_id == entity_id,
                ),
            )
        )
    ).scalar_one()
    relation_rows = (
        await db.execute(
            _relation_core_statement(library, related_entity_id=entity_id).limit(DETAIL_LIMIT)
        )
    ).all()
    return GraphCatalogEntityDetailRead(
        entity=_entity_item(core, library),
        properties=core.Entity.properties,
        aliases=aliases,
        alias_count=alias_count,
        aliases_truncated=alias_count > len(aliases),
        evidence=evidence,
        evidence_count=evidence_count,
        evidence_truncated=evidence_count > len(evidence),
        documents=documents,
        document_count=document_count,
        documents_truncated=document_count > len(documents),
        related_relations=[_relation_item(row, library) for row in relation_rows],
        relation_count=int(relation_count),
        relations_truncated=int(relation_count) > len(relation_rows),
        extraction=_extraction(
            await _job(
                db,
                library=library,
                ontology_version_id=core.Entity.ontology_version_id,
                job_id=core.Entity.created_by_job_id,
            )
        ),
    )


@graph_catalog_invariant_boundary
async def get_graph_catalog_relation_detail(
    db,
    *,
    user: User,
    organization_id: uuid.UUID,
    library_slug: str,
    relation_id: uuid.UUID,
) -> GraphCatalogRelationDetailRead:
    scope = await resolve_graph_catalog_scope(
        db,
        user=user,
        selection=GraphCatalogSelection(
            organization_id=organization_id,
            library_slugs=(library_slug,),
        ),
    )
    library = scope.libraries[0]
    core = (
        await db.execute(_relation_core_statement(library, relation_id=relation_id))
    ).first()
    if core is None:
        raise GraphCatalogError("graph_catalog_not_found")
    evidence, evidence_count = await _relation_evidence(db, library, relation_id)
    documents, document_count = await _documents(db, library, relation_id, kind="relation")
    return GraphCatalogRelationDetailRead(
        relation=_relation_item(core, library),
        properties=core.KnowledgeRelation.properties,
        evidence=evidence,
        evidence_count=evidence_count,
        evidence_truncated=evidence_count > len(evidence),
        documents=documents,
        document_count=document_count,
        documents_truncated=document_count > len(documents),
        extraction=_extraction(
            await _job(
                db,
                library=library,
                ontology_version_id=core.KnowledgeRelation.ontology_version_id,
                job_id=core.KnowledgeRelation.created_by_job_id,
            )
        ),
    )
