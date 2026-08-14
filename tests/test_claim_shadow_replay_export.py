from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.raw_claim import GraphRawClaim
from app.schemas.claim_decision import (
    ClaimDecisionProjectionV1,
    MappingCandidateProposalV1,
    deterministic_decision_id,
)
from app.schemas.raw_claim import RawClaimV1, stable_evidence_identity
from app.services.claim_shadow_replay_export import (
    ClaimShadowReplayExportError,
    ClaimShadowReplayExportRequest,
    _claim_from_rows,
    _restore_core_evidence_refs,
)
from app.services.claim_shadow_replay_export import _decision_from_row
from tests.test_claim_decision import _build
from tests.test_raw_claim import _claim


def _core_and_occurrence(claim: RawClaimV1):
    from app.services.raw_claim_persistence import _core_payload

    core = GraphRawClaim(id=claim.claim_id, **_core_payload(claim))
    occurrence = SimpleNamespace(
        extraction_occurrence_id=claim.extraction_occurrence_id,
        claim_id=claim.claim_id,
        extraction_occurrence_fingerprint=claim.extraction_occurrence_fingerprint,
        job_id=claim.job_id,
        extraction_unit_id=claim.extraction_unit_id,
        extractor_version=claim.extractor_version,
        prompt_version=claim.prompt_version,
        model_provider=claim.model_provider,
        model_name=claim.model_name,
        model_config_hash=claim.model_config_hash,
        prompt_content_hash=claim.prompt_content_hash,
        parser_version=claim.parser_version,
        normalization_rule_version=claim.normalization_rule_version,
        ontology_snapshot_hash=claim.ontology_snapshot_hash,
        evidence_refs=claim.model_dump(mode="json")["evidence_refs"],
    )
    return core, occurrence


def test_export_rebuilds_typed_claim_from_persisted_core_and_occurrence():
    claim = _claim()
    core, occurrence = _core_and_occurrence(claim)

    rebuilt = _claim_from_rows(core, occurrence)

    assert rebuilt == claim
    assert rebuilt.content_scoped_claim_fingerprint == claim.content_scoped_claim_fingerprint
    assert rebuilt.extraction_occurrence_fingerprint == claim.extraction_occurrence_fingerprint


def test_export_rebuilds_claim_with_nullable_semantic_evidence_refs():
    payload = _claim().model_dump(mode="json")
    payload["negation"]["evidence_ref"] = None
    payload["modality"]["evidence_ref"] = None
    payload["qualifiers"][0]["evidence_ref"] = None
    payload["content_scoped_claim_fingerprint"] = None
    payload["extraction_occurrence_fingerprint"] = None
    claim = RawClaimV1.model_validate(payload)
    core, occurrence = _core_and_occurrence(claim)

    rebuilt = _claim_from_rows(core, occurrence)

    assert rebuilt == claim
    assert rebuilt.negation.evidence_ref is None
    assert rebuilt.modality.evidence_ref is None
    assert rebuilt.qualifiers[0].evidence_ref is None


def test_export_preserves_nullable_evidence_refs_and_restores_known_identity():
    claim = _claim()
    identity = stable_evidence_identity(claim.evidence_refs[0])
    identity_to_ref = {identity: claim.evidence_refs[0].ref_id}
    payload = [
        {"value": "asserted", "evidence_ref": None},
        {"value": "bounded", "evidence_ref": identity},
        {"nested": [{"value": "optional", "evidence_ref": None}]},
    ]

    first = _restore_core_evidence_refs(payload, identity_to_ref)
    second = _restore_core_evidence_refs(payload, identity_to_ref)

    assert first == second
    assert first == [
        {"value": "asserted", "evidence_ref": None},
        {"value": "bounded", "evidence_ref": claim.evidence_refs[0].ref_id},
        {"nested": [{"value": "optional", "evidence_ref": None}]},
    ]
    assert identity not in json.dumps(first, sort_keys=True)


@pytest.mark.parametrize("identity", ["f" * 64, 1, {}, []])
def test_export_rejects_unknown_or_non_string_non_null_evidence_identity(identity):
    with pytest.raises(ClaimShadowReplayExportError, match="core_evidence_ref_unmapped"):
        _restore_core_evidence_refs({"evidence_ref": identity}, {})


def test_shared_evidence_identity_keeps_persistence_bytes_unchanged():
    reference = _claim().evidence_refs[0]
    legacy_payload = reference.model_dump(mode="json")
    legacy_payload.pop("ref_id")
    legacy_payload.pop("job_id")
    legacy_payload.pop("extraction_unit_id")
    legacy = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert stable_evidence_identity(reference) == legacy


def test_export_evidence_binding_is_order_independent_but_detects_swap():
    claim = _claim()
    core, occurrence = _core_and_occurrence(claim)
    occurrence.evidence_refs = list(reversed(occurrence.evidence_refs))
    assert _claim_from_rows(core, occurrence) == claim

    core.evidence_refs = [dict(core.evidence_refs[0], evidence_identity="f" * 64)]
    with pytest.raises(ClaimShadowReplayExportError, match="core_evidence_binding_conflict"):
        _claim_from_rows(core, occurrence)


def test_export_rejects_duplicate_stable_evidence_identity():
    claim = _claim()
    second = claim.evidence_refs[0].model_dump(mode="json")
    second["ref_id"] = "evidence-2"
    claim_payload = claim.model_dump(mode="json")
    claim_payload["evidence_refs"] = [
        claim.evidence_refs[0].model_dump(mode="json"),
        second,
    ]
    claim_payload["content_scoped_claim_fingerprint"] = None
    claim_payload["extraction_occurrence_fingerprint"] = None
    claim = RawClaimV1.model_validate(claim_payload)
    core, occurrence = _core_and_occurrence(claim)

    with pytest.raises(ClaimShadowReplayExportError, match="duplicate_evidence_identity"):
        _claim_from_rows(core, occurrence)


def test_export_rejects_tampered_occurrence_fingerprint():
    claim = _claim()
    core, occurrence = _core_and_occurrence(claim)
    occurrence.extraction_occurrence_fingerprint = "0" * 64

    with pytest.raises(ClaimShadowReplayExportError, match="raw_claim_row_invalid"):
        _claim_from_rows(core, occurrence)


def test_export_request_requires_explicit_bounded_scope():
    with pytest.raises(ClaimShadowReplayExportError, match="export_scope_required"):
        ClaimShadowReplayExportRequest(library_id=UUID(int=1))

    request = ClaimShadowReplayExportRequest(
        library_id=UUID(int=1),
        job_ids=(UUID(int=2),),
        max_claims=1,
    )
    assert request.job_ids == (UUID(int=2),)

    with pytest.raises(ClaimShadowReplayExportError, match="job_ids_duplicate"):
        ClaimShadowReplayExportRequest(
            library_id=UUID(int=1),
            job_ids=(UUID(int=2), UUID(int=2)),
        )


def test_decision_projection_round_trip_contract_is_available_to_export():
    claim = _claim()
    proposal = MappingCandidateProposalV1(
        raw_predicate=claim.raw_predicate,
        source_mention=claim.source_mention,
        target_mention=claim.target_mention,
        surface_direction=claim.surface_direction,
        evidence_ref_ids=("evidence-1",),
    )
    decision = ClaimDecisionProjectionV1(
        decision_id=uuid4(),
        decision_version=1,
        library_id=claim.library_id,
        document_id=claim.document_id,
        document_revision_id=claim.document_revision_id,
        revision_no=claim.revision_no,
        claim_id=claim.claim_id,
        extraction_occurrence_id=claim.extraction_occurrence_id,
        decision_kind="mapping_candidate",
        reason_code="unknown_predicate",
        created_by_kind="system",
        producer_key="m4d-test",
        producer_version="v1",
        created_at="2026-08-07T00:00:00Z",
        proposal=proposal,
    )
    assert decision.decision_id == decision.decision_id
    assert deterministic_decision_id(UUID(int=9), decision.decision_fingerprint) != decision.decision_id


def test_export_rejects_decision_id_outside_production_namespace():
    claim = _claim()
    decision = _build(claim)
    row = SimpleNamespace(**decision.model_dump(mode="python"))

    with pytest.raises(ClaimShadowReplayExportError, match="decision_id_mismatch"):
        _decision_from_row(row)
