"""Pure builder for append-only raw-claim decision projections."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.schemas._strict_datetime import strict_datetime
from app.schemas.claim_decision import (
    ClaimDecisionProjectionV1,
    DecisionKind,
    DecisionProducerKind,
    DecisionProposalV1,
    DecisionReasonCode,
    MappingCandidateProposalV1,
    SchemaExtensionCandidateProposalV1,
    deterministic_decision_id,
)
from app.schemas.raw_claim import RawClaimV1


class ClaimDecisionBuildError(ValueError):
    """The explicit decision proposal cannot be bound to the raw claim."""


def _validate_refs(claim: RawClaimV1, refs: tuple[str, ...]) -> None:
    claim_refs = {reference.ref_id for reference in claim.evidence_refs}
    if not set(refs).issubset(claim_refs):
        raise ClaimDecisionBuildError("decision proposal references evidence outside the claim")


def _validate_mapping(claim: RawClaimV1, proposal: MappingCandidateProposalV1) -> None:
    if proposal.raw_predicate != claim.raw_predicate:
        raise ClaimDecisionBuildError("mapping proposal raw_predicate does not match the claim")
    if proposal.source_mention != claim.source_mention or proposal.target_mention != claim.target_mention:
        raise ClaimDecisionBuildError("mapping proposal endpoint surface does not match the claim")
    if proposal.surface_direction != claim.surface_direction:
        raise ClaimDecisionBuildError("mapping proposal direction does not match the claim")
    mention_refs = {claim.source_mention.evidence_ref, claim.target_mention.evidence_ref}
    if not mention_refs.issubset(set(proposal.evidence_ref_ids)):
        raise ClaimDecisionBuildError("mapping proposal omits a source or target mention evidence reference")
    _validate_refs(claim, proposal.evidence_ref_ids)


def _validate_extension(claim: RawClaimV1, proposal: SchemaExtensionCandidateProposalV1) -> None:
    if proposal.raw_predicate != claim.raw_predicate:
        raise ClaimDecisionBuildError("schema extension raw_predicate does not match the claim")
    source = proposal.source_endpoint
    target = proposal.target_endpoint
    if (
        source.local_id != claim.source_mention.local_id
        or source.surface != claim.source_mention.surface
        or source.entity_type_hint != claim.source_mention.entity_type_hint
        or target.local_id != claim.target_mention.local_id
        or target.surface != claim.target_mention.surface
        or target.entity_type_hint != claim.target_mention.entity_type_hint
    ):
        raise ClaimDecisionBuildError("schema extension endpoint surface does not match the claim")
    if proposal.surface_direction != claim.surface_direction:
        raise ClaimDecisionBuildError("schema extension direction does not match the claim")
    if claim.source_mention.evidence_ref not in source.evidence_ref_ids:
        raise ClaimDecisionBuildError("source extension omits the source mention evidence reference")
    if claim.target_mention.evidence_ref not in target.evidence_ref_ids:
        raise ClaimDecisionBuildError("target extension omits the target mention evidence reference")
    refs = set(proposal.evidence_ref_ids) | set(source.evidence_ref_ids) | set(target.evidence_ref_ids)
    _validate_refs(claim, tuple(refs))


def build_claim_decision_projection(
    claim: RawClaimV1,
    *,
    decision_kind: DecisionKind,
    reason_code: DecisionReasonCode,
    proposal: DecisionProposalV1,
    decision_version: int,
    created_by_kind: DecisionProducerKind,
    producer_key: str,
    producer_version: str,
    created_at: datetime,
    id_namespace: UUID,
    extraction_occurrence_id: UUID | None = None,
) -> ClaimDecisionProjectionV1:
    """Bind an explicit proposal to one evidence-backed claim without inference."""
    if not isinstance(claim, RawClaimV1):
        raise TypeError("claim must be a validated RawClaimV1")
    if not isinstance(created_at, datetime):
        raise ClaimDecisionBuildError("decision created_at must be a datetime")
    try:
        strict_datetime(created_at, field="created_at")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ClaimDecisionBuildError("decision created_at is invalid") from exc
    if extraction_occurrence_id is not None and extraction_occurrence_id != claim.extraction_occurrence_id:
        raise ClaimDecisionBuildError("decision occurrence does not match the claim occurrence")
    if decision_kind == "mapping_candidate":
        if not isinstance(proposal, MappingCandidateProposalV1):
            raise ClaimDecisionBuildError("mapping_candidate requires a mapping proposal")
        _validate_mapping(claim, proposal)
    elif decision_kind == "schema_extension_candidate":
        if not isinstance(proposal, SchemaExtensionCandidateProposalV1):
            raise ClaimDecisionBuildError("schema_extension_candidate requires a schema extension proposal")
        _validate_extension(claim, proposal)
    else:
        raise ClaimDecisionBuildError("unsupported decision kind")

    draft = ClaimDecisionProjectionV1(
        decision_id=UUID(int=0),
        library_id=claim.library_id,
        document_id=claim.document_id,
        document_revision_id=claim.document_revision_id,
        revision_no=claim.revision_no,
        claim_id=claim.claim_id,
        extraction_occurrence_id=extraction_occurrence_id,
        decision_kind=decision_kind,
        reason_code=reason_code,
        created_by_kind=created_by_kind,
        producer_key=producer_key,
        producer_version=producer_version,
        created_at=created_at,
        decision_version=decision_version,
        proposal=proposal,
    )
    decision_id = deterministic_decision_id(id_namespace, draft.decision_fingerprint or "")
    return draft.model_copy(update={"decision_id": decision_id})
