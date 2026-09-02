from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_CREATE_NEW,
    ENTITY_RESOLUTION_LINK_EXISTING,
    ENTITY_RESOLUTION_PENDING_REVIEW,
    ENTITY_RESOLUTION_REJECTED,
)
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.schemas.v03_graph import GraphEntityCreate, GraphRelationCreate
from app.services import graph_entities, graph_evidence, graph_relations
from app.services.canonical_entity_resolution import (
    MAX_EVIDENCE_REFS,
    EntityResolutionInput,
    resolve_canonical_entity,
)
from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.graph_candidate_routing import load_confidence_policy_v1
from app.services.graph_candidate_validation import load_ontology_rule_set_v1
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.graph_relation_fact_resolution import (
    finalize_resolved_graph_relation_fact,
    materialize_resolved_graph_relation_fact,
    preflight_graph_relation_candidate_fact,
)


class GraphExtractionMaterializationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphExtractionMaterializationResult:
    entity_count: int
    entity_mention_count: int
    relation_count: int
    relation_evidence_count: int
    already_materialized: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _extraction_key(value: dict[str, Any]) -> str:
    return canonical_graph_value_hash_v1(value)


def _entity_candidate_eligible(candidate: Any, threshold: float) -> bool:
    return (
        candidate.status == "validated"
        and candidate.purged_at is None
        and isinstance(candidate.final_confidence, (int, float))
        and not isinstance(candidate.final_confidence, bool)
        and candidate.final_confidence >= threshold
        and isinstance(candidate.canonical_name, str)
        and bool(candidate.canonical_name)
        and isinstance(candidate.normalized_name, str)
        and bool(candidate.normalized_name)
        and isinstance(candidate.proposed_properties, dict)
    )


def _relation_candidate_eligible(candidate: Any, threshold: float) -> bool:
    return (
        candidate.status == "validated"
        and candidate.purged_at is None
        and candidate.ontology_validation_status == "valid"
        and not candidate.has_conflict
        and isinstance(candidate.final_confidence, (int, float))
        and not isinstance(candidate.final_confidence, bool)
        and candidate.final_confidence >= threshold
        and isinstance(candidate.proposed_properties, dict)
    )


def _valid_evidence(rows: list[Any]) -> list[Any]:
    return [
        row
        for row in rows
        if row.purged_at is None
        and row.validation_status == "valid"
        and row.resolved_evidence_id is not None
        and row.resolved_document_id is not None
        and row.resolved_document_revision_id is not None
        and row.resolved_chunk_id is not None
        and row.resolved_source_span is not None
    ]


def _append_candidate_error(candidate: Any, code: str) -> None:
    errors = list(candidate.validation_errors or [])
    if not any(isinstance(item, dict) and item.get("code") == code for item in errors):
        errors.append({"code": code})
    candidate.validation_errors = errors


def _resolution_evidence_refs(rows: list[Any]) -> tuple[dict[str, str], ...]:
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.resolved_evidence_id),
            str(row.resolved_document_revision_id),
            str(row.resolved_chunk_id),
        ),
    )
    return tuple(
        {
            "chunk_id": str(row.resolved_chunk_id),
            "document_id": str(row.resolved_document_id),
            "document_revision_id": str(row.resolved_document_revision_id),
            "evidence_id": str(row.resolved_evidence_id),
        }
        for row in ordered[:MAX_EVIDENCE_REFS]
    )


def _resolution_existing_entity_id(candidate: GraphEntityCandidate, entity: Entity | None) -> uuid.UUID | None:
    if entity is None or candidate.normalization_method != "exact_normalized_match":
        return None
    return entity.id


def _mark_resolution_pending(candidate: GraphEntityCandidate, reason: str) -> None:
    candidate.status = "pending_review"
    candidate.review_reason = reason
    _append_candidate_error(candidate, reason)


def _mark_resolution_rejected(candidate: GraphEntityCandidate, reason: str) -> None:
    candidate.status = "rejected"
    candidate.review_reason = reason
    _append_candidate_error(candidate, reason)


def _apply_fact_resolution_lifecycle(candidate: GraphRelationCandidate, decision: Any) -> None:
    if decision.status == "pending":
        _mark_resolution_pending(candidate, decision.reason_code or "fact_resolution_pending")
    elif decision.status == "rejected":
        _mark_resolution_rejected(candidate, decision.reason_code or "fact_resolution_rejected")
    else:
        raise GraphExtractionMaterializationError(
            "fact_resolution_invalid",
            "unresolved Fact Resolution returned an invalid decision status",
        )


def _add_extracted_aliases(
    db,
    *,
    library: Library,
    candidate: GraphEntityCandidate,
    entity: Entity,
) -> None:
    aliases = getattr(candidate, "proposed_aliases", None)
    if not isinstance(aliases, list):
        return
    seen: set[str] = set()
    for value in aliases:
        if not isinstance(value, str):
            continue
        alias = value.strip()
        normalized = normalize_graph_name_v1(alias)
        if not normalized or normalized == entity.normalized_name or normalized in seen:
            continue
        seen.add(normalized)
        db.add(
            EntityAlias(
                library_id=library.id,
                entity_id=entity.id,
                alias=alias,
                normalized_alias=normalized,
                source_type="extracted",
                confidence=candidate.final_confidence,
                status="active",
            )
        )


async def _load_materialization_scope(db, *, job_id: uuid.UUID):
    job = await db.get(GraphExtractionJob, job_id, with_for_update=True)
    if job is None:
        raise GraphExtractionMaterializationError(
            "job_not_found",
            "graph extraction Job was not found",
        )
    materialization_statistics = (job.statistics or {}).get("materialization")
    if job.status == "succeeded" or (
        job.status == "partially_succeeded" and isinstance(materialization_statistics, dict)
    ):
        return job, None, None, None, None
    if (
        job.execution_mode != "production"
        or job.trigger_type == "eval"
        or (
            (job.status, job.current_stage)
            not in {
                ("processing", "materializing"),
                ("partially_succeeded", "finalizing"),
            }
        )
    ):
        raise GraphExtractionMaterializationError(
            "job_not_materializable",
            "graph extraction Job is not ready for production materialization",
        )
    library = await db.get(Library, job.library_id, with_for_update=True)
    document = await db.get(Document, job.document_id)
    revision = await db.get(DocumentRevision, job.document_revision_id)
    ontology = await db.get(OntologyVersion, job.ontology_version_id)
    if not settings.graph_extraction_enabled:
        raise GraphExtractionMaterializationError(
            "graph_extraction_disabled",
            "graph extraction is disabled globally",
        )
    if (
        library is None
        or getattr(library, "deleted_at", None) is not None
        or not library.graph_extraction_enabled
        or not library.external_llm_enabled
        or not isinstance(library.graph_extraction_allowed_security_levels, list)
        or not library.graph_extraction_allowed_security_levels
    ):
        raise GraphExtractionMaterializationError(
            "library_opt_out",
            "Library no longer permits graph extraction materialization",
        )
    if (
        revision is None
        or revision.library_id != library.id
        or revision.document_id != document.id
        or revision.status != "ready"
        or not isinstance(revision.security_level, str)
        or revision.security_level.strip() not in library.graph_extraction_allowed_security_levels
    ):
        raise GraphExtractionMaterializationError(
            "revision_not_authorized",
            "current revision is not ready or no longer authorized",
        )
    if (
        document is None
        or document.library_id != library.id
        or document.deleted_at is not None
        or document.current_revision_id != revision.id
        or document.status != "ready"
    ):
        raise GraphExtractionMaterializationError(
            "document_not_current",
            "document is deleted or extraction revision is no longer current",
        )
    snapshot = job.ontology_snapshot or {}
    explicit_ai_draft = bool(
        job.schema_discovery_run_id is not None
        and snapshot.get("schema_state") == "ai_draft"
        and snapshot.get("confirmed") is False
        and snapshot.get("ontology_version_id") == str(job.ontology_version_id)
    )
    if ontology is None or ontology.library_id != library.id or (
        ontology.status != "active" and not (explicit_ai_draft and ontology.status == "draft")
    ):
        raise GraphExtractionMaterializationError(
            "active_ontology_changed",
            "frozen ontology is no longer active or explicitly bound as an AI draft",
        )
    pending_result = await db.execute(
        select(func.count(GraphExtractionUnit.id)).where(
            GraphExtractionUnit.job_id == job.id,
            GraphExtractionUnit.status.in_(("queued", "processing")),
        )
    )
    if int(pending_result.scalar_one()) != 0:
        raise GraphExtractionMaterializationError(
            "units_not_succeeded",
            "all graph extraction Units must be terminal before materialization",
        )
    succeeded_result = await db.execute(
        select(func.count(GraphExtractionUnit.id)).where(
            GraphExtractionUnit.job_id == job.id,
            GraphExtractionUnit.status == "succeeded",
        )
    )
    if int(succeeded_result.scalar_one()) == 0:
        raise GraphExtractionMaterializationError(
            "units_not_succeeded",
            "at least one graph extraction Unit must succeed before materialization",
        )
    return job, library, document, revision, ontology


async def _eligible_matched_entity(
    db,
    *,
    candidate: GraphEntityCandidate,
    job: GraphExtractionJob,
    expected_entity_type_id: uuid.UUID,
) -> Entity | None:
    if candidate.matched_entity_id is not None:
        entity = await db.get(Entity, candidate.matched_entity_id)
    else:
        existing_result = await db.execute(
            select(Entity).where(
                Entity.library_id == job.library_id,
                Entity.ontology_version_id == job.ontology_version_id,
                Entity.entity_type_id == expected_entity_type_id,
                Entity.normalized_name == candidate.normalized_name,
            )
        )
        entity = existing_result.scalars().one_or_none()
        if entity is None:
            return None
    if (
        entity is None
        or entity.library_id != job.library_id
        or entity.ontology_version_id != job.ontology_version_id
        or entity.entity_type_id != expected_entity_type_id
        or entity.status not in {"draft", "active"}
    ):
        raise GraphExtractionMaterializationError(
            "matched_entity_ineligible",
            "matched Entity is missing, out of scope, or no longer eligible",
        )
    return entity


async def _entity_mention_for_evidence(
    db,
    *,
    job: GraphExtractionJob,
    library: Library,
    candidate: GraphEntityCandidate,
    entity: Entity,
    evidence: GraphEntityCandidateEvidence,
) -> tuple[EntityMention, bool]:
    extraction_key = _extraction_key(
        {
            "kind": "entity_mention_v1",
            "library_id": str(library.id),
            "entity_id": str(entity.id),
            "evidence_id": str(evidence.resolved_evidence_id),
            "source_span": evidence.resolved_source_span,
            "mention_text": candidate.canonical_name,
        }
    )
    existing_result = await db.execute(
        select(EntityMention).where(EntityMention.extraction_key == extraction_key)
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        if (
            existing.library_id != library.id
            or existing.entity_id != entity.id
            or existing.evidence_id != evidence.resolved_evidence_id
        ):
            raise GraphExtractionMaterializationError(
                "entity_mention_replay_mismatch",
                "existing Entity Mention does not match its extraction key",
            )
        return existing, False
    mention = await graph_evidence.create_entity_mention(
        db,
        library,
        entity_id=entity.id,
        evidence_id=evidence.resolved_evidence_id,
        mention_text=candidate.canonical_name,
        normalized_text=candidate.normalized_name,
        chunk_id=evidence.resolved_chunk_id,
        source_span=evidence.resolved_source_span,
        confidence=candidate.final_confidence,
        source_type="extracted",
        status="active",
    )
    mention.created_by_job_id = job.id
    mention.extraction_key = extraction_key
    return mention, True


async def _draft_relation(
    db,
    *,
    job: GraphExtractionJob,
    library: Library,
    candidate: GraphRelationCandidate,
    relation_type_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
) -> tuple[KnowledgeRelation | None, bool]:
    extraction_key = _extraction_key(
        {
            "kind": "knowledge_relation_v1",
            "library_id": str(library.id),
            "ontology_version_id": str(job.ontology_version_id),
            "relation_type_id": str(relation_type_id),
            "source_entity_id": str(source_entity_id),
            "target_entity_id": str(target_entity_id),
            "properties": candidate.proposed_properties,
        }
    )
    existing_result = await db.execute(
        select(KnowledgeRelation).where(KnowledgeRelation.extraction_key == extraction_key)
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        if (
            existing.library_id != library.id
            or existing.ontology_version_id != job.ontology_version_id
            or existing.relation_type_id != relation_type_id
            or existing.source_entity_id != source_entity_id
            or existing.target_entity_id != target_entity_id
        ):
            raise GraphExtractionMaterializationError(
                "relation_replay_mismatch",
                "existing Knowledge Relation does not match its extraction key",
            )
        if existing.status == "stale":
            existing.status = "draft"
        elif existing.status not in {"draft", "pending_review", "active"}:
            return None, False
        return existing, False
    relation = await graph_relations.create_relation(
        db,
        library,
        GraphRelationCreate(
            relation_type_id=relation_type_id,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            properties=candidate.proposed_properties,
            status="draft",
            source_type="extracted",
            confidence=candidate.final_confidence,
            schema_boundary_clear=True,
        ),
        allow_draft_ontology=True,
    )
    relation.created_by_job_id = job.id
    relation.extraction_key = extraction_key
    return relation, True


async def _relation_evidence_for_candidate(
    db,
    *,
    job: GraphExtractionJob,
    library: Library,
    relation: KnowledgeRelation,
    candidate: GraphRelationCandidate,
    evidence: GraphRelationCandidateEvidence,
) -> tuple[RelationEvidence, bool]:
    existing_result = await db.execute(
        select(RelationEvidence).where(
            RelationEvidence.library_id == library.id,
            RelationEvidence.relation_id == relation.id,
            RelationEvidence.evidence_id == evidence.resolved_evidence_id,
        )
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        return existing, False
    row = await graph_evidence.create_relation_evidence(
        db,
        library,
        relation_id=relation.id,
        evidence_id=evidence.resolved_evidence_id,
        support_type="supports",
        chunk_id=evidence.resolved_chunk_id,
        source_span=evidence.resolved_source_span,
        confidence=candidate.final_confidence,
        status="active",
    )
    row.created_by_job_id = job.id
    return row, True


async def _materialize_job_transaction(
    db,
    *,
    job_id: uuid.UUID,
) -> GraphExtractionMaterializationResult:
    job, library, _document, _revision, _ontology = await _load_materialization_scope(
        db,
        job_id=job_id,
    )
    if library is None:
        return GraphExtractionMaterializationResult(0, 0, 0, 0, True)
    rules = load_ontology_rule_set_v1(job)
    policy = load_confidence_policy_v1(job)

    entity_result = await db.execute(
        select(GraphEntityCandidate)
        .where(
            GraphEntityCandidate.job_id == job.id,
            GraphEntityCandidate.status.in_(("validated", "materialized")),
            GraphEntityCandidate.purged_at.is_(None),
        )
        .order_by(GraphEntityCandidate.candidate_key)
        .with_for_update()
    )
    entity_candidates = list(entity_result.scalars().all())
    relation_result = await db.execute(
        select(GraphRelationCandidate)
        .where(
            GraphRelationCandidate.job_id == job.id,
            GraphRelationCandidate.status.in_(("validated", "materialized")),
            GraphRelationCandidate.purged_at.is_(None),
        )
        .order_by(GraphRelationCandidate.candidate_key)
        .with_for_update()
    )
    relation_candidates = list(relation_result.scalars().all())
    entity_evidence_result = await db.execute(
        select(GraphEntityCandidateEvidence).where(
            GraphEntityCandidateEvidence.job_id == job.id,
            GraphEntityCandidateEvidence.purged_at.is_(None),
        )
    )
    relation_evidence_result = await db.execute(
        select(GraphRelationCandidateEvidence).where(
            GraphRelationCandidateEvidence.job_id == job.id,
            GraphRelationCandidateEvidence.purged_at.is_(None),
        )
    )
    entity_evidence_by_candidate: dict[uuid.UUID, list[Any]] = {}
    for row in entity_evidence_result.scalars().all():
        entity_evidence_by_candidate.setdefault(row.candidate_id, []).append(row)
    relation_evidence_by_candidate: dict[uuid.UUID, list[Any]] = {}
    for row in relation_evidence_result.scalars().all():
        relation_evidence_by_candidate.setdefault(row.candidate_id, []).append(row)

    entity_count = 0
    mention_count = 0
    relation_count = 0
    relation_evidence_count = 0
    materialized_entity_candidate_count = 0
    publishable_relation_count = 0
    publishable_relation_evidence_count = 0
    pending_entity_candidate_count = 0
    entity_by_candidate_id: dict[uuid.UUID, Entity] = {}
    for candidate in entity_candidates:
        type_rule = rules.entity_types_by_key.get(candidate.entity_type_key)
        if type_rule is None:
            raise GraphExtractionMaterializationError(
                "entity_type_missing",
                "validated Entity Candidate references an unknown frozen Entity Type",
            )
        if candidate.status == "materialized":
            entity = await db.get(Entity, candidate.materialized_entity_id)
            if entity is not None:
                entity_by_candidate_id[candidate.id] = entity
            continue
        evidence_rows = _valid_evidence(entity_evidence_by_candidate.get(candidate.id, []))
        if (
            not _entity_candidate_eligible(
                candidate,
                policy.entity_materialization_threshold,
            )
            or not evidence_rows
        ):
            continue
        matched = await _eligible_matched_entity(
            db,
            candidate=candidate,
            job=job,
            expected_entity_type_id=type_rule.id,
        )
        resolution = await resolve_canonical_entity(
            db,
            EntityResolutionInput(
                library_id=library.id,
                observed_name=candidate.canonical_name,
                observed_normalized_name=candidate.normalized_name,
                observed_entity_type_id=type_rule.id,
                observed_entity_type_key=candidate.entity_type_key,
                observed_ontology_version_id=job.ontology_version_id,
                existing_entity_id=_resolution_existing_entity_id(candidate, matched),
                observed_properties=candidate.proposed_properties,
                context={
                    "candidate_key": candidate.candidate_key,
                    "job_id": str(job.id),
                    "ontology_version_id": str(job.ontology_version_id),
                },
                graph_entity_candidate_id=candidate.id,
                source_fingerprint=f"graph_entity_candidate:{candidate.id}",
                evidence_refs=_resolution_evidence_refs(evidence_rows),
            ),
        )
        decision = resolution.decision
        if decision.decision_kind == ENTITY_RESOLUTION_PENDING_REVIEW:
            _mark_resolution_pending(candidate, decision.reason_code or "canonical_resolution_pending")
            pending_entity_candidate_count += 1
            continue
        if decision.decision_kind == ENTITY_RESOLUTION_REJECTED:
            _mark_resolution_rejected(candidate, decision.reason_code or "canonical_resolution_rejected")
            continue
        if (
            decision.decision_kind not in {
                ENTITY_RESOLUTION_LINK_EXISTING,
                ENTITY_RESOLUTION_CREATE_NEW,
            }
            or resolution.canonical_entity is None
        ):
            raise GraphExtractionMaterializationError(
                "canonical_resolution_invalid",
                "Canonical resolution returned an invalid materialization result",
            )
        entity = matched
        if (
            entity is not None
            and entity.canonical_entity_id is not None
            and entity.canonical_entity_id != resolution.canonical_entity.id
        ):
            _mark_resolution_pending(candidate, "canonical_projection_conflict")
            pending_entity_candidate_count += 1
            continue
        if entity is None:
            entity = await graph_entities.create_entity(
                db,
                library,
                GraphEntityCreate(
                    ontology_version_id=job.ontology_version_id,
                    entity_type_id=type_rule.id,
                    canonical_name=candidate.canonical_name,
                    properties=candidate.proposed_properties,
                    status="draft",
                    source_type="extracted",
                    confidence=candidate.final_confidence,
                ),
                allow_draft_ontology=True,
            )
            entity.created_by_job_id = job.id
            _add_extracted_aliases(
                db,
                library=library,
                candidate=candidate,
                entity=entity,
            )
            entity_count += 1
        entity.canonical_entity_id = resolution.canonical_entity.id
        entity_by_candidate_id[candidate.id] = entity
        for evidence in evidence_rows:
            _mention, created = await _entity_mention_for_evidence(
                db,
                job=job,
                library=library,
                candidate=candidate,
                entity=entity,
                evidence=evidence,
            )
            mention_count += int(created)
        decision.entity_id = entity.id
        candidate.materialized_entity_id = entity.id
        candidate.status = "materialized"
        materialized_entity_candidate_count += 1

    for candidate in relation_candidates:
        if candidate.status == "materialized":
            continue
        relation_evidence_rows = _valid_evidence(relation_evidence_by_candidate.get(candidate.id, []))
        if not relation_evidence_rows:
            continue
        source = entity_by_candidate_id.get(candidate.source_candidate_id)
        target = entity_by_candidate_id.get(candidate.target_candidate_id)
        if source is None or target is None:
            continue
        if not _relation_candidate_eligible(
            candidate,
            policy.relation_draft_threshold,
        ):
            continue
        relation_type = rules.relation_types_by_key.get(candidate.relation_type_key)
        if relation_type is None:
            raise GraphExtractionMaterializationError(
                "relation_type_missing",
                "validated Relation Candidate references an unknown frozen Relation Type",
            )
        preflight = await preflight_graph_relation_candidate_fact(
            db,
            library_id=library.id,
            candidate=candidate,
            relation_type_id=relation_type.id,
            source_entity=source,
            target_entity=target,
            evidence_rows=relation_evidence_rows,
        )
        if not preflight.is_resolved:
            assert preflight.decision is not None
            _apply_fact_resolution_lifecycle(candidate, preflight.decision)
            continue
        relation, created = await _draft_relation(
            db,
            job=job,
            library=library,
            candidate=candidate,
            relation_type_id=relation_type.id,
            source_entity_id=source.id,
            target_entity_id=target.id,
        )
        if relation is None:
            continue
        relation.review_status = "not_required"
        fact_materialization = await materialize_resolved_graph_relation_fact(
            db,
            library_id=library.id,
            candidate=candidate,
            relation=relation,
            source_entity=source,
            preflight=preflight,
        )
        for evidence in relation_evidence_rows:
            row, evidence_created = await _relation_evidence_for_candidate(
                db,
                job=job,
                library=library,
                relation=relation,
                candidate=candidate,
                evidence=evidence,
            )
            await finalize_resolved_graph_relation_fact(
                db,
                library_id=library.id,
                candidate=candidate,
                relation=relation,
                relation_evidence=row,
                preflight=preflight,
                materialization=fact_materialization,
            )
            relation_evidence_count += int(evidence_created)
        relation_count += int(created)
        publishable_relation_evidence_count += len(relation_evidence_rows)
        candidate.materialized_relation_id = relation.id
        candidate.status = "materialized"
        publishable_relation_count += int(created)

    counts = getattr(job, "counts", None)
    counts = counts if isinstance(counts, dict) else {}
    partially_succeeded = job.status == "partially_succeeded" or bool(
        counts.get("failed", 0) or counts.get("cancelled", 0)
    )
    job.status = "partially_succeeded" if partially_succeeded else "succeeded"
    job.current_stage = "finalizing"
    job.error_code = "unit_failures" if partially_succeeded else None
    job.error_message = None
    job.finished_at = _utcnow()
    statistics = dict(job.statistics or {})
    statistics["materialization"] = {
        "outcome": "materialized",
        "entity_count": entity_count,
        "entity_mention_count": mention_count,
        "relation_count": relation_count,
        "relation_evidence_count": relation_evidence_count,
        "publishable_entity_candidate_count": materialized_entity_candidate_count,
        "publishable_relation_count": publishable_relation_count,
        "publishable_relation_evidence_count": publishable_relation_evidence_count,
        "pending_entity_candidate_count": pending_entity_candidate_count,
        "failure_reasons": {},
    }
    job.statistics = statistics
    await db.flush()
    return GraphExtractionMaterializationResult(
        entity_count,
        mention_count,
        relation_count,
        relation_evidence_count,
    )


async def _mark_materialization_failed(session_factory, *, job_id: uuid.UUID) -> None:
    async with session_factory() as db:
        async with db.begin():
            job = await db.get(GraphExtractionJob, job_id, with_for_update=True)
            if job is None or job.status == "succeeded":
                return
            job.status = "failed"
            job.current_stage = "finalizing"
            job.error_code = "materialization_failed"
            job.error_message = None
            job.finished_at = _utcnow()
            await db.flush()


async def materialize_graph_extraction_job(
    session_factory,
    *,
    job_id: uuid.UUID,
) -> GraphExtractionMaterializationResult:
    try:
        async with session_factory() as db:
            async with db.begin():
                return await _materialize_job_transaction(db, job_id=job_id)
    except GraphExtractionMaterializationError as exc:
        if exc.code != "job_not_materializable":
            await _mark_materialization_failed(session_factory, job_id=job_id)
        raise
    except Exception as exc:
        await _mark_materialization_failed(session_factory, job_id=job_id)
        raise GraphExtractionMaterializationError(
            "materialization_failed",
            "graph extraction materialization failed",
        ) from exc
