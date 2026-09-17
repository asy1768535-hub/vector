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
from app.models.fact_foundation import FactAssertion, LogicalFact
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
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.models.raw_claim_projection_binding import GraphRawClaimProjectionBinding
from app.models.relation_evidence import RelationEvidence
from app.schemas.v03_graph import GraphEntityCreate, GraphRelationCreate
from app.services import graph_entities, graph_evidence, graph_relations
from app.services.canonical_entity_evolution import (
    CanonicalEvolutionContext,
    CanonicalReassignCommand,
    apply_canonical_evolution,
    build_evolution_precondition_fingerprint,
    resolve_current_canonical_identity,
)
from app.services.canonical_entity_resolution import (
    MAX_EVIDENCE_REFS,
    EntityResolutionInput,
    resolve_canonical_entity,
)
from app.services.fact_lifecycle import reconcile_resolved_fact_decision
from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.graph_candidate_routing import load_confidence_policy_v1
from app.services.graph_candidate_validation import load_ontology_rule_set_v1
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.graph_relation_fact_resolution import (
    finalize_resolved_graph_relation_fact,
    materialize_resolved_graph_relation_fact,
    preflight_graph_relation_candidate_fact,
)
from app.services.raw_claim_fact_resolution import (
    record_raw_claim_projection_pending_fact,
    resolve_raw_claim_fact,
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


async def _load_materialization_scope(
    db,
    *,
    job_id: uuid.UUID,
    allow_succeeded_reprocess: bool = False,
):
    job = await db.get(GraphExtractionJob, job_id, with_for_update=True)
    if job is None:
        raise GraphExtractionMaterializationError(
            "job_not_found",
            "graph extraction Job was not found",
        )
    materialization_statistics = (job.statistics or {}).get("materialization")
    if (not allow_succeeded_reprocess and job.status == "succeeded") or (
        job.status == "partially_succeeded" and isinstance(materialization_statistics, dict)
    ):
        return job, None, None, None, None
    if (
        job.execution_mode != "production"
        or job.trigger_type == "eval"
        or (not allow_succeeded_reprocess and (
            (job.status, job.current_stage)
            not in {
                ("processing", "materializing"),
                ("partially_succeeded", "finalizing"),
            }
        ))
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


def _raw_claim_matches_candidate_evidence(raw_claim: Any, evidence: Any) -> bool:
    refs = getattr(raw_claim, "evidence_refs", None)
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
        return False
    reference = refs[0]
    span = reference.get("source_span")
    return (
        reference.get("evidence_id") == str(evidence.resolved_evidence_id)
        and reference.get("document_id") == str(evidence.resolved_document_id)
        and reference.get("document_revision_id") == str(evidence.resolved_document_revision_id)
        and reference.get("chunk_id") == str(evidence.resolved_chunk_id)
        and isinstance(span, dict)
        and span.get("start") == evidence.resolved_source_span.get("start")
        and span.get("end") == evidence.resolved_source_span.get("end")
    )


async def _materialize_raw_claim_facts(
    db,
    *,
    job: GraphExtractionJob,
    library: Library,
    relation_candidates: dict[uuid.UUID, GraphRelationCandidate],
    relations: dict[uuid.UUID, KnowledgeRelation],
    entities: dict[uuid.UUID, Entity],
    relation_evidence_by_candidate: dict[uuid.UUID, list[Any]],
) -> None:
    """Consume only uniquely valid projection bindings after candidate materialization."""
    bindings = list(
        (
            await db.execute(
                select(GraphRawClaimProjectionBinding)
                .join(
                    GraphRawClaimOccurrence,
                    GraphRawClaimOccurrence.extraction_occurrence_id
                    == GraphRawClaimProjectionBinding.raw_claim_occurrence_id,
                )
                .where(GraphRawClaimOccurrence.job_id == job.id)
            )
        ).scalars().all()
    )
    bindings_by_claim: dict[uuid.UUID, list[GraphRawClaimProjectionBinding]] = {}
    for binding in bindings:
        bindings_by_claim.setdefault(binding.raw_claim_id, []).append(binding)

    for raw_claim_id, claim_bindings in bindings_by_claim.items():
        raw_claim = await db.get(GraphRawClaim, raw_claim_id)
        if raw_claim is None or (
            raw_claim.library_id != library.id
            or raw_claim.document_id != job.document_id
            or raw_claim.document_revision_id != job.document_revision_id
        ):
            continue
        valid: list[tuple[GraphRawClaimProjectionBinding, Any, Any, Any, Any, Any]] = []
        for binding in claim_bindings:
            occurrence = (
                await db.execute(
                    select(GraphRawClaimOccurrence).where(
                        GraphRawClaimOccurrence.extraction_occurrence_id
                        == binding.raw_claim_occurrence_id
                    )
                )
            ).scalars().one_or_none()
            candidate = relation_candidates.get(binding.graph_relation_candidate_id)
            relation = relations.get(binding.graph_relation_candidate_id)
            if (
                occurrence is None
                or occurrence.claim_id != raw_claim.id
                or occurrence.job_id != job.id
                or candidate is None
                or candidate.job_id != job.id
                or candidate.library_id != library.id
                or candidate.ontology_version_id != job.ontology_version_id
                or candidate.purged_at is not None
                or candidate.status != "materialized"
                or relation is None
                or relation.library_id != library.id
                or relation.ontology_version_id != job.ontology_version_id
            ):
                continue
            source = entities.get(candidate.source_candidate_id)
            target = entities.get(candidate.target_candidate_id)
            evidence_rows = _valid_evidence(relation_evidence_by_candidate.get(candidate.id, []))
            if (
                source is None
                or target is None
                or source.canonical_entity_id is None
                or target.canonical_entity_id is None
                or len(evidence_rows) != 1
            ):
                continue
            evidence = evidence_rows[0]
            if not _raw_claim_matches_candidate_evidence(raw_claim, evidence):
                continue
            valid.append((binding, candidate, relation, source, target, evidence))

        valid_candidate_ids = {binding.graph_relation_candidate_id for binding, *_rest in valid}
        if len(valid_candidate_ids) > 1:
            projection_contexts = [
                {
                    "proposed_properties": candidate.proposed_properties,
                    "relation_type_key": candidate.relation_type_key,
                    "source_canonical_entity_id": str(source.canonical_entity_id),
                    "target_canonical_entity_id": str(target.canonical_entity_id),
                }
                for _binding, candidate, _relation, source, target, _evidence in valid
            ]
            await record_raw_claim_projection_pending_fact(
                db,
                library_id=library.id,
                raw_claim=raw_claim,
                evidence=valid[0][-1],
                reason_code="raw_claim_projection_ambiguity",
                projection_contexts=projection_contexts,
            )
            continue
        if len(valid_candidate_ids) != 1:
            continue

        _binding, candidate, relation, source, target, evidence = valid[0]
        expected_fact = (
            await db.get(LogicalFact, relation.logical_fact_id)
            if relation.logical_fact_id is not None
            else None
        )
        if expected_fact is None:
            continue
        relation_evidence, _created = await _relation_evidence_for_candidate(
            db,
            job=job,
            library=library,
            relation=relation,
            candidate=candidate,
            evidence=evidence,
        )
        expected_assertion = (
            await db.get(FactAssertion, relation_evidence.fact_assertion_id)
            if relation_evidence.fact_assertion_id is not None
            else None
        )
        if expected_assertion is None:
            continue
        result = await resolve_raw_claim_fact(
            db,
            library_id=library.id,
            raw_claim=raw_claim,
            projection_candidate=candidate,
            relation=relation,
            relation_evidence=relation_evidence,
            source_entity=source,
            target_entity=target,
            evidence=evidence,
            expected_logical_fact=expected_fact,
            expected_assertion=expected_assertion,
        )
        if result.decision.status == "resolved":
            await reconcile_resolved_fact_decision(
                db,
                library_id=library.id,
                decision=result.decision,
            )


async def _materialize_job_transaction(
    db,
    *,
    job_id: uuid.UUID,
    allow_succeeded_reprocess: bool = False,
) -> GraphExtractionMaterializationResult:
    job, library, _document, _revision, _ontology = await _load_materialization_scope(
        db,
        job_id=job_id,
        allow_succeeded_reprocess=allow_succeeded_reprocess,
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
    relation_by_candidate_id: dict[uuid.UUID, KnowledgeRelation] = {}
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
        if (
            matched is not None
            and matched.canonical_entity_id is not None
            and hasattr(matched, "library_id")
            and hasattr(db, "sync_session")
        ):
            current = await resolve_current_canonical_identity(
                db,
                library.id,
                matched.canonical_entity_id,
                CanonicalEvolutionContext(entity_id=matched.id),
            )
            if (
                current.status != "resolved"
                or not current.resolution_eligible
                or current.current_canonical_entity_id is None
            ):
                _mark_resolution_pending(candidate, f"canonical_current_{current.status}")
                pending_entity_candidate_count += 1
                continue
            if current.current_canonical_entity_id != matched.canonical_entity_id:
                if not isinstance(job.requested_by, uuid.UUID):
                    _mark_resolution_pending(candidate, "canonical_evolution_actor_missing")
                    pending_entity_candidate_count += 1
                    continue
                reassignment = CanonicalReassignCommand(
                    library_id=library.id,
                    entity_id=matched.id,
                    from_canonical_entity_id=matched.canonical_entity_id,
                    target_canonical_entity_id=current.current_canonical_entity_id,
                    idempotency_key=(
                        f"canonical-evolution-reassign:{matched.id}:"
                        f"{matched.canonical_entity_id}:{current.current_canonical_entity_id}"
                    ),
                    reason_code="materializer_current_canonical",
                    reason_text="current canonical projection requires persisted reassignment",
                    method="canonical_evolution_v1",
                    evidence_refs=tuple(_resolution_evidence_refs(evidence_rows)),
                    actor_type="user",
                    actor_id=job.requested_by,
                    request_id=f"graph-extraction-job:{job.id}:candidate:{candidate.id}",
                )
                reassignment = reassignment.with_expected_precondition(
                    await build_evolution_precondition_fingerprint(db, reassignment)
                )
                result = await apply_canonical_evolution(db, reassignment)
                replayed_applied = (
                    result.status == "REUSED"
                    and result.effective_outcome == "APPLIED"
                    and result.current_decision_status == "applied"
                    and result.reused_decision_id == result.current_decision_id
                )
                if result.status != "APPLIED" and not replayed_applied:
                    _mark_resolution_pending(
                        candidate,
                        f"canonical_projection_{result.status.lower()}",
                    )
                    pending_entity_candidate_count += 1
                    continue
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
            relation = await db.get(KnowledgeRelation, candidate.materialized_relation_id)
            if (
                relation is not None
                and relation.library_id == library.id
                and relation.ontology_version_id == job.ontology_version_id
            ):
                relation_by_candidate_id[candidate.id] = relation
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
        fact_resolution = None
        for evidence in relation_evidence_rows:
            row, evidence_created = await _relation_evidence_for_candidate(
                db,
                job=job,
                library=library,
                relation=relation,
                candidate=candidate,
                evidence=evidence,
            )
            fact_resolution = await finalize_resolved_graph_relation_fact(
                db,
                library_id=library.id,
                candidate=candidate,
                relation=relation,
                relation_evidence=row,
                preflight=preflight,
                materialization=fact_materialization,
            )
            relation_evidence_count += int(evidence_created)
        if fact_resolution is not None:
            await reconcile_resolved_fact_decision(
                db,
                library_id=library.id,
                decision=fact_resolution.decision,
            )
        relation_count += int(created)
        publishable_relation_evidence_count += len(relation_evidence_rows)
        candidate.materialized_relation_id = relation.id
        candidate.status = "materialized"
        relation_by_candidate_id[candidate.id] = relation
        publishable_relation_count += int(created)

    await _materialize_raw_claim_facts(
        db,
        job=job,
        library=library,
        relation_candidates={candidate.id: candidate for candidate in relation_candidates},
        relations=relation_by_candidate_id,
        entities=entity_by_candidate_id,
        relation_evidence_by_candidate=relation_evidence_by_candidate,
    )

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
    allow_succeeded_reprocess: bool = False,
) -> GraphExtractionMaterializationResult:
    try:
        async with session_factory() as db:
            async with db.begin():
                return await _materialize_job_transaction(
                    db,
                    job_id=job_id,
                    allow_succeeded_reprocess=allow_succeeded_reprocess,
                )
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
