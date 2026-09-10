from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.config import Settings, settings
from app.models.classification_decision import (
    DocumentClassificationDecision,
    DocumentClassificationDecisionSet,
    DocumentClassificationRun,
)
from app.models.classification_job import DocumentClassificationJob
from app.models.classification_taxonomy import ClassificationLabel
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_artifact import KnowledgeArtifact
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.user import User
from app.schemas.knowledge_catalog import (
    CatalogCapabilitiesRead,
    CatalogClassificationLabelRead,
    CatalogClassificationRead,
    CatalogDocumentDetailRead,
    CatalogDocumentListItemRead,
    CatalogDocumentPageRead,
    CatalogEntityKnowledgeUnitRead,
    CatalogEvidenceDetailRead,
    CatalogEvidenceFactRefRead,
    CatalogEvidenceLocatorRead,
    CatalogGraphCountsRead,
    CatalogGraphRead,
    CatalogOutlineRead,
    CatalogRelationKnowledgeUnitRead,
    CatalogRevisionFileRead,
    CatalogSummaryRead,
    CatalogUploaderOptionRead,
    CatalogUploaderOptionsRead,
    CatalogUploaderRead,
)
from app.schemas.knowledge_artifact import OutlinePayloadV1, SummaryPayloadV1
from app.services.evidence_read import get_evidence_detail
from app.services.knowledge_artifacts import ArtifactContractError, validate_artifact_payload
from app.services.knowledge_catalog_contracts import (
    CatalogCapabilityState,
    CatalogDocumentCursor,
    CatalogDocumentQuery,
    KnowledgeCatalogError,
    catalog_filter_fingerprint,
    decode_catalog_document_cursor,
    encode_catalog_document_cursor,
    project_catalog_capabilities,
)
from app.services.revision_files import RevisionFileAccess, revision_file_access_from_row
from app.services.object_storage_contracts import ObjectStorageError


@dataclass(frozen=True, slots=True)
class PreparedCatalogFileAccess:
    revision_file_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    access: RevisionFileAccess


@dataclass(frozen=True, slots=True)
class _CurrentDocument:
    document: Document
    revision: DocumentRevision


@dataclass(frozen=True, slots=True)
class _CatalogHydration:
    artifacts: dict[uuid.UUID, dict[str, KnowledgeArtifact]]
    artifact_jobs: dict[tuple[uuid.UUID, str], KnowledgeArtifactJob]
    classifications: dict[uuid.UUID, CatalogClassificationRead]
    classification_jobs: dict[uuid.UUID, DocumentClassificationJob]
    graph_jobs: dict[uuid.UUID, GraphExtractionJob]
    files: dict[uuid.UUID, DocumentRevisionFile]
    graph_counts: dict[uuid.UUID, CatalogGraphCountsRead]
    uploaders: dict[uuid.UUID, User]


def _not_found() -> KnowledgeCatalogError:
    return KnowledgeCatalogError("catalog_not_found")


def _base_current_document_statement(library_id: uuid.UUID):
    return (
        select(Document, DocumentRevision)
        .join(
            DocumentRevision,
            and_(
                Document.current_revision_id == DocumentRevision.id,
                DocumentRevision.library_id == library_id,
                DocumentRevision.document_id == Document.id,
            ),
        )
        .where(
            Document.library_id == library_id,
            Document.deleted_at.is_(None),
        )
    )


def _classification_state_predicate(state: str):
    decision_set = DocumentClassificationDecisionSet
    run = DocumentClassificationRun
    effective_exists = exists(
        select(decision_set.id).where(
            decision_set.document_revision_id == DocumentRevision.id,
            decision_set.library_id == Document.library_id,
            decision_set.lifecycle == "effective",
        )
    )
    latest_run_status = (
        select(run.status)
        .where(
            run.document_revision_id == DocumentRevision.id,
            run.library_id == Document.library_id,
        )
        .order_by(run.generation_no.desc(), run.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_run_created = (
        select(run.created_at)
        .where(
            run.document_revision_id == DocumentRevision.id,
            run.library_id == Document.library_id,
        )
        .order_by(run.generation_no.desc(), run.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_removed_created = (
        select(decision_set.created_at)
        .where(
            decision_set.document_revision_id == DocumentRevision.id,
            decision_set.library_id == Document.library_id,
            decision_set.source == "manual",
            decision_set.lifecycle == "removed",
        )
        .order_by(decision_set.generation_no.desc(), decision_set.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    manually_unclassified = and_(
        latest_removed_created.is_not(None),
        or_(latest_run_created.is_(None), latest_removed_created >= latest_run_created),
    )
    if state == "classified":
        return effective_exists
    if state == "pending_review":
        return and_(
            ~effective_exists,
            ~manually_unclassified,
            latest_run_status.in_(("pending_review", "blocked_manual")),
        )
    if state == "failed":
        return and_(
            ~effective_exists,
            ~manually_unclassified,
            latest_run_status == "failed",
        )
    return and_(
        ~effective_exists,
        or_(
            manually_unclassified,
            latest_run_status.is_(None),
            ~latest_run_status.in_(("pending_review", "blocked_manual", "failed")),
        ),
    )


def _document_filter_predicates(query: CatalogDocumentQuery):
    predicates: list[Any] = []
    if query.title_query is not None:
        escaped = query.title_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicates.append(
            func.coalesce(Document.display_name, Document.title, "Untitled").ilike(
                f"%{escaped}%",
                escape="\\",
            )
        )
    if query.document_status is not None:
        predicates.append(Document.status == query.document_status)
    if query.classification_state is not None:
        predicates.append(_classification_state_predicate(query.classification_state))
    if query.label_id is not None:
        predicates.append(
            exists(
                select(DocumentClassificationDecision.id)
                .join(
                    DocumentClassificationDecisionSet,
                    DocumentClassificationDecisionSet.id
                    == DocumentClassificationDecision.decision_set_id,
                )
                .where(
                    DocumentClassificationDecisionSet.library_id == Document.library_id,
                    DocumentClassificationDecisionSet.document_revision_id
                    == DocumentRevision.id,
                    DocumentClassificationDecisionSet.lifecycle == "effective",
                    DocumentClassificationDecision.label_id == query.label_id,
                )
            )
        )
    if query.uploader_id is not None:
        predicates.append(Document.created_by == query.uploader_id)
    elif query.include_system_uploader:
        predicates.append(Document.created_by.is_(None))
    if query.uploaded_from is not None:
        predicates.append(
            Document.created_at
            >= datetime.combine(query.uploaded_from, time.min, tzinfo=timezone.utc)
        )
    if query.uploaded_to is not None:
        predicates.append(
            Document.created_at
            < datetime.combine(
                query.uploaded_to + timedelta(days=1),
                time.min,
                tzinfo=timezone.utc,
            )
        )
    return tuple(predicates)


def _masked_email(value: str | None) -> str | None:
    if not value:
        return None
    local, separator, domain = value.partition("@")
    if not separator or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def catalog_uploader_read(
    user: User | None,
    *,
    reveal_email: bool,
) -> CatalogUploaderRead:
    if user is None:
        return CatalogUploaderRead(
            display_name="系统或历史导入",
            username=None,
            email=None,
            is_system=True,
        )
    if user.deleted_at is not None:
        return CatalogUploaderRead(
            display_name="已注销用户",
            username=None,
            email=None,
            is_system=False,
        )
    display_name = user.display_name or user.username or _masked_email(user.email) or "未知上传人"
    return CatalogUploaderRead(
        display_name=display_name,
        username=user.username,
        email=user.email if reveal_email else _masked_email(user.email),
        is_system=False,
    )


def _uploader_option_label(uploader: CatalogUploaderRead) -> str:
    parts = [uploader.display_name]
    if uploader.username and uploader.username != uploader.display_name:
        parts.append(uploader.username)
    if uploader.email:
        parts.append(uploader.email)
    return " · ".join(parts)


def _document_title(document: Document, revision: DocumentRevision) -> str:
    value = document.display_name or document.title or revision.title or "Untitled"
    return value.strip()[:512] or "Untitled"


async def _current_document(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
) -> _CurrentDocument:
    row = (
        await db.execute(
            _base_current_document_statement(library_id).where(Document.id == document_id)
        )
    ).first()
    if row is None:
        raise _not_found()
    return _CurrentDocument(row[0], row[1])


async def _current_artifacts(
    db,
    *,
    library_id: uuid.UUID,
    revision_ids: tuple[uuid.UUID, ...],
) -> tuple[
    dict[uuid.UUID, dict[str, KnowledgeArtifact]],
    dict[tuple[uuid.UUID, str], KnowledgeArtifactJob],
]:
    if not revision_ids:
        return {}, {}
    artifacts = tuple(
        (
            await db.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.library_id == library_id,
                    KnowledgeArtifact.document_revision_id.in_(revision_ids),
                    KnowledgeArtifact.lifecycle_state == "current",
                )
            )
        ).scalars().all()
    )
    by_revision: dict[uuid.UUID, dict[str, KnowledgeArtifact]] = defaultdict(dict)
    for artifact in artifacts:
        if artifact.artifact_type in by_revision[artifact.document_revision_id]:
            raise KnowledgeCatalogError("catalog_invariant_failed")
        by_revision[artifact.document_revision_id][artifact.artifact_type] = artifact

    job_rows = tuple(
        (
            await db.execute(
                select(KnowledgeArtifactJob)
                .where(
                    KnowledgeArtifactJob.library_id == library_id,
                    KnowledgeArtifactJob.document_revision_id.in_(revision_ids),
                )
                .distinct(
                    KnowledgeArtifactJob.document_revision_id,
                    KnowledgeArtifactJob.artifact_type,
                )
                .order_by(
                    KnowledgeArtifactJob.document_revision_id,
                    KnowledgeArtifactJob.artifact_type,
                    KnowledgeArtifactJob.retry_generation.desc(),
                    KnowledgeArtifactJob.created_at.desc(),
                    KnowledgeArtifactJob.id.desc(),
                )
            )
        ).scalars().all()
    )
    jobs = {
        (job.document_revision_id, job.artifact_type): job for job in job_rows
    }
    return dict(by_revision), jobs


async def _current_classifications(
    db,
    *,
    library_id: uuid.UUID,
    revision_ids: tuple[uuid.UUID, ...],
    enabled: bool,
) -> tuple[
    dict[uuid.UUID, CatalogClassificationRead],
    dict[uuid.UUID, DocumentClassificationJob],
]:
    if not revision_ids or not enabled:
        return {}, {}
    effective_sets = tuple(
        (
            await db.execute(
                select(DocumentClassificationDecisionSet).where(
                    DocumentClassificationDecisionSet.library_id == library_id,
                    DocumentClassificationDecisionSet.document_revision_id.in_(revision_ids),
                    DocumentClassificationDecisionSet.lifecycle == "effective",
                )
            )
        ).scalars().all()
    )
    effective_by_revision = {row.document_revision_id: row for row in effective_sets}
    if len(effective_by_revision) != len(effective_sets):
        raise KnowledgeCatalogError("catalog_invariant_failed")

    latest_sets = tuple(
        (
            await db.execute(
                select(DocumentClassificationDecisionSet)
                .where(
                    DocumentClassificationDecisionSet.library_id == library_id,
                    DocumentClassificationDecisionSet.document_revision_id.in_(revision_ids),
                )
                .distinct(DocumentClassificationDecisionSet.document_revision_id)
                .order_by(
                    DocumentClassificationDecisionSet.document_revision_id,
                    DocumentClassificationDecisionSet.generation_no.desc(),
                    DocumentClassificationDecisionSet.id.desc(),
                )
            )
        ).scalars().all()
    )
    latest_set_by_revision = {row.document_revision_id: row for row in latest_sets}
    latest_runs = tuple(
        (
            await db.execute(
                select(DocumentClassificationRun)
                .where(
                    DocumentClassificationRun.library_id == library_id,
                    DocumentClassificationRun.document_revision_id.in_(revision_ids),
                )
                .distinct(DocumentClassificationRun.document_revision_id)
                .order_by(
                    DocumentClassificationRun.document_revision_id,
                    DocumentClassificationRun.generation_no.desc(),
                    DocumentClassificationRun.id.desc(),
                )
            )
        ).scalars().all()
    )
    latest_run_by_revision = {row.document_revision_id: row for row in latest_runs}

    decisions_by_set: dict[uuid.UUID, list[CatalogClassificationLabelRead]] = defaultdict(list)
    set_ids = tuple(row.id for row in effective_sets)
    if set_ids:
        decision_rows = (
            await db.execute(
                select(DocumentClassificationDecision, ClassificationLabel)
                .join(
                    ClassificationLabel,
                    ClassificationLabel.id == DocumentClassificationDecision.label_id,
                )
                .where(DocumentClassificationDecision.decision_set_id.in_(set_ids))
                .order_by(
                    DocumentClassificationDecision.decision_set_id,
                    DocumentClassificationDecision.ordinal,
                )
            )
        ).all()
        sets_by_id = {row.id: row for row in effective_sets}
        for decision, label in decision_rows:
            owner = sets_by_id.get(decision.decision_set_id)
            if owner is None or label.taxonomy_version_id != owner.taxonomy_version_id:
                raise KnowledgeCatalogError("catalog_invariant_failed")
            decisions_by_set[decision.decision_set_id].append(
                CatalogClassificationLabelRead(
                    id=label.id,
                    key=label.key,
                    label=label.label,
                    role=decision.role,
                    ordinal=decision.ordinal,
                )
            )

    classifications: dict[uuid.UUID, CatalogClassificationRead] = {}
    for revision_id in revision_ids:
        effective = effective_by_revision.get(revision_id)
        latest_set = latest_set_by_revision.get(revision_id)
        latest_run = latest_run_by_revision.get(revision_id)
        manually_unclassified = (
            latest_set is not None
            and latest_set.source == "manual"
            and latest_set.lifecycle == "removed"
            and (latest_run is None or latest_set.created_at >= latest_run.created_at)
        )
        if effective is not None:
            state = "classified"
        elif manually_unclassified:
            state = "unclassified"
        elif latest_run is not None and latest_run.status in {
            "pending_review",
            "blocked_manual",
        }:
            state = "pending_review"
        elif latest_run is not None and latest_run.status == "failed":
            state = "failed"
        else:
            state = "unclassified"
        classifications[revision_id] = CatalogClassificationRead(
            state=state,
            decision_set_id=effective.id if effective is not None else None,
            taxonomy_version_id=(
                effective.taxonomy_version_id if effective is not None else None
            ),
            source=effective.source if effective is not None else None,
            labels=(
                decisions_by_set.get(effective.id, []) if effective is not None else []
            ),
            latest_run_id=latest_run.id if latest_run is not None else None,
            latest_run_status=latest_run.status if latest_run is not None else None,
        )

    job_rows = tuple(
        (
            await db.execute(
                select(DocumentClassificationJob)
                .where(
                    DocumentClassificationJob.library_id == library_id,
                    DocumentClassificationJob.document_revision_id.in_(revision_ids),
                )
                .distinct(DocumentClassificationJob.document_revision_id)
                .order_by(
                    DocumentClassificationJob.document_revision_id,
                    DocumentClassificationJob.retry_generation.desc(),
                    DocumentClassificationJob.created_at.desc(),
                    DocumentClassificationJob.id.desc(),
                )
            )
        ).scalars().all()
    )
    return classifications, {row.document_revision_id: row for row in job_rows}


async def _latest_graph_jobs(
    db,
    *,
    library_id: uuid.UUID,
    revision_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, GraphExtractionJob]:
    if not revision_ids:
        return {}
    rows = tuple(
        (
            await db.execute(
                select(GraphExtractionJob)
                .where(
                    GraphExtractionJob.library_id == library_id,
                    GraphExtractionJob.document_revision_id.in_(revision_ids),
                    GraphExtractionJob.execution_mode == "production",
                )
                .distinct(GraphExtractionJob.document_revision_id)
                .order_by(
                    GraphExtractionJob.document_revision_id,
                    GraphExtractionJob.retry_generation.desc(),
                    GraphExtractionJob.created_at.desc(),
                    GraphExtractionJob.id.desc(),
                )
            )
        ).scalars().all()
    )
    return {row.document_revision_id: row for row in rows}


async def _current_files(
    db,
    *,
    library_id: uuid.UUID,
    revision_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, DocumentRevisionFile]:
    if not revision_ids:
        return {}
    rows = tuple(
        (
            await db.execute(
                select(DocumentRevisionFile).where(
                    DocumentRevisionFile.library_id == library_id,
                    DocumentRevisionFile.document_revision_id.in_(revision_ids),
                    DocumentRevisionFile.lifecycle_status == "available",
                )
            )
        ).scalars().all()
    )
    result = {row.document_revision_id: row for row in rows}
    if len(result) != len(rows):
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return result


async def _current_uploaders(
    db,
    *,
    current_documents: tuple[_CurrentDocument, ...],
) -> dict[uuid.UUID, User]:
    uploader_ids = tuple(
        {
            item.document.created_by
            for item in current_documents
            if item.document.created_by is not None
        }
    )
    if not uploader_ids:
        return {}
    rows = tuple(
        (
            await db.execute(select(User).where(User.id.in_(uploader_ids)))
        ).scalars().all()
    )
    return {row.id: row for row in rows}


def _publication_fact_base(library_id: uuid.UUID, item_kind: str):
    return (
        select(GraphPublicationItem, EvidenceUnit)
        .join(
            GraphPublication,
            and_(
                GraphPublication.id == GraphPublicationItem.publication_id,
                GraphPublication.library_id == library_id,
                GraphPublication.status == "active",
            ),
        )
        .join(
            OntologyVersion,
            and_(
                OntologyVersion.id == GraphPublication.ontology_version_id,
                OntologyVersion.library_id == library_id,
                OntologyVersion.status == "active",
            ),
        )
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.library_id == library_id,
                EvidenceUnit.status == "active",
                GraphPublicationItem.support_evidence_ids.op("?")(
                    cast(EvidenceUnit.id, String)
                ),
            ),
        )
        .where(
            GraphPublicationItem.library_id == library_id,
            GraphPublicationItem.ontology_version_id == GraphPublication.ontology_version_id,
            GraphPublicationItem.item_kind == item_kind,
            GraphPublicationItem.status == "active",
        )
    )


async def _document_graph_counts(
    db,
    *,
    library_id: uuid.UUID,
    current_documents: tuple[_CurrentDocument, ...],
    enabled: bool,
) -> dict[uuid.UUID, CatalogGraphCountsRead]:
    counts = {
        item.revision.id: CatalogGraphCountsRead(entities=0, relations=0)
        for item in current_documents
    }
    if not current_documents or not enabled:
        return counts
    revision_ids = tuple(item.revision.id for item in current_documents)
    entity_rows = (
        await db.execute(
            _publication_fact_base(library_id, "entity")
            .join(
                EntityMention,
                and_(
                    EntityMention.library_id == library_id,
                    EntityMention.entity_id == GraphPublicationItem.entity_id,
                    EntityMention.evidence_id == EvidenceUnit.id,
                    EntityMention.document_id == EvidenceUnit.document_id,
                    EntityMention.document_revision_id == EvidenceUnit.document_revision_id,
                    EntityMention.status == "active",
                ),
            )
            .join(
                Entity,
                and_(
                    Entity.id == GraphPublicationItem.entity_id,
                    Entity.library_id == library_id,
                    Entity.ontology_version_id == GraphPublicationItem.ontology_version_id,
                    Entity.status == "active",
                ),
            )
            .join(
                EntityType,
                and_(
                    EntityType.id == Entity.entity_type_id,
                    EntityType.library_id == library_id,
                    EntityType.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    EntityType.status == "active",
                ),
            )
            .where(EvidenceUnit.document_revision_id.in_(revision_ids))
            .with_only_columns(
                EvidenceUnit.document_revision_id,
                func.count(func.distinct(GraphPublicationItem.id)),
            )
            .group_by(EvidenceUnit.document_revision_id)
        )
    ).all()
    count_source_entity = aliased(Entity, name="catalog_count_source_entity")
    count_target_entity = aliased(Entity, name="catalog_count_target_entity")
    count_source_item = aliased(GraphPublicationItem, name="catalog_count_source_item")
    count_target_item = aliased(GraphPublicationItem, name="catalog_count_target_item")
    relation_rows = (
        # Endpoint Publication membership is part of a healthy published relation.
        await db.execute(
            _publication_fact_base(library_id, "relation")
            .join(
                RelationEvidence,
                and_(
                    RelationEvidence.library_id == library_id,
                    RelationEvidence.relation_id == GraphPublicationItem.relation_id,
                    RelationEvidence.evidence_id == EvidenceUnit.id,
                    RelationEvidence.document_id == EvidenceUnit.document_id,
                    RelationEvidence.document_revision_id == EvidenceUnit.document_revision_id,
                    RelationEvidence.status == "active",
                ),
            )
            .join(
                KnowledgeRelation,
                and_(
                    KnowledgeRelation.id == GraphPublicationItem.relation_id,
                    KnowledgeRelation.library_id == library_id,
                    KnowledgeRelation.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    KnowledgeRelation.status == "active",
                    KnowledgeRelation.review_status.in_(("approved", "not_required")),
                ),
            )
            .join(
                RelationType,
                and_(
                    RelationType.id == KnowledgeRelation.relation_type_id,
                    RelationType.library_id == library_id,
                    RelationType.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    RelationType.status == "active",
                ),
            )
            .join(
                count_source_entity,
                and_(
                    count_source_entity.id == KnowledgeRelation.source_entity_id,
                    count_source_entity.library_id == library_id,
                    count_source_entity.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    count_source_entity.status == "active",
                ),
            )
            .join(
                count_target_entity,
                and_(
                    count_target_entity.id == KnowledgeRelation.target_entity_id,
                    count_target_entity.library_id == library_id,
                    count_target_entity.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    count_target_entity.status == "active",
                ),
            )
            .join(
                count_source_item,
                and_(
                    count_source_item.publication_id
                    == GraphPublicationItem.publication_id,
                    count_source_item.library_id == library_id,
                    count_source_item.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    count_source_item.item_kind == "entity",
                    count_source_item.status == "active",
                    count_source_item.entity_id == count_source_entity.id,
                ),
            )
            .join(
                count_target_item,
                and_(
                    count_target_item.publication_id
                    == GraphPublicationItem.publication_id,
                    count_target_item.library_id == library_id,
                    count_target_item.ontology_version_id
                    == GraphPublicationItem.ontology_version_id,
                    count_target_item.item_kind == "entity",
                    count_target_item.status == "active",
                    count_target_item.entity_id == count_target_entity.id,
                ),
            )
            .where(EvidenceUnit.document_revision_id.in_(revision_ids))
            .with_only_columns(
                EvidenceUnit.document_revision_id,
                func.count(func.distinct(GraphPublicationItem.id)),
            )
            .group_by(EvidenceUnit.document_revision_id)
        )
    ).all()
    entity_counts = {row[0]: row[1] for row in entity_rows}
    relation_counts = {row[0]: row[1] for row in relation_rows}
    return {
        revision_id: CatalogGraphCountsRead(
            entities=entity_counts.get(revision_id, 0),
            relations=relation_counts.get(revision_id, 0),
        )
        for revision_id in revision_ids
    }


async def _hydrate_current_documents(
    db,
    *,
    library: Library,
    current_documents: tuple[_CurrentDocument, ...],
    config: Settings,
) -> _CatalogHydration:
    revision_ids = tuple(item.revision.id for item in current_documents)
    artifacts, artifact_jobs = await _current_artifacts(
        db,
        library_id=library.id,
        revision_ids=revision_ids,
    )
    classifications, classification_jobs = await _current_classifications(
        db,
        library_id=library.id,
        revision_ids=revision_ids,
        enabled=config.classification_decision_enabled,
    )
    graph_jobs = await _latest_graph_jobs(
        db,
        library_id=library.id,
        revision_ids=revision_ids,
    )
    files = await _current_files(
        db,
        library_id=library.id,
        revision_ids=revision_ids,
    )
    graph_counts = await _document_graph_counts(
        db,
        library_id=library.id,
        current_documents=current_documents,
        enabled=config.graph_retrieval_enabled,
    )
    uploaders = await _current_uploaders(
        db,
        current_documents=current_documents,
    )
    return _CatalogHydration(
        artifacts=artifacts,
        artifact_jobs=artifact_jobs,
        classifications=classifications,
        classification_jobs=classification_jobs,
        graph_jobs=graph_jobs,
        files=files,
        graph_counts=graph_counts,
        uploaders=uploaders,
    )


def _artifact_capability_state(
    *,
    enabled: bool,
    artifact: KnowledgeArtifact | None,
    job: KnowledgeArtifactJob | None,
) -> CatalogCapabilityState:
    if artifact is not None:
        return "ready"
    if not enabled:
        return "disabled"
    if job is None:
        return "unavailable"
    if job.status in {"queued", "processing"}:
        return "processing"
    if job.status == "failed":
        return "failed"
    return "unavailable"


def _classification_capability_state(
    *,
    enabled: bool,
    classification: CatalogClassificationRead,
    job: DocumentClassificationJob | None,
) -> CatalogCapabilityState:
    if not enabled:
        return "disabled"
    if classification.state == "classified":
        return "ready"
    if classification.state == "pending_review":
        return "pending_review"
    if classification.state == "failed":
        return "failed"
    if job is not None and job.status in {"queued", "processing"}:
        return "processing"
    if job is not None and job.status == "failed":
        return "failed"
    return "ready"


def _graph_capability_state(
    *,
    enabled: bool,
    counts: CatalogGraphCountsRead,
    job: GraphExtractionJob | None,
) -> CatalogCapabilityState:
    if not enabled:
        return "disabled"
    if counts.entities or counts.relations:
        return "ready"
    if job is None:
        return "unavailable"
    if job.status in {"queued", "processing"}:
        return "processing"
    if job.status == "failed":
        return "failed"
    if job.status in {"succeeded", "partially_succeeded"}:
        return "ready"
    return "unavailable"


def _empty_classification() -> CatalogClassificationRead:
    return CatalogClassificationRead(
        state="unclassified",
        decision_set_id=None,
        taxonomy_version_id=None,
        source=None,
        labels=[],
        latest_run_id=None,
        latest_run_status=None,
    )


def _validated_artifact_payload(artifact: KnowledgeArtifact):
    try:
        return validate_artifact_payload(
            artifact.artifact_type,
            artifact.contract_version,
            artifact.payload,
        )
    except ArtifactContractError as exc:
        raise KnowledgeCatalogError("catalog_invariant_failed") from exc


def _summary_excerpt(artifact: KnowledgeArtifact | None) -> str | None:
    if artifact is None:
        return None
    payload = _validated_artifact_payload(artifact)
    if not isinstance(payload, SummaryPayloadV1):
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return payload.summary.strip()[:1000]


def _document_uploader_read(
    document: Document,
    hydration: _CatalogHydration,
    *,
    reveal_email: bool,
) -> CatalogUploaderRead:
    if document.created_by is None:
        return catalog_uploader_read(None, reveal_email=reveal_email)
    uploader = hydration.uploaders.get(document.created_by)
    if uploader is None:
        return CatalogUploaderRead(
            display_name="已注销用户",
            username=None,
            email=None,
            is_system=False,
        )
    return catalog_uploader_read(uploader, reveal_email=reveal_email)


def _list_item(
    current: _CurrentDocument,
    hydration: _CatalogHydration,
    *,
    library: Library,
    config: Settings,
    include_uploader: bool = False,
    reveal_uploader_email: bool = False,
    viewer_id: uuid.UUID | None = None,
    can_delete_any: bool = False,
) -> CatalogDocumentListItemRead:
    document, revision = current.document, current.revision
    artifacts = hydration.artifacts.get(revision.id, {})
    classification = hydration.classifications.get(revision.id, _empty_classification())
    graph_counts = hydration.graph_counts.get(
        revision.id,
        CatalogGraphCountsRead(entities=0, relations=0),
    )
    summary_state = _artifact_capability_state(
        enabled=(
            config.knowledge_artifact_runtime_enabled
            and bool(library.summary_artifact_enabled)
        ),
        artifact=artifacts.get("summary"),
        job=hydration.artifact_jobs.get((revision.id, "summary")),
    )
    outline_state = _artifact_capability_state(
        enabled=(
            config.knowledge_artifact_runtime_enabled
            and bool(library.outline_artifact_enabled)
        ),
        artifact=artifacts.get("outline"),
        job=hydration.artifact_jobs.get((revision.id, "outline")),
    )
    classification_state = _classification_capability_state(
        enabled=config.classification_decision_enabled,
        classification=classification,
        job=hydration.classification_jobs.get(revision.id),
    )
    graph_state = _graph_capability_state(
        enabled=config.graph_retrieval_enabled,
        counts=graph_counts,
        job=hydration.graph_jobs.get(revision.id),
    )
    projection = project_catalog_capabilities(
        document_status=document.status,
        revision_status=revision.status,
        source_available=(
            bool(isinstance(revision.normalized_text, str) and revision.normalized_text.strip())
            or revision.id in hydration.files
        ),
        summary_state=summary_state,
        outline_state=outline_state,
        classification_state=classification_state,
        graph_state=graph_state,
    )
    return CatalogDocumentListItemRead(
        document_id=document.id,
        library_id=library.id,
        title=_document_title(document, revision),
        document_status=document.status,
        revision_id=revision.id,
        revision_no=revision.revision_no,
        revision_status=revision.status,
        revision_content_hash=revision.content_hash,
        updated_at=document.updated_at,
        uploaded_at=document.created_at,
        uploader=(
            _document_uploader_read(
                document,
                hydration,
                reveal_email=reveal_uploader_email,
            )
            if include_uploader
            else None
        ),
        can_delete=bool(
            can_delete_any
            or (
                viewer_id is not None
                and document.created_by is not None
                and document.created_by == viewer_id
            )
        ),
        overall_state=projection.overall_state,
        capabilities=CatalogCapabilitiesRead(
            source=projection.source,
            search=projection.search,
            chat=projection.chat,
            summary=projection.summary,
            outline=projection.outline,
            classification=projection.classification,
            graph=projection.graph,
        ),
        classification=classification,
        summary_excerpt=_summary_excerpt(artifacts.get("summary")),
        graph_counts=graph_counts,
    )


async def list_catalog_documents(
    db,
    *,
    library: Library,
    query: CatalogDocumentQuery,
    cursor_value: str | None = None,
    config: Settings = settings,
    include_uploader: bool = False,
    reveal_uploader_email: bool = False,
    viewer_id: uuid.UUID | None = None,
    can_delete_any: bool = False,
) -> CatalogDocumentPageRead:
    if not isinstance(library, Library) or not isinstance(query, CatalogDocumentQuery):
        raise KnowledgeCatalogError("catalog_request_invalid")
    if (
        query.classification_state is not None or query.label_id is not None
    ) and not config.classification_decision_enabled:
        raise KnowledgeCatalogError("catalog_classification_filter_unavailable")
    fingerprint = catalog_filter_fingerprint(library.id, query)
    cursor = decode_catalog_document_cursor(
        cursor_value,
        expected_filter_fingerprint=fingerprint,
    )
    predicates = _document_filter_predicates(query)
    base = _base_current_document_statement(library.id).where(*predicates)
    total = (
        await db.execute(
            select(func.count())
            .select_from(base.order_by(None).subquery("catalog_document_count"))
        )
    ).scalar_one()
    page_statement = base
    if cursor is not None:
        page_statement = page_statement.where(
            or_(
                Document.updated_at < cursor.updated_at,
                and_(
                    Document.updated_at == cursor.updated_at,
                    Document.id < cursor.document_id,
                ),
            )
        )
    rows = (
        await db.execute(
            page_statement.order_by(Document.updated_at.desc(), Document.id.desc()).limit(
                query.limit + 1
            )
        )
    ).all()
    has_more = len(rows) > query.limit
    selected_rows = rows[: query.limit]
    current_documents = tuple(_CurrentDocument(row[0], row[1]) for row in selected_rows)
    hydration = await _hydrate_current_documents(
        db,
        library=library,
        current_documents=current_documents,
        config=config,
    )
    items = [
        _list_item(
            current,
            hydration,
            library=library,
            config=config,
            include_uploader=include_uploader,
            reveal_uploader_email=reveal_uploader_email,
            viewer_id=viewer_id,
            can_delete_any=can_delete_any,
        )
        for current in current_documents
    ]
    next_cursor = None
    if has_more and current_documents:
        last = current_documents[-1].document
        next_cursor = encode_catalog_document_cursor(
            CatalogDocumentCursor(last.updated_at, last.id, fingerprint)
        )
    return CatalogDocumentPageRead(items=items, total=total, next_cursor=next_cursor)


async def list_catalog_uploader_options(
    db,
    *,
    library: Library,
    viewer: User,
    reveal_email: bool,
) -> CatalogUploaderOptionsRead:
    uploader_ids = tuple(
        row[0]
        for row in (
            await db.execute(
                select(Document.created_by)
                .where(
                    Document.library_id == library.id,
                    Document.deleted_at.is_(None),
                )
                .distinct()
                .order_by(Document.created_by)
                .limit(100)
            )
        ).all()
    )
    user_ids = tuple(item for item in uploader_ids if item is not None)
    users: dict[uuid.UUID, User] = {}
    if user_ids:
        users = {
            row.id: row
            for row in (
                await db.execute(select(User).where(User.id.in_(user_ids)))
            ).scalars().all()
        }
    items = [CatalogUploaderOptionRead(value=str(viewer.id), label="我上传的")]
    for uploader_id in user_ids:
        if uploader_id == viewer.id:
            continue
        uploader = users.get(uploader_id)
        if uploader is None:
            label = "已注销用户"
        else:
            label = _uploader_option_label(
                catalog_uploader_read(uploader, reveal_email=reveal_email)
            )
        items.append(CatalogUploaderOptionRead(value=str(uploader_id), label=label))
    if None in uploader_ids:
        items.append(CatalogUploaderOptionRead(value="system", label="系统或历史导入"))
    return CatalogUploaderOptionsRead(items=items)


def _summary_read(artifact: KnowledgeArtifact | None) -> CatalogSummaryRead | None:
    if artifact is None:
        return None
    payload = _validated_artifact_payload(artifact)
    if not isinstance(payload, SummaryPayloadV1):
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return CatalogSummaryRead(
        artifact_id=artifact.id,
        contract_version="summary-v1",
        extractor_version=artifact.extractor_version,
        generation_mode=payload.generation_mode,
        summary=payload.summary,
        source_character_count=payload.source_character_count,
        truncated=payload.truncated,
    )


def _outline_read(artifact: KnowledgeArtifact | None) -> CatalogOutlineRead | None:
    if artifact is None:
        return None
    payload = _validated_artifact_payload(artifact)
    if not isinstance(payload, OutlinePayloadV1):
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return CatalogOutlineRead(
        artifact_id=artifact.id,
        contract_version="outline-v1",
        extractor_version=artifact.extractor_version,
        generation_mode=payload.generation_mode,
        items=payload.items,
    )


def _revision_file_read(row: DocumentRevisionFile | None) -> CatalogRevisionFileRead | None:
    if row is None:
        return None
    return CatalogRevisionFileRead(
        id=row.id,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
    )


def _entity_fact_projection(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
):
    return (
        select(
            GraphPublication.id.label("publication_id"),
            GraphPublication.ontology_version_id.label("ontology_version_id"),
            GraphPublicationItem.id.label("item_id"),
            GraphPublicationItem.item_hash.label("item_hash"),
            Entity.id.label("entity_id"),
            EntityType.id.label("entity_type_id"),
            EntityType.key.label("entity_type_key"),
            EntityType.label.label("entity_type_label"),
            Entity.canonical_name.label("canonical_name"),
            Entity.source_type.label("source_type"),
            Entity.confidence.label("confidence"),
        )
        .select_from(GraphPublicationItem)
        .join(
            GraphPublication,
            and_(
                GraphPublication.id == GraphPublicationItem.publication_id,
                GraphPublication.library_id == library_id,
                GraphPublication.status == "active",
            ),
        )
        .join(
            OntologyVersion,
            and_(
                OntologyVersion.id == GraphPublication.ontology_version_id,
                OntologyVersion.library_id == library_id,
                OntologyVersion.status == "active",
            ),
        )
        .join(
            Entity,
            and_(
                Entity.id == GraphPublicationItem.entity_id,
                Entity.library_id == library_id,
                Entity.ontology_version_id == GraphPublication.ontology_version_id,
                Entity.status == "active",
            ),
        )
        .join(
            EntityType,
            and_(
                EntityType.id == Entity.entity_type_id,
                EntityType.library_id == library_id,
                EntityType.ontology_version_id == GraphPublication.ontology_version_id,
                EntityType.status == "active",
            ),
        )
        .join(
            EntityMention,
            and_(
                EntityMention.library_id == library_id,
                EntityMention.entity_id == Entity.id,
                EntityMention.document_id == document_id,
                EntityMention.document_revision_id == revision_id,
                EntityMention.status == "active",
            ),
        )
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.id == EntityMention.evidence_id,
                EvidenceUnit.library_id == library_id,
                EvidenceUnit.document_id == document_id,
                EvidenceUnit.document_revision_id == revision_id,
                EvidenceUnit.status == "active",
                GraphPublicationItem.support_evidence_ids.op("?")(
                    cast(EvidenceUnit.id, String)
                ),
            ),
        )
        .where(
            GraphPublicationItem.library_id == library_id,
            GraphPublicationItem.ontology_version_id == GraphPublication.ontology_version_id,
            GraphPublicationItem.item_kind == "entity",
            GraphPublicationItem.status == "active",
            GraphPublicationItem.relation_id.is_(None),
        )
        .distinct()
    )


def _relation_fact_projection(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
):
    source_entity = aliased(Entity, name="catalog_source_entity")
    target_entity = aliased(Entity, name="catalog_target_entity")
    source_item = aliased(GraphPublicationItem, name="catalog_source_item")
    target_item = aliased(GraphPublicationItem, name="catalog_target_item")
    return (
        select(
            GraphPublication.id.label("publication_id"),
            GraphPublication.ontology_version_id.label("ontology_version_id"),
            GraphPublicationItem.id.label("item_id"),
            GraphPublicationItem.item_hash.label("item_hash"),
            KnowledgeRelation.id.label("relation_id"),
            RelationType.id.label("relation_type_id"),
            RelationType.key.label("relation_type_key"),
            RelationType.label.label("relation_type_label"),
            RelationType.direction.label("direction"),
            source_entity.id.label("source_entity_id"),
            source_entity.canonical_name.label("source_entity_name"),
            target_entity.id.label("target_entity_id"),
            target_entity.canonical_name.label("target_entity_name"),
            KnowledgeRelation.source_type.label("source_type"),
            KnowledgeRelation.confidence.label("confidence"),
            KnowledgeRelation.review_status.label("review_status"),
        )
        .select_from(GraphPublicationItem)
        .join(
            GraphPublication,
            and_(
                GraphPublication.id == GraphPublicationItem.publication_id,
                GraphPublication.library_id == library_id,
                GraphPublication.status == "active",
            ),
        )
        .join(
            OntologyVersion,
            and_(
                OntologyVersion.id == GraphPublication.ontology_version_id,
                OntologyVersion.library_id == library_id,
                OntologyVersion.status == "active",
            ),
        )
        .join(
            KnowledgeRelation,
            and_(
                KnowledgeRelation.id == GraphPublicationItem.relation_id,
                KnowledgeRelation.library_id == library_id,
                KnowledgeRelation.ontology_version_id
                == GraphPublication.ontology_version_id,
                KnowledgeRelation.status == "active",
                KnowledgeRelation.review_status.in_(("approved", "not_required")),
            ),
        )
        .join(
            RelationType,
            and_(
                RelationType.id == KnowledgeRelation.relation_type_id,
                RelationType.library_id == library_id,
                RelationType.ontology_version_id == GraphPublication.ontology_version_id,
                RelationType.status == "active",
            ),
        )
        .join(
            source_entity,
            and_(
                source_entity.id == KnowledgeRelation.source_entity_id,
                source_entity.library_id == library_id,
                source_entity.ontology_version_id == GraphPublication.ontology_version_id,
                source_entity.status == "active",
            ),
        )
        .join(
            target_entity,
            and_(
                target_entity.id == KnowledgeRelation.target_entity_id,
                target_entity.library_id == library_id,
                target_entity.ontology_version_id == GraphPublication.ontology_version_id,
                target_entity.status == "active",
            ),
        )
        .join(
            source_item,
            and_(
                source_item.publication_id == GraphPublication.id,
                source_item.library_id == library_id,
                source_item.ontology_version_id == GraphPublication.ontology_version_id,
                source_item.item_kind == "entity",
                source_item.status == "active",
                source_item.entity_id == source_entity.id,
            ),
        )
        .join(
            target_item,
            and_(
                target_item.publication_id == GraphPublication.id,
                target_item.library_id == library_id,
                target_item.ontology_version_id == GraphPublication.ontology_version_id,
                target_item.item_kind == "entity",
                target_item.status == "active",
                target_item.entity_id == target_entity.id,
            ),
        )
        .join(
            RelationEvidence,
            and_(
                RelationEvidence.library_id == library_id,
                RelationEvidence.relation_id == KnowledgeRelation.id,
                RelationEvidence.document_id == document_id,
                RelationEvidence.document_revision_id == revision_id,
                RelationEvidence.status == "active",
            ),
        )
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.id == RelationEvidence.evidence_id,
                EvidenceUnit.library_id == library_id,
                EvidenceUnit.document_id == document_id,
                EvidenceUnit.document_revision_id == revision_id,
                EvidenceUnit.status == "active",
                GraphPublicationItem.support_evidence_ids.op("?")(
                    cast(EvidenceUnit.id, String)
                ),
            ),
        )
        .where(
            GraphPublicationItem.library_id == library_id,
            GraphPublicationItem.ontology_version_id == GraphPublication.ontology_version_id,
            GraphPublicationItem.item_kind == "relation",
            GraphPublicationItem.status == "active",
            GraphPublicationItem.entity_id.is_(None),
        )
        .distinct()
    )


async def _fact_evidence_by_item(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    item_kind: str,
    item_ids: tuple[uuid.UUID, ...],
    maximum: int,
) -> dict[uuid.UUID, list[CatalogEvidenceLocatorRead]]:
    if not item_ids:
        return {}
    if item_kind == "entity":
        binding = EntityMention
        binding_conditions = (
            binding.entity_id == GraphPublicationItem.entity_id,
            binding.evidence_id == EvidenceUnit.id,
        )
    else:
        binding = RelationEvidence
        binding_conditions = (
            binding.relation_id == GraphPublicationItem.relation_id,
            binding.evidence_id == EvidenceUnit.id,
        )
    candidates = (
        select(
            GraphPublicationItem.id.label("item_id"),
            EvidenceUnit.id.label("evidence_id"),
            EvidenceUnit.document_id.label("document_id"),
            EvidenceUnit.document_revision_id.label("document_revision_id"),
            EvidenceUnit.page_start.label("page_start"),
            EvidenceUnit.page_end.label("page_end"),
            EvidenceUnit.source_start.label("source_start"),
            EvidenceUnit.source_end.label("source_end"),
        )
        .select_from(GraphPublicationItem)
        .join(
            EvidenceUnit,
            and_(
                EvidenceUnit.library_id == library_id,
                EvidenceUnit.document_id == document_id,
                EvidenceUnit.document_revision_id == revision_id,
                EvidenceUnit.status == "active",
                GraphPublicationItem.support_evidence_ids.op("?")(
                    cast(EvidenceUnit.id, String)
                ),
            ),
        )
        .join(
            binding,
            and_(
                binding.library_id == library_id,
                binding.document_id == document_id,
                binding.document_revision_id == revision_id,
                binding.status == "active",
                *binding_conditions,
            ),
        )
        .where(
            GraphPublicationItem.id.in_(item_ids),
            GraphPublicationItem.library_id == library_id,
            GraphPublicationItem.item_kind == item_kind,
            GraphPublicationItem.status == "active",
        )
        .distinct()
        .subquery("catalog_fact_evidence_candidates")
    )
    ranked = select(
        candidates,
        func.row_number()
        .over(
            partition_by=candidates.c.item_id,
            order_by=candidates.c.evidence_id,
        )
        .label("evidence_rank"),
    ).subquery("catalog_ranked_fact_evidence")
    rows = (
        await db.execute(
            select(ranked)
            .where(ranked.c.evidence_rank <= maximum)
            .order_by(ranked.c.item_id, ranked.c.evidence_rank)
        )
    ).all()
    result: dict[uuid.UUID, list[CatalogEvidenceLocatorRead]] = defaultdict(list)
    try:
        for row in rows:
            result[row.item_id].append(
                CatalogEvidenceLocatorRead(
                    evidence_id=row.evidence_id,
                    document_id=row.document_id,
                    document_revision_id=row.document_revision_id,
                    page_start=row.page_start,
                    page_end=row.page_end,
                    source_start=row.source_start,
                    source_end=row.source_end,
                )
            )
    except ValidationError as exc:
        raise KnowledgeCatalogError("catalog_invariant_failed") from exc
    if any(not result.get(item_id) for item_id in item_ids):
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return dict(result)


async def _document_graph(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    config: Settings,
) -> CatalogGraphRead:
    if not config.graph_retrieval_enabled:
        return CatalogGraphRead(
            entities=[],
            relations=[],
            counts=CatalogGraphCountsRead(entities=0, relations=0),
            entities_truncated=False,
            relations_truncated=False,
        )
    entity_projection = _entity_fact_projection(
        library_id=library_id,
        document_id=document_id,
        revision_id=revision_id,
    ).subquery("catalog_document_entities")
    relation_projection = _relation_fact_projection(
        library_id=library_id,
        document_id=document_id,
        revision_id=revision_id,
    ).subquery("catalog_document_relations")
    entity_total = (
        await db.execute(select(func.count()).select_from(entity_projection))
    ).scalar_one()
    relation_total = (
        await db.execute(select(func.count()).select_from(relation_projection))
    ).scalar_one()
    entity_rows = (
        await db.execute(
            select(entity_projection)
            .order_by(
                entity_projection.c.canonical_name,
                entity_projection.c.entity_id,
                entity_projection.c.publication_id,
            )
            .limit(config.knowledge_catalog_max_entity_cards)
        )
    ).all()
    relation_rows = (
        await db.execute(
            select(relation_projection)
            .order_by(
                relation_projection.c.relation_type_key,
                relation_projection.c.source_entity_name,
                relation_projection.c.target_entity_name,
                relation_projection.c.relation_id,
            )
            .limit(config.knowledge_catalog_max_relation_cards)
        )
    ).all()
    entity_evidence = await _fact_evidence_by_item(
        db,
        library_id=library_id,
        document_id=document_id,
        revision_id=revision_id,
        item_kind="entity",
        item_ids=tuple(row.item_id for row in entity_rows),
        maximum=config.knowledge_catalog_max_evidence_per_fact,
    )
    relation_evidence = await _fact_evidence_by_item(
        db,
        library_id=library_id,
        document_id=document_id,
        revision_id=revision_id,
        item_kind="relation",
        item_ids=tuple(row.item_id for row in relation_rows),
        maximum=config.knowledge_catalog_max_evidence_per_fact,
    )
    try:
        entities = [
            CatalogEntityKnowledgeUnitRead(
                publication_id=row.publication_id,
                ontology_version_id=row.ontology_version_id,
                item_id=row.item_id,
                item_hash=row.item_hash,
                entity_id=row.entity_id,
                entity_type_id=row.entity_type_id,
                entity_type_key=row.entity_type_key,
                entity_type_label=row.entity_type_label,
                canonical_name=row.canonical_name,
                source_type=row.source_type,
                confidence=row.confidence,
                evidence=entity_evidence[row.item_id],
            )
            for row in entity_rows
        ]
        relations = [
            CatalogRelationKnowledgeUnitRead(
                publication_id=row.publication_id,
                ontology_version_id=row.ontology_version_id,
                item_id=row.item_id,
                item_hash=row.item_hash,
                relation_id=row.relation_id,
                relation_type_id=row.relation_type_id,
                relation_type_key=row.relation_type_key,
                relation_type_label=row.relation_type_label,
                direction=row.direction,
                source_entity_id=row.source_entity_id,
                source_entity_name=row.source_entity_name,
                target_entity_id=row.target_entity_id,
                target_entity_name=row.target_entity_name,
                source_type=row.source_type,
                confidence=row.confidence,
                review_status=row.review_status,
                evidence=relation_evidence[row.item_id],
            )
            for row in relation_rows
        ]
        return CatalogGraphRead(
            entities=entities,
            relations=relations,
            counts=CatalogGraphCountsRead(
                entities=entity_total,
                relations=relation_total,
            ),
            entities_truncated=len(entities) < entity_total,
            relations_truncated=len(relations) < relation_total,
        )
    except (ValidationError, KeyError) as exc:
        raise KnowledgeCatalogError("catalog_invariant_failed") from exc


async def get_catalog_document_detail(
    db,
    *,
    library: Library,
    document_id: uuid.UUID,
    config: Settings = settings,
    include_uploader: bool = False,
    reveal_uploader_email: bool = False,
    viewer_id: uuid.UUID | None = None,
    can_delete_any: bool = False,
) -> CatalogDocumentDetailRead:
    if not isinstance(library, Library) or not isinstance(document_id, uuid.UUID):
        raise KnowledgeCatalogError("catalog_request_invalid")
    current = await _current_document(
        db,
        library_id=library.id,
        document_id=document_id,
    )
    hydration = await _hydrate_current_documents(
        db,
        library=library,
        current_documents=(current,),
        config=config,
    )
    item = _list_item(
        current,
        hydration,
        library=library,
        config=config,
        include_uploader=include_uploader,
        reveal_uploader_email=reveal_uploader_email,
        viewer_id=viewer_id,
        can_delete_any=can_delete_any,
    )
    artifacts = hydration.artifacts.get(current.revision.id, {})
    graph = await _document_graph(
        db,
        library_id=library.id,
        document_id=document_id,
        revision_id=current.revision.id,
        config=config,
    )
    if graph.counts != item.graph_counts:
        raise KnowledgeCatalogError("catalog_invariant_failed")
    return CatalogDocumentDetailRead(
        **item.model_dump(),
        file=_revision_file_read(hydration.files.get(current.revision.id)),
        summary=_summary_read(artifacts.get("summary")),
        outline=_outline_read(artifacts.get("outline")),
        graph=graph,
    )


def _evidence_fact_statement(
    *,
    library_id: uuid.UUID,
    evidence_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    item_kind: str,
):
    if item_kind == "entity":
        projection = _entity_fact_projection(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        ).subquery("catalog_evidence_entities")
        binding = EntityMention
        fact_id = projection.c.entity_id
        binding_fact = binding.entity_id == projection.c.entity_id
    else:
        projection = _relation_fact_projection(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        ).subquery("catalog_evidence_relations")
        binding = RelationEvidence
        fact_id = projection.c.relation_id
        binding_fact = binding.relation_id == projection.c.relation_id
    return (
        select(
            projection.c.publication_id,
            projection.c.ontology_version_id,
            projection.c.item_id,
            fact_id.label("fact_id"),
            binding.chunk_id.label("chunk_id"),
            projection.c.item_hash,
        )
        .select_from(projection)
        .join(
            GraphPublicationItem,
            and_(
                GraphPublicationItem.id == projection.c.item_id,
                GraphPublicationItem.publication_id == projection.c.publication_id,
                GraphPublicationItem.library_id == library_id,
                GraphPublicationItem.ontology_version_id
                == projection.c.ontology_version_id,
                GraphPublicationItem.item_kind == item_kind,
                GraphPublicationItem.status == "active",
                GraphPublicationItem.support_evidence_ids.op("?")(str(evidence_id)),
            ),
        )
        .join(
            binding,
            and_(
                binding.library_id == library_id,
                binding.evidence_id == evidence_id,
                binding.document_id == document_id,
                binding.document_revision_id == revision_id,
                binding.status == "active",
                binding_fact,
            ),
        )
        .order_by(projection.c.publication_id, projection.c.item_id)
        .limit(101)
    )


def _normalized_title_path(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value[:16]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:512])
    return result or None


async def get_catalog_evidence_detail(
    db,
    *,
    library: Library,
    evidence_id: uuid.UUID,
) -> CatalogEvidenceDetailRead:
    if not isinstance(library, Library) or not isinstance(evidence_id, uuid.UUID):
        raise KnowledgeCatalogError("catalog_request_invalid")
    try:
        detail = await get_evidence_detail(
            db,
            library,
            evidence_id,
            window=2000,
        )
    except LookupError as exc:
        raise _not_found() from exc
    entity_rows = (
        await db.execute(
            _evidence_fact_statement(
                library_id=library.id,
                evidence_id=evidence_id,
                document_id=detail.document_id,
                revision_id=detail.document_revision_id,
                item_kind="entity",
            )
        )
    ).all()
    relation_rows = (
        await db.execute(
            _evidence_fact_statement(
                library_id=library.id,
                evidence_id=evidence_id,
                document_id=detail.document_id,
                revision_id=detail.document_revision_id,
                item_kind="relation",
            )
        )
    ).all()
    combined = [("entity", row) for row in entity_rows] + [
        ("relation", row) for row in relation_rows
    ]
    if not combined:
        raise _not_found()
    truncated = len(combined) > 100
    fact_refs = [
        CatalogEvidenceFactRefRead(
            publication_id=row.publication_id,
            ontology_version_id=row.ontology_version_id,
            item_kind=item_kind,
            item_id=row.item_id,
            fact_id=row.fact_id,
            chunk_id=getattr(row, "chunk_id", None),
            item_hash=row.item_hash,
        )
        for item_kind, row in combined[:100]
    ]
    revision_file_id = (
        await db.execute(
            select(DocumentRevisionFile.id).where(
                DocumentRevisionFile.library_id == library.id,
                DocumentRevisionFile.document_id == detail.document_id,
                DocumentRevisionFile.document_revision_id == detail.document_revision_id,
                DocumentRevisionFile.lifecycle_status == "available",
            )
        )
    ).scalar_one_or_none()
    try:
        return CatalogEvidenceDetailRead(
            evidence_id=detail.id,
            library_id=library.id,
            document_id=detail.document_id,
            document_revision_id=detail.document_revision_id,
            revision_file_id=revision_file_id,
            evidence_kind=detail.evidence_kind,
            text_quote=(detail.text_quote[:8000] if detail.text_quote else None),
            text_window=(detail.text_window[:16000] if detail.text_window else None),
            window_start=detail.window_start,
            window_end=detail.window_end,
            source_start=detail.source_start,
            source_end=detail.source_end,
            page_start=detail.page_start,
            page_end=detail.page_end,
            title_path=_normalized_title_path(detail.title_path),
            fact_refs=fact_refs,
            fact_refs_truncated=truncated,
        )
    except ValidationError as exc:
        raise KnowledgeCatalogError("catalog_invariant_failed") from exc


async def prepare_catalog_file_access(
    db,
    *,
    library: Library,
    revision_file_id: uuid.UUID,
) -> PreparedCatalogFileAccess:
    if not isinstance(library, Library) or not isinstance(revision_file_id, uuid.UUID):
        raise KnowledgeCatalogError("catalog_request_invalid")
    row = (
        await db.execute(
            select(DocumentRevisionFile, Document, DocumentRevision)
            .join(
                Document,
                and_(
                    Document.id == DocumentRevisionFile.document_id,
                    Document.library_id == library.id,
                    Document.current_revision_id
                    == DocumentRevisionFile.document_revision_id,
                    Document.status == "ready",
                    Document.deleted_at.is_(None),
                ),
            )
            .join(
                DocumentRevision,
                and_(
                    DocumentRevision.id == DocumentRevisionFile.document_revision_id,
                    DocumentRevision.library_id == library.id,
                    DocumentRevision.document_id == Document.id,
                    DocumentRevision.status == "ready",
                ),
            )
            .where(
                DocumentRevisionFile.id == revision_file_id,
                DocumentRevisionFile.library_id == library.id,
                DocumentRevisionFile.lifecycle_status == "available",
            )
        )
    ).first()
    if row is None:
        raise _not_found()
    try:
        access = revision_file_access_from_row(row[0])
    except ObjectStorageError as exc:
        raise _not_found() from exc
    return PreparedCatalogFileAccess(
        revision_file_id=row[0].id,
        document_id=row[1].id,
        document_revision_id=row[2].id,
        access=access,
    )
