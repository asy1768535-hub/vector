from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.evidence_locator import sha256_text
from app.schemas.raw_claim import EvidenceReferenceV1, MAX_JSON_NODES
from app.schemas.shadow_raw_response import (
    ShadowExtractionConfigV1,
    ShadowExtractionProvenanceV1,
    ShadowRawResponseV1,
    resolve_shadow_extraction,
)
from app.services.raw_claim_shadow_builder import (
    ShadowClaimBuildError,
    build_raw_claim_from_shadow,
)


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
OTHER_JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
OTHER_UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000002")
NAMESPACE = uuid.UUID("70000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def _provenance(**changes) -> ShadowExtractionProvenanceV1:
    values = {
        "library_id": LIBRARY_ID,
        "document_id": DOCUMENT_ID,
        "document_revision_id": REVISION_ID,
        "revision_no": 1,
        "job_id": JOB_ID,
        "extraction_unit_id": UNIT_ID,
        "extractor_version": "shadow-extractor-v1",
        "prompt_version": "shadow-prompt-v1",
        "model_provider": "fixture-provider",
        "model_name": "fixture-model",
        "model_config_hash": HASH,
        "prompt_content_hash": HASH,
        "parser_version": "shadow-parser-v1",
        "normalization_rule_version": "shadow-normalization-v1",
        "ontology_snapshot_hash": HASH,
    }
    values.update(changes)
    return ShadowExtractionProvenanceV1(**values)


def _reference(
    ref_id: str = "evidence-1",
    *,
    job_id: uuid.UUID = JOB_ID,
    extraction_unit_id: uuid.UUID = UNIT_ID,
    evidence_id: uuid.UUID = EVIDENCE_ID,
) -> EvidenceReferenceV1:
    quote = f"quote for {ref_id}"
    return EvidenceReferenceV1(
        ref_id=ref_id,
        evidence_id=evidence_id,
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        revision_no=1,
        job_id=job_id,
        extraction_unit_id=extraction_unit_id,
        unit_id=evidence_id,
        quote_sha256=sha256_text(quote),
        unit_text_sha256=sha256_text(quote),
        source_span={"start": 0, "end": len(quote)},
    )


def _response(**changes) -> ShadowRawResponseV1:
    values = {
        "source_mention": {
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": "source_type",
            "evidence_ref": "evidence-1",
        },
        "surface_raw_predicate": "supports",
        "target_mention": {
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-1",
        },
        "surface_direction": "source_to_target",
        "negation": {"value": False, "evidence_ref": "evidence-1"},
        "modality": {"value": "asserted", "evidence_ref": "evidence-1"},
        "evidence_ref_keys": ["evidence-1"],
    }
    values.update(changes)
    return ShadowRawResponseV1(**values)


def _build(response: ShadowRawResponseV1 | None = None, **provenance_changes):
    response = response or _response()
    provenance = _provenance(**provenance_changes)
    evidence = {
        "evidence-1": _reference(
            job_id=provenance.job_id,
            extraction_unit_id=provenance.extraction_unit_id,
        )
    }
    return build_raw_claim_from_shadow(
        response,
        verified_evidence=evidence,
        provenance=provenance,
        id_namespace=NAMESPACE,
    )


@pytest.mark.parametrize(
    ("direction", "predicate"),
    [
        ("source_to_target", "supports"),
        ("target_to_source", "is supported by"),
        ("undirected", "is associated with"),
        ("unknown", "is alleged to support"),
    ],
)
def test_protocol_preserves_surface_predicate_and_direction(direction, predicate):
    response = _response(surface_raw_predicate=predicate, surface_direction=direction)
    claim = _build(response)

    assert claim.raw_predicate == predicate
    assert claim.surface_direction == direction
    if direction == "unknown":
        assert claim.source_mention.local_id == "source-1"
        assert claim.target_mention.local_id == "target-1"


@pytest.mark.parametrize("domain", ["asset", "legal", "medical", "ordinary"])
def test_domain_fixture_values_are_dynamic_without_an_allowlist(domain):
    claim = _build(_response(surface_raw_predicate=f"{domain} surface predicate"))
    assert claim.raw_predicate == f"{domain} surface predicate"


def test_canonical_relation_key_is_rejected_and_can_never_supply_raw_predicate():
    with pytest.raises(ValidationError, match="extra"):
        _response(canonical_relation_type_key="supports")


def test_nested_claim_properties_and_multiple_evidence_refs_are_bound():
    response = _response(
        source_mention={
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": "source_type",
            "evidence_ref": "evidence-1",
        },
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-2",
        },
        negation={"value": True, "evidence_ref": "evidence-2"},
        modality={"value": "alleged", "evidence_ref": "evidence-1"},
        qualifiers=[
            {"key": "jurisdiction", "value": {"name": "North"}, "evidence_ref": "evidence-2"}
        ],
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "evidence-1",
        },
        effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-2"},
        evidence_ref_keys=["evidence-1", "evidence-2"],
    )
    provenance = _provenance()
    evidence = {
        "evidence-1": _reference(),
        "evidence-2": _reference("evidence-2", evidence_id=uuid.UUID("60000000-0000-0000-0000-000000000002")),
    }
    claim = build_raw_claim_from_shadow(
        response,
        verified_evidence=evidence,
        provenance=provenance,
        id_namespace=NAMESPACE,
    )

    assert claim.negation.value is True
    assert claim.modality.value == "alleged"
    assert claim.qualifiers[0].value == {"name": "North"}
    assert {ref.ref_id for ref in claim.evidence_refs} == {"evidence-1", "evidence-2"}


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_ref_keys": ["missing"]},
        {"evidence_ref_keys": ["evidence-1", "evidence-1"]},
    ],
)
def test_protocol_rejects_dangling_or_duplicate_evidence_keys(changes):
    with pytest.raises(ValidationError):
        _response(**changes)


def test_builder_rejects_missing_or_mismatched_verified_evidence_mapping():
    response = _response()
    with pytest.raises(ShadowClaimBuildError, match="match"):
        build_raw_claim_from_shadow(
            response,
            verified_evidence={},
            provenance=_provenance(),
            id_namespace=NAMESPACE,
        )

    with pytest.raises(ShadowClaimBuildError, match="key"):
        build_raw_claim_from_shadow(
            response,
            verified_evidence={"evidence-1": _reference("different")},
            provenance=_provenance(),
            id_namespace=NAMESPACE,
        )


def test_builder_rejects_cross_scope_evidence_and_untyped_mapping():
    with pytest.raises(ValidationError, match="scope"):
        build_raw_claim_from_shadow(
            _response(),
            verified_evidence={"evidence-1": _reference(job_id=OTHER_JOB_ID)},
            provenance=_provenance(),
            id_namespace=NAMESPACE,
        )

    with pytest.raises(TypeError, match="EvidenceReferenceV1"):
        build_raw_claim_from_shadow(
            _response(),
            verified_evidence={"evidence-1": {"ref_id": "evidence-1"}},
            provenance=_provenance(),
            id_namespace=NAMESPACE,
        )


def test_protocol_rejects_duplicate_local_ids_and_invalid_direction():
    with pytest.raises(ValidationError, match="local IDs"):
        _response(
            target_mention={
                "local_id": "source-1",
                "surface": "Target entity",
                "entity_type_hint": "target_type",
                "evidence_ref": "evidence-1",
            }
        )
    with pytest.raises(ValidationError):
        _response(surface_direction="reverse")


@pytest.mark.parametrize(
    "changes",
    [
        {"surface_raw_predicate": ""},
        {"surface_raw_predicate": "x" * 257},
        {"qualifiers": [{"key": "x", "value": {"rawFileSha256": "secret"}}]},
        {"qualifiers": [{"key": "x", "value": {"storage-path": "secret"}}]},
        {"qualifiers": [{"key": "x", "value": list(range(MAX_JSON_NODES + 1))}]},
    ],
)
def test_protocol_rejects_unbounded_or_sensitive_payload(changes):
    with pytest.raises((ValidationError, ValueError)):
        _response(**changes)


def test_protocol_rejects_missing_evidence_binding_for_nested_properties():
    with pytest.raises(ValidationError, match="exactly bind"):
        _response(qualifiers=[{"key": "status", "value": "alleged", "evidence_ref": "missing"}])


def test_deterministic_ids_and_fingerprints_separate_content_from_occurrence_provenance():
    first = _build()
    rebuilt = _build()
    rerun = _build(model_name="different-model", job_id=OTHER_JOB_ID, extraction_unit_id=OTHER_UNIT_ID)

    assert first.claim_id == rebuilt.claim_id
    assert first.extraction_occurrence_id == rebuilt.extraction_occurrence_id
    assert first.content_scoped_claim_fingerprint == rebuilt.content_scoped_claim_fingerprint
    assert first.extraction_occurrence_fingerprint == rebuilt.extraction_occurrence_fingerprint
    assert rerun.claim_id == first.claim_id
    assert rerun.content_scoped_claim_fingerprint == first.content_scoped_claim_fingerprint
    assert rerun.extraction_occurrence_id != first.extraction_occurrence_id
    assert rerun.extraction_occurrence_fingerprint != first.extraction_occurrence_fingerprint


def test_default_shadow_rollout_is_off_and_library_policy_is_explicit():
    assert resolve_shadow_extraction(ShadowExtractionConfigV1()) is False
    assert resolve_shadow_extraction(
        ShadowExtractionConfigV1(global_enabled=False, library_policy="enabled")
    ) is True
    assert resolve_shadow_extraction(
        ShadowExtractionConfigV1(global_enabled=True, library_policy="disabled")
    ) is False
