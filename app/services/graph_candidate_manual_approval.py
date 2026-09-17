"""Management-gated approval for safe, evidence-bound graph candidate repair."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.models.graph_candidate_evidence import GraphEntityCandidateEvidence
from app.models.graph_candidates import GraphEntityCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.library import Library
from app.models.user import User
from app.services import audit_log
from app.services.canonical_entity_resolution import (
    EntityResolutionInput,
    resolve_canonical_entity,
)
from app.services.graph_candidate_validation import load_ontology_rule_set_v1
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_management,
)

_MANUAL_CREATE_REASON = "cross_ontology_weak_identity"


class GraphCandidateManualApprovalError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GraphCandidateManualApprovalResult:
    job_id: uuid.UUID
    approved_candidate_ids: tuple[uuid.UUID, ...]


def _valid_evidence(rows: list[GraphEntityCandidateEvidence]) -> list[GraphEntityCandidateEvidence]:
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


def _evidence_refs(rows: list[GraphEntityCandidateEvidence]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "chunk_id": str(row.resolved_chunk_id),
            "document_id": str(row.resolved_document_id),
            "document_revision_id": str(row.resolved_document_revision_id),
            "evidence_id": str(row.resolved_evidence_id),
        }
        for row in sorted(
            rows,
            key=lambda row: (
                str(row.resolved_evidence_id),
                str(row.resolved_document_revision_id),
                str(row.resolved_chunk_id),
            ),
        )[:16]
    )


def _remove_review_reason(candidate: GraphEntityCandidate) -> None:
    candidate.review_reason = None
    candidate.validation_errors = [
        item
        for item in (candidate.validation_errors or [])
        if not (isinstance(item, dict) and item.get("code") == _MANUAL_CREATE_REASON)
    ]


async def _require_management(db, *, library: Library, actor_user_id: uuid.UUID) -> User:
    user = await db.get(User, actor_user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        raise GraphCandidateManualApprovalError("graph_candidate_approval_forbidden")
    try:
        await resolve_loaded_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise GraphCandidateManualApprovalError("graph_candidate_approval_forbidden") from exc
    return user


async def approve_pending_graph_entity_candidates_as_new(
    db,
    *,
    library: Library,
    job_id: uuid.UUID,
    candidate_ids: tuple[uuid.UUID, ...],
    actor_user_id: uuid.UUID,
) -> GraphCandidateManualApprovalResult:
    """Approve only pending cross-ontology candidates as distinct identities.

    The caller owns the transaction.  This does not materialize or publish by
    itself; those existing, evidence-aware stages run only after approval is
    committed.
    """

    if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
        raise GraphCandidateManualApprovalError("invalid_candidate_ids")
    await _require_management(db, library=library, actor_user_id=actor_user_id)
    job = (
        await db.execute(
            select(GraphExtractionJob)
            .where(
                GraphExtractionJob.id == job_id,
                GraphExtractionJob.library_id == library.id,
            )
            .with_for_update()
        )
    ).scalars().first()
    if job is None:
        raise GraphCandidateManualApprovalError("graph_extraction_job_not_found")
    if job.status not in {"succeeded", "partially_succeeded"}:
        raise GraphCandidateManualApprovalError("graph_extraction_job_not_ready")

    rows = list(
        (
            await db.execute(
                select(GraphEntityCandidate)
                .where(
                    GraphEntityCandidate.id.in_(candidate_ids),
                    GraphEntityCandidate.job_id == job.id,
                    GraphEntityCandidate.library_id == library.id,
                    GraphEntityCandidate.purged_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(rows) != len(candidate_ids):
        raise GraphCandidateManualApprovalError("graph_candidate_scope_mismatch")
    by_id = {row.id: row for row in rows}
    candidates = [by_id[candidate_id] for candidate_id in candidate_ids]
    if any(
        row.status != "pending_review" or row.review_reason != _MANUAL_CREATE_REASON
        for row in candidates
    ):
        raise GraphCandidateManualApprovalError("graph_candidate_not_pending_cross_ontology_review")

    rules = load_ontology_rule_set_v1(job)
    evidence_rows = list(
        (
            await db.execute(
                select(GraphEntityCandidateEvidence).where(
                    GraphEntityCandidateEvidence.job_id == job.id,
                    GraphEntityCandidateEvidence.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars().all()
    )
    evidence_by_candidate: dict[uuid.UUID, list[GraphEntityCandidateEvidence]] = {}
    for evidence in evidence_rows:
        evidence_by_candidate.setdefault(evidence.candidate_id, []).append(evidence)

    for candidate in candidates:
        type_rule = rules.entity_types_by_key.get(candidate.entity_type_key)
        if type_rule is None:
            raise GraphCandidateManualApprovalError("graph_candidate_entity_type_missing")
        evidence = _valid_evidence(evidence_by_candidate.get(candidate.id, []))
        if not evidence:
            raise GraphCandidateManualApprovalError("graph_candidate_evidence_missing")
        resolution = await resolve_canonical_entity(
            db,
            EntityResolutionInput(
                library_id=library.id,
                observed_name=candidate.canonical_name,
                observed_normalized_name=candidate.normalized_name,
                observed_entity_type_id=type_rule.id,
                observed_entity_type_key=candidate.entity_type_key,
                observed_ontology_version_id=job.ontology_version_id,
                observed_properties=candidate.proposed_properties,
                context={
                    "candidate_key": candidate.candidate_key,
                    "job_id": str(job.id),
                    "ontology_version_id": str(job.ontology_version_id),
                },
                graph_entity_candidate_id=candidate.id,
                source_fingerprint=f"graph_entity_candidate:{candidate.id}",
                evidence_refs=_evidence_refs(evidence),
                confirm_create_new_identity=True,
            ),
        )
        if resolution.decision.decision_kind != "create_new" or resolution.canonical_entity is None:
            raise GraphCandidateManualApprovalError("graph_candidate_approval_resolution_invalid")
        candidate.status = "validated"
        _remove_review_reason(candidate)

    await audit_log.record(
        db,
        actor_user_id,
        "graph_extraction.candidates_approved_as_new",
        {
            "job_id": str(job.id),
            "candidate_ids": [str(candidate.id) for candidate in candidates],
            "reason": _MANUAL_CREATE_REASON,
        },
    )
    await db.flush()
    return GraphCandidateManualApprovalResult(
        job_id=job.id,
        approved_candidate_ids=tuple(candidate.id for candidate in candidates),
    )


__all__ = [
    "GraphCandidateManualApprovalError",
    "GraphCandidateManualApprovalResult",
    "approve_pending_graph_entity_candidates_as_new",
]
