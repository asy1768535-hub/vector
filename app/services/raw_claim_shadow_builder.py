"""Pure builder from an explicit shadow response to RawClaimV1."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid5

from app.schemas.raw_claim import EvidenceReferenceV1, RawClaimV1
from app.schemas.shadow_raw_response import (
    ShadowExtractionProvenanceV1,
    ShadowRawResponseV1,
)


class ShadowClaimBuildError(ValueError):
    """The shadow response cannot be safely promoted to a raw claim."""


def deterministic_claim_id(namespace: UUID, content_fingerprint: str) -> UUID:
    return uuid5(namespace, f"raw_claim_v1:claim:{content_fingerprint}")


def deterministic_occurrence_id(namespace: UUID, occurrence_fingerprint: str) -> UUID:
    return uuid5(namespace, f"raw_claim_v1:occurrence:{occurrence_fingerprint}")


def _validated_response(response: ShadowRawResponseV1) -> ShadowRawResponseV1:
    if not isinstance(response, ShadowRawResponseV1):
        raise TypeError("shadow builder accepts ShadowRawResponseV1 only")
    return ShadowRawResponseV1.model_validate(response.model_dump(mode="json"))


def _validated_evidence(
    response: ShadowRawResponseV1,
    evidence: Mapping[str, EvidenceReferenceV1],
) -> tuple[EvidenceReferenceV1, ...]:
    declared = set(response.evidence_ref_keys)
    if set(evidence) != declared:
        raise ShadowClaimBuildError("verified evidence mapping does not match response ref keys")
    ordered: list[EvidenceReferenceV1] = []
    for ref_key in response.evidence_ref_keys:
        reference = evidence.get(ref_key)
        if not isinstance(reference, EvidenceReferenceV1):
            raise TypeError("verified evidence mapping accepts EvidenceReferenceV1 only")
        if reference.ref_id != ref_key:
            raise ShadowClaimBuildError("verified evidence mapping key does not match ref_id")
        ordered.append(reference)
    return tuple(ordered)


def _build_claim(
    response: ShadowRawResponseV1,
    provenance: ShadowExtractionProvenanceV1,
    evidence_refs: tuple[EvidenceReferenceV1, ...],
    *,
    claim_id: UUID,
    occurrence_id: UUID,
) -> RawClaimV1:
    return RawClaimV1(
        claim_id=claim_id,
        library_id=provenance.library_id,
        document_id=provenance.document_id,
        document_revision_id=provenance.document_revision_id,
        revision_no=provenance.revision_no,
        job_id=provenance.job_id,
        extraction_unit_id=provenance.extraction_unit_id,
        source_mention=response.source_mention,
        raw_predicate=response.surface_raw_predicate,
        target_mention=response.target_mention,
        surface_direction=response.surface_direction,
        negation=response.negation,
        modality=response.modality,
        qualifiers=response.qualifiers,
        valid_time=response.valid_time,
        effective_time=response.effective_time,
        evidence_refs=evidence_refs,
        extractor_version=provenance.extractor_version,
        prompt_version=provenance.prompt_version,
        model_provider=provenance.model_provider,
        model_name=provenance.model_name,
        model_config_hash=provenance.model_config_hash,
        prompt_content_hash=provenance.prompt_content_hash,
        parser_version=provenance.parser_version,
        normalization_rule_version=provenance.normalization_rule_version,
        ontology_snapshot_hash=provenance.ontology_snapshot_hash,
        extraction_occurrence_id=occurrence_id,
    )


def build_raw_claim_from_shadow(
    response: ShadowRawResponseV1,
    *,
    verified_evidence: Mapping[str, EvidenceReferenceV1],
    provenance: ShadowExtractionProvenanceV1,
    id_namespace: UUID,
) -> RawClaimV1:
    """Build an idempotent RawClaimV1 without canonical inference or DB access."""
    response = _validated_response(response)
    evidence_refs = _validated_evidence(response, verified_evidence)
    if not isinstance(provenance, ShadowExtractionProvenanceV1):
        raise TypeError("shadow builder accepts ShadowExtractionProvenanceV1 only")
    provenance = ShadowExtractionProvenanceV1.model_validate(provenance.model_dump(mode="json"))

    provisional = _build_claim(
        response,
        provenance,
        evidence_refs,
        claim_id=UUID(int=0),
        occurrence_id=UUID(int=0),
    )
    claim_id = deterministic_claim_id(id_namespace, provisional.content_scoped_claim_fingerprint)
    occurrence_id = deterministic_occurrence_id(
        id_namespace, provisional.extraction_occurrence_fingerprint
    )
    return _build_claim(
        response,
        provenance,
        evidence_refs,
        claim_id=claim_id,
        occurrence_id=occurrence_id,
    )
