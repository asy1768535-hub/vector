from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.claim_decision import (
    ClaimDecisionProjectionV1,
    CLAIM_DECISION_ID_NAMESPACE,
    MappingCandidateProposalV1,
    SchemaExtensionCandidateProposalV1,
    canonical_claim_decision_json,
    claim_decision_fingerprint,
    deterministic_decision_id,
)
from app.services.claim_decision_builder import (
    ClaimDecisionBuildError,
    build_claim_decision_projection,
)
from tests.test_raw_claim import _claim


NAMESPACE = uuid.UUID("b0000000-0000-0000-0000-000000000001")
CREATED_AT = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _mapping_proposal(claim=None, **changes) -> MappingCandidateProposalV1:
    claim = claim or _claim()
    values = {
        "raw_predicate": claim.raw_predicate,
        "source_mention": claim.source_mention,
        "target_mention": claim.target_mention,
        "surface_direction": claim.surface_direction,
        "evidence_ref_ids": [reference.ref_id for reference in claim.evidence_refs],
    }
    values.update(changes)
    return MappingCandidateProposalV1(**values)


def _extension_proposal(claim=None, **changes) -> SchemaExtensionCandidateProposalV1:
    claim = claim or _claim()
    values = {
        "raw_predicate": claim.raw_predicate,
        "source_endpoint": {
            "local_id": claim.source_mention.local_id,
            "surface": claim.source_mention.surface,
            "entity_type_hint": claim.source_mention.entity_type_hint,
            "evidence_ref_ids": [claim.source_mention.evidence_ref],
        },
        "target_endpoint": {
            "local_id": claim.target_mention.local_id,
            "surface": claim.target_mention.surface,
            "entity_type_hint": claim.target_mention.entity_type_hint,
            "evidence_ref_ids": [claim.target_mention.evidence_ref],
        },
        "surface_direction": claim.surface_direction,
        "evidence_ref_ids": [reference.ref_id for reference in claim.evidence_refs],
    }
    values.update(changes)
    return SchemaExtensionCandidateProposalV1(**values)


def _build(claim=None, **changes) -> ClaimDecisionProjectionV1:
    claim = claim or _claim()
    values = {
        "decision_kind": "mapping_candidate",
        "reason_code": "unknown_predicate",
        "proposal": _mapping_proposal(claim),
        "decision_version": 1,
        "created_by_kind": "system",
        "producer_key": "m3a-builder",
        "producer_version": "m3a-v1",
        "created_at": CREATED_AT,
        "id_namespace": NAMESPACE,
    }
    values.update(changes)
    return build_claim_decision_projection(claim, **values)


def _build_production(claim=None, **changes) -> ClaimDecisionProjectionV1:
    projection = _build(claim, **changes)
    return projection.model_copy(
        update={
            "decision_id": deterministic_decision_id(
                CLAIM_DECISION_ID_NAMESPACE,
                projection.decision_fingerprint or "",
            )
        }
    )


def test_mapping_projection_preserves_explicit_raw_predicate_and_all_scope():
    claim = _claim(raw_predicate="is alleged to support", surface_direction="unknown")
    projection = _build(claim, proposal=_mapping_proposal(claim))

    assert projection.proposal.raw_predicate == "is alleged to support"
    assert projection.proposal.surface_direction == "unknown"
    assert projection.proposal.source_mention.local_id == claim.source_mention.local_id
    assert projection.proposal.target_mention.local_id == claim.target_mention.local_id
    assert projection.library_id == claim.library_id
    assert projection.document_revision_id == claim.document_revision_id
    assert projection.claim_id == claim.claim_id
    assert projection.status == "pending"
    assert "relation_type_key" not in canonical_claim_decision_json(projection)


def test_mapping_without_suggested_key_and_explicit_key_are_both_valid():
    without_key = _build()
    with_key = _build(proposal=_mapping_proposal(suggested_canonical_key="supports"))

    assert without_key.proposal.suggested_canonical_key is None
    assert with_key.proposal.suggested_canonical_key == "supports"
    assert without_key.decision_id != with_key.decision_id
    assert without_key.decision_fingerprint != with_key.decision_fingerprint


def test_schema_extension_preserves_role_specific_unknown_endpoints():
    claim = _claim(
        source_mention={
            "local_id": "asset-1",
            "surface": "Asset A",
            "entity_type_hint": "unseen_asset_type",
            "evidence_ref": "evidence-1",
        },
        target_mention={
            "local_id": "counterparty-1",
            "surface": "Counterparty B",
            "entity_type_hint": "unseen_counterparty_type",
            "evidence_ref": "evidence-1",
        },
    )
    projection = _build(
        claim,
        decision_kind="schema_extension_candidate",
        reason_code="unknown_source_type",
        proposal=_extension_proposal(claim),
    )

    assert projection.proposal.source_endpoint.entity_type_hint == "unseen_asset_type"
    assert projection.proposal.target_endpoint.entity_type_hint == "unseen_counterparty_type"
    assert projection.proposal.surface_direction == claim.surface_direction


@pytest.mark.parametrize("reason_code", ["unknown_predicate", "unknown_direction", "ambiguous_mapping"])
def test_mapping_reason_codes_are_supported(reason_code):
    claim = _claim(surface_direction="unknown" if reason_code == "unknown_direction" else "source_to_target")
    projection = _build(claim, reason_code=reason_code, proposal=_mapping_proposal(claim))
    assert projection.reason_code == reason_code


@pytest.mark.parametrize("reason_code", ["unknown_source_type", "unknown_target_type"])
def test_schema_extension_reason_codes_are_supported(reason_code):
    claim = _claim()
    projection = _build(
        claim,
        decision_kind="schema_extension_candidate",
        reason_code=reason_code,
        proposal=_extension_proposal(claim),
    )
    assert projection.reason_code == reason_code


@pytest.mark.parametrize(
    ("decision_kind", "reason_code"),
    [
        ("mapping_candidate", "unknown_source_type"),
        ("mapping_candidate", "unknown_target_type"),
        ("schema_extension_candidate", "unknown_predicate"),
        ("schema_extension_candidate", "unknown_direction"),
        ("schema_extension_candidate", "ambiguous_mapping"),
    ],
)
def test_reason_codes_cannot_cross_decision_kind(decision_kind, reason_code):
    claim = _claim(surface_direction="unknown" if reason_code == "unknown_direction" else "source_to_target")
    proposal = _mapping_proposal(claim) if decision_kind == "mapping_candidate" else _extension_proposal(claim)
    with pytest.raises((ClaimDecisionBuildError, ValidationError), match="incompatible|reason"):
        _build(claim, decision_kind=decision_kind, reason_code=reason_code, proposal=proposal)


def test_unknown_direction_requires_unknown_surface_direction():
    claim = _claim(surface_direction="source_to_target")
    with pytest.raises((ClaimDecisionBuildError, ValidationError), match="unknown.*direction|incompatible"):
        _build(claim, reason_code="unknown_direction", proposal=_mapping_proposal(claim))


@pytest.mark.parametrize("domain", ["asset", "legal", "medical", "ordinary"])
def test_domain_fixture_is_opaque_and_not_mapped(domain):
    claim = _claim(raw_predicate=f"{domain} surface relation")
    projection = _build(claim, proposal=_mapping_proposal(claim))
    assert projection.proposal.raw_predicate == claim.raw_predicate
    assert projection.proposal.suggested_canonical_key is None


def test_unknown_direction_never_swaps_endpoints_or_creates_canonical_relation():
    claim = _claim(surface_direction="unknown")
    projection = _build(claim, proposal=_mapping_proposal(claim))

    assert projection.proposal.surface_direction == "unknown"
    assert projection.proposal.source_mention.local_id == "source-1"
    assert projection.proposal.target_mention.local_id == "target-1"
    assert not hasattr(projection.proposal, "canonical_relation")


def test_claim_scope_is_copied_and_mismatched_occurrence_is_rejected():
    claim = _claim()
    projection = _build(claim)
    assert projection.library_id == claim.library_id
    assert projection.document_id == claim.document_id
    assert projection.document_revision_id == claim.document_revision_id
    with pytest.raises(ClaimDecisionBuildError, match="occurrence"):
        _build(claim, extraction_occurrence_id=uuid.uuid4())


def test_optional_occurrence_is_preserved_in_decision_identity_and_round_trip():
    claim = _claim()
    without_occurrence = _build(claim)
    with_occurrence = _build(claim, extraction_occurrence_id=claim.extraction_occurrence_id)

    assert without_occurrence.extraction_occurrence_id is None
    assert with_occurrence.extraction_occurrence_id == claim.extraction_occurrence_id
    assert without_occurrence.decision_id != with_occurrence.decision_id
    assert without_occurrence.decision_fingerprint != with_occurrence.decision_fingerprint

    restored = ClaimDecisionProjectionV1.model_validate(
        without_occurrence.model_dump(mode="json")
    )
    assert restored.extraction_occurrence_id is None
    assert restored.decision_id == without_occurrence.decision_id
    assert restored.decision_fingerprint == without_occurrence.decision_fingerprint


def test_occurrence_mismatch_and_no_evidence_claim_are_rejected():
    claim = _claim()
    with pytest.raises(ClaimDecisionBuildError, match="occurrence"):
        _build(claim, extraction_occurrence_id=uuid.uuid4())

    payload = claim.model_dump(mode="python")
    payload["evidence_refs"] = []
    with pytest.raises(ValidationError):
        from app.schemas.raw_claim import RawClaimV1

        RawClaimV1(**payload)


def test_proposal_surface_and_direction_mismatch_is_rejected():
    claim = _claim()
    with pytest.raises(ClaimDecisionBuildError, match="raw_predicate"):
        _build(claim, proposal=_mapping_proposal(claim, raw_predicate="invented"))
    with pytest.raises(ClaimDecisionBuildError, match="direction"):
        _build(claim, proposal=_mapping_proposal(claim, surface_direction="unknown"))
    with pytest.raises(ClaimDecisionBuildError, match="endpoint"):
        _build(
            claim,
            proposal=_mapping_proposal(
                claim,
                source_mention={
                    "local_id": "different",
                    "surface": "Source entity",
                    "entity_type_hint": "source_type",
                    "evidence_ref": "evidence-1",
                },
            ),
        )


def test_mapping_proposal_must_include_both_mention_evidence_refs():
    first_ref = _claim().evidence_refs[0].model_dump()
    second_ref = {
        **first_ref,
        "ref_id": "evidence-2",
        "evidence_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "unit_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "chunk_id": None,
        "block_id": None,
        "locator": None,
    }
    claim = _claim(
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-2",
        },
        evidence_refs=[first_ref, second_ref],
    )
    with pytest.raises(ClaimDecisionBuildError, match="mention evidence"):
        _build(claim, proposal=_mapping_proposal(claim, evidence_ref_ids=["evidence-1"]))

    with pytest.raises(ClaimDecisionBuildError, match="mention evidence"):
        _build(claim, proposal=_mapping_proposal(claim, evidence_ref_ids=["unrelated"]))


def test_schema_extension_must_bind_role_evidence_refs():
    claim = _claim()
    proposal = _extension_proposal(claim)
    with pytest.raises(ClaimDecisionBuildError, match="source mention"):
        _build(
            claim,
            decision_kind="schema_extension_candidate",
            reason_code="unknown_source_type",
            proposal=proposal.model_copy(
                update={
                    "source_endpoint": proposal.source_endpoint.model_copy(
                        update={"evidence_ref_ids": ("not-the-source",)}
                    )
                }
            ),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"relation_type_key": "supports"},
        {"rawFileSha256": "a" * 64},
        {"storage-path": "secret"},
        {"objectKey": "secret"},
        {"proposal": {"nested": {"text": "body"}}},
    ],
)
def test_extra_sensitive_and_untyped_proposal_payload_is_rejected(payload):
    base = _mapping_proposal().model_dump(mode="python")
    base.update(payload)
    with pytest.raises(ValidationError):
        MappingCandidateProposalV1(**base)


def test_bounded_strings_and_proposal_size_are_enforced():
    with pytest.raises(ValidationError):
        _mapping_proposal(suggested_canonical_key="x" * 129)
    with pytest.raises(ValidationError):
        _mapping_proposal(evidence_ref_ids=[f"ref-{i}" for i in range(17)])
    with pytest.raises(ValidationError):
        _mapping_proposal(raw_predicate="x" * 257)


def test_schema_version_status_and_datetime_are_strict():
    projection = _build()
    payload = projection.model_dump(mode="json")
    payload["decision_schema_version"] = "claim_decision_projection_v2"
    with pytest.raises(ValidationError):
        ClaimDecisionProjectionV1(**payload)

    payload = projection.model_dump(mode="json")
    payload["created_at"] = "2026-08-07T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        ClaimDecisionProjectionV1(**payload)


def test_fingerprint_is_canonical_and_versioned_append_only():
    first = _build()
    rebuilt = _build(created_at=CREATED_AT + timedelta(days=2))
    second_version = _build(decision_version=2)
    second_producer = _build(producer_version="m3a-v2")
    second_producer_key = _build(producer_key="another-m3a-builder")

    assert first.decision_id == rebuilt.decision_id
    assert first.decision_fingerprint == rebuilt.decision_fingerprint
    assert first.decision_id != second_version.decision_id
    assert first.decision_id != second_producer.decision_id
    assert first.decision_id != second_producer_key.decision_id
    assert claim_decision_fingerprint(json.loads(canonical_claim_decision_json(first))) == first.decision_fingerprint


@pytest.mark.parametrize("producer_key", ["", "x" * 129, "C:\\secret\\producer", "rawFileSha256"])
def test_producer_key_is_bounded_and_non_sensitive(producer_key):
    with pytest.raises((ValidationError, ValueError)):
        _build(producer_key=producer_key)


def test_evidence_reference_order_is_not_part_of_decision_identity():
    first_ref = _claim().evidence_refs[0].model_dump()
    second_ref = {
        **first_ref,
        "ref_id": "evidence-2",
        "evidence_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "unit_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "chunk_id": None,
        "block_id": None,
        "locator": None,
    }
    claim = _claim(
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-2",
        },
        evidence_refs=[first_ref, second_ref],
    )
    first = _build(claim, proposal=_mapping_proposal(claim, evidence_ref_ids=["evidence-2", "evidence-1"]))
    reordered = _build(claim, proposal=_mapping_proposal(claim, evidence_ref_ids=["evidence-1", "evidence-2"]))

    assert first.decision_fingerprint == reordered.decision_fingerprint


def test_mapping_and_extension_are_distinct_append_only_events():
    claim = _claim()
    mapping = _build(claim)
    extension = _build(
        claim,
        decision_kind="schema_extension_candidate",
        reason_code="unknown_target_type",
        proposal=_extension_proposal(claim),
    )
    assert mapping.decision_kind == "mapping_candidate"
    assert extension.decision_kind == "schema_extension_candidate"
    assert mapping.decision_id != extension.decision_id
