"""Explicit RawClaim-to-candidate anchors for the shadow extraction boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.graph_candidate_evidence import GraphRelationCandidateEvidence
from app.models.graph_candidates import GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.models.raw_claim_projection_binding import GraphRawClaimProjectionBinding
from app.schemas.shadow_extraction import AnchoredShadowClaimV1, ShadowProjectionContextV1


BINDING_CONTRACT_VERSION = "raw_claim_projection_binding_v1"
BINDING_METHOD = "explicit_shadow_projection_ref_v1"
_MAX_PROJECTION_CONTEXTS = 32


@dataclass(frozen=True, slots=True)
class RawClaimProjectionAnchor:
    projection_ref: str
    graph_relation_candidate_id: uuid.UUID
    allowed_evidence_ref_keys: frozenset[str]

    def context(self) -> ShadowProjectionContextV1:
        return ShadowProjectionContextV1(
            projection_ref=self.projection_ref,
            allowed_evidence_ref_keys=tuple(sorted(self.allowed_evidence_ref_keys)),
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def binding_fingerprint_v1(
    *,
    raw_claim_id: uuid.UUID,
    raw_claim_occurrence_id: uuid.UUID,
    graph_relation_candidate_id: uuid.UUID,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "binding_contract_version": BINDING_CONTRACT_VERSION,
                "binding_method": BINDING_METHOD,
                "graph_relation_candidate_id": str(graph_relation_candidate_id),
                "raw_claim_id": str(raw_claim_id),
                "raw_claim_occurrence_id": str(raw_claim_occurrence_id),
            }
        ).encode("utf-8")
    ).hexdigest()


def projection_anchor_for_shadow_claim(
    claim: AnchoredShadowClaimV1,
    *,
    anchors: Sequence[RawClaimProjectionAnchor],
) -> RawClaimProjectionAnchor | None:
    """Resolve only a server-issued opaque ref; invalid anchors remain unbound."""
    if claim.projection_ref is None:
        return None
    anchor = next((item for item in anchors if item.projection_ref == claim.projection_ref), None)
    if anchor is None:
        return None
    if not set(claim.claim.evidence_ref_keys).issubset(anchor.allowed_evidence_ref_keys):
        return None
    return anchor


def _candidate_is_eligible(
    candidate: Any,
    *,
    library_id: uuid.UUID,
    job: Any,
    occurrence: Any,
) -> bool:
    return (
        candidate.job_id == occurrence.job_id == job.id
        and candidate.library_id == library_id == job.library_id
        and candidate.ontology_version_id == job.ontology_version_id
        and candidate.purged_at is None
        and candidate.status not in {"rejected", "superseded"}
    )


def _span(value: Any) -> tuple[int, int] | None:
    if isinstance(value, Mapping):
        start = value.get("start")
        end = value.get("end")
    else:
        start = getattr(value, "start", None)
        end = getattr(value, "end", None)
    if isinstance(start, int) and isinstance(end, int):
        return start, end
    return None


def _evidence_matches_reference(evidence: Any, reference: Any) -> bool:
    return (
        evidence.resolved_evidence_id == reference.evidence_id
        and evidence.resolved_document_id == reference.document_id
        and evidence.resolved_document_revision_id == reference.document_revision_id
        and evidence.resolved_chunk_id == reference.chunk_id
        and evidence.resolved_block_id == reference.block_id
        and _span(evidence.resolved_source_span) == _span(reference.source_span)
    )


def _allowed_evidence_ref_keys(
    evidence_rows: Sequence[Any], evidence_by_ref: Mapping[str, Any]
) -> frozenset[str]:
    return frozenset(
        ref_key
        for ref_key, reference in evidence_by_ref.items()
        if any(_evidence_matches_reference(row, reference) for row in evidence_rows)
    )


async def load_shadow_projection_anchors(
    db: Any,
    *,
    job: Any,
    occurrence: Any,
    evidence_by_ref: Mapping[str, Any],
) -> tuple[RawClaimProjectionAnchor, ...]:
    """Return bounded opaque contexts backed by current candidate evidence lineage."""
    candidates = list(
        (
            await db.execute(
                select(GraphRelationCandidate)
                .where(
                    GraphRelationCandidate.job_id == job.id,
                    GraphRelationCandidate.library_id == job.library_id,
                    GraphRelationCandidate.ontology_version_id == job.ontology_version_id,
                    GraphRelationCandidate.purged_at.is_(None),
                    GraphRelationCandidate.status.notin_(("rejected", "superseded")),
                )
                .order_by(GraphRelationCandidate.candidate_key, GraphRelationCandidate.id)
            )
        ).scalars().all()
    )
    if not candidates:
        return ()
    candidate_ids = [candidate.id for candidate in candidates]
    evidence_rows = list(
        (
            await db.execute(
                select(GraphRelationCandidateEvidence).where(
                    GraphRelationCandidateEvidence.job_id == job.id,
                    GraphRelationCandidateEvidence.extraction_unit_id == occurrence.id,
                    GraphRelationCandidateEvidence.candidate_id.in_(candidate_ids),
                    GraphRelationCandidateEvidence.purged_at.is_(None),
                    GraphRelationCandidateEvidence.validation_status == "valid",
                )
            )
        ).scalars().all()
    )
    rows_by_candidate: dict[uuid.UUID, list[Any]] = {}
    for row in evidence_rows:
        rows_by_candidate.setdefault(row.candidate_id, []).append(row)

    anchors: list[RawClaimProjectionAnchor] = []
    for candidate in candidates:
        if not _candidate_is_eligible(
            candidate, library_id=job.library_id, job=job, occurrence=occurrence
        ):
            continue
        allowed = _allowed_evidence_ref_keys(rows_by_candidate.get(candidate.id, ()), evidence_by_ref)
        if not allowed:
            continue
        anchors.append(
            RawClaimProjectionAnchor(
                projection_ref=f"p{len(anchors)}",
                graph_relation_candidate_id=candidate.id,
                allowed_evidence_ref_keys=allowed,
            )
        )
        if len(anchors) == _MAX_PROJECTION_CONTEXTS:
            break
    return tuple(anchors)


async def _find_existing_binding(
    db: Any,
    *,
    raw_claim_occurrence_id: uuid.UUID,
    graph_relation_candidate_id: uuid.UUID,
) -> GraphRawClaimProjectionBinding | None:
    return (
        await db.execute(
            select(GraphRawClaimProjectionBinding).where(
                GraphRawClaimProjectionBinding.raw_claim_occurrence_id == raw_claim_occurrence_id,
                GraphRawClaimProjectionBinding.graph_relation_candidate_id == graph_relation_candidate_id,
            )
        )
    ).scalar_one_or_none()


async def create_or_get_raw_claim_projection_binding(
    db: Any,
    *,
    claim: GraphRawClaim,
    occurrence: GraphRawClaimOccurrence,
    source_claim: Any,
    anchor: RawClaimProjectionAnchor,
) -> GraphRawClaimProjectionBinding | None:
    """Persist one verified binding, or leave the immutable RawClaim unbound."""
    candidate = await db.get(GraphRelationCandidate, anchor.graph_relation_candidate_id)
    job = await db.get(GraphExtractionJob, occurrence.job_id)
    if candidate is None or job is None or not _candidate_is_eligible(
        candidate, library_id=claim.library_id, job=job, occurrence=occurrence
    ):
        return None
    source_ref_keys = {reference.ref_id for reference in source_claim.evidence_refs}
    if not source_ref_keys.issubset(anchor.allowed_evidence_ref_keys):
        return None
    evidence_rows = list(
        (
            await db.execute(
                select(GraphRelationCandidateEvidence).where(
                    GraphRelationCandidateEvidence.job_id == occurrence.job_id,
                    GraphRelationCandidateEvidence.extraction_unit_id == occurrence.extraction_unit_id,
                    GraphRelationCandidateEvidence.candidate_id == candidate.id,
                    GraphRelationCandidateEvidence.purged_at.is_(None),
                    GraphRelationCandidateEvidence.validation_status == "valid",
                )
            )
        ).scalars().all()
    )
    if source_ref_keys != _allowed_evidence_ref_keys(
        evidence_rows,
        {reference.ref_id: reference for reference in source_claim.evidence_refs},
    ):
        return None

    existing = await _find_existing_binding(
        db,
        raw_claim_occurrence_id=occurrence.extraction_occurrence_id,
        graph_relation_candidate_id=candidate.id,
    )
    if existing is not None:
        return existing
    row = GraphRawClaimProjectionBinding(
        raw_claim_id=claim.id,
        raw_claim_occurrence_id=occurrence.extraction_occurrence_id,
        graph_relation_candidate_id=candidate.id,
        binding_contract_version=BINDING_CONTRACT_VERSION,
        binding_method=BINDING_METHOD,
        binding_fingerprint=binding_fingerprint_v1(
            raw_claim_id=claim.id,
            raw_claim_occurrence_id=occurrence.extraction_occurrence_id,
            graph_relation_candidate_id=candidate.id,
        ),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await _find_existing_binding(
            db,
            raw_claim_occurrence_id=occurrence.extraction_occurrence_id,
            graph_relation_candidate_id=candidate.id,
        )
        if existing is not None:
            return existing
        raise
    return row
