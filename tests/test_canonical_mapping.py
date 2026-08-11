from __future__ import annotations

import ast
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.canonical_mapping import (
    CLAIM_DECISION_ID_NAMESPACE,
    CANONICAL_MAPPING_UUID_NAMESPACE,
    CANONICAL_SCHEMA_HASH,
    MAPPING_SCHEMA_HASH,
    CanonicalEndpointV1,
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    CanonicalMappingProposalV1,
    CanonicalSemanticProjectionV1,
    EndpointResolutionAttestationV1,
    EvidenceValidationAttestationV1,
    EntityLinkDecisionV1,
    EntityLinkResolverProvenanceV1,
    EndpointTypeBindingV1,
    EvidenceValidationBindingV1,
    FrozenOntologySnapshotV1,
    MapperProvenanceV1,
    MappingAuthorizationProvenanceV1,
    MappingAuthorizationRegistryEntryV1,
    MappingAuthorizationRegistrySnapshotV1,
    MappingScopeV1,
    MappingActorProvenanceV1,
    MappingRemapProvenanceV1,
    MappingSourceProvenanceV1,
    build_canonical_mapping,
    canonical_mapping_attempt_fingerprint,
    canonical_mapping_input_json,
    canonical_mapping_json,
    canonical_mapping_json_value,
    semantic_projection_from_claim,
)
from app.schemas.claim_decision import (
    DecisionEndpointV1,
    MappingCandidateProposalV1,
    SchemaExtensionCandidateProposalV1,
)
from app.schemas.claim_shadow_replay_artifact import redact_raw_claim_v2
from app.schemas.raw_claim import RawClaimV1
from tests.test_raw_claim import _claim, _locator
from app.services.claim_decision_builder import build_claim_decision_projection


HASH = "a" * 64
ONTOLOGY_HASH = ""
ONTOLOGY_ID = uuid.UUID("b1000000-0000-0000-0000-000000000001")
SOURCE_ENTITY_ID = uuid.UUID("b2000000-0000-0000-0000-000000000001")
TARGET_ENTITY_ID = uuid.UUID("b3000000-0000-0000-0000-000000000001")
CREATED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _plain_claim(**changes):
    values = {
        "modality": {"value": None, "evidence_ref": None},
        "qualifiers": [],
        "valid_time": None,
        "effective_time": None,
        "ontology_snapshot_hash": ONTOLOGY_HASH,
    }
    values.update(changes)
    return _claim(**values)


def _ontology(*, contract_version: str = "ontology-contract-v1") -> FrozenOntologySnapshotV1:
    constraints = [
        {"relation_key": "supports", "source_type_key": "source_type", "target_type_key": "target_type", "direction": "source_to_target"},
        {"relation_key": "supports", "source_type_key": "source_type", "target_type_key": "target_type", "direction": "target_to_source"},
        {"relation_key": "supports", "source_type_key": "target_type", "target_type_key": "source_type", "direction": "source_to_target"},
        {"relation_key": "supports", "source_type_key": "target_type", "target_type_key": "source_type", "direction": "target_to_source"},
        {"relation_key": "supports", "source_type_key": "source_type", "target_type_key": "target_type", "direction": "undirected"},
        {"relation_key": "supports", "source_type_key": "target_type", "target_type_key": "source_type", "direction": "undirected"},
        {"relation_key": "symmetric_supports", "source_type_key": "source_type", "target_type_key": "target_type", "direction": "undirected"},
        {"relation_key": "symmetric_supports", "source_type_key": "target_type", "target_type_key": "source_type", "direction": "undirected"},
    ]
    return FrozenOntologySnapshotV1.from_content(
        ontology_version_id=ONTOLOGY_ID,
        ontology_contract_version=contract_version,
        entity_type_keys=("source_type", "target_type"),
        relation_type_keys=("supports", "symmetric_supports"),
        constraints=constraints,
    )


ONTOLOGY_HASH = _ontology().ontology_snapshot_hash


def _scope(claim) -> MappingScopeV1:
    return MappingScopeV1(
        library_id=claim.library_id,
        document_id=claim.document_id,
        document_revision_id=claim.document_revision_id,
        revision_no=claim.revision_no,
        job_id=claim.job_id,
        extraction_unit_id=claim.extraction_unit_id,
        claim_id=claim.claim_id,
        extraction_occurrence_id=claim.extraction_occurrence_id,
    )


def _registry(
    claim,
    ontology: FrozenOntologySnapshotV1,
    relation_key: str = "supports",
) -> MappingAuthorizationRegistrySnapshotV1:
    return MappingAuthorizationRegistrySnapshotV1.from_content(
        scope=_scope(claim),
        ontology_snapshot_hash=ontology.ontology_snapshot_hash,
        entries=[
            MappingAuthorizationRegistryEntryV1(
                authorization_key="fixture-registry",
                authorization_version="m0-v1",
                surface_predicate_sha256=hashlib.sha256(claim.raw_predicate.encode("utf-8")).hexdigest(),
                canonical_relation_key_sha256=hashlib.sha256(relation_key.encode("utf-8")).hexdigest(),
                allowed_endpoint_transforms=("identity", "swap"),
                allowed_predicate_transforms=("identity", "inverse", "symmetric"),
                allowed_canonical_directions=("source_to_target", "target_to_source", "undirected"),
                allowed_source_type_keys=("source_type", "target_type"),
                allowed_target_type_keys=("source_type", "target_type"),
            )
        ],
    )


def _authorization(
    claim,
    relation_key: str = "supports",
    registry: MappingAuthorizationRegistrySnapshotV1 | None = None,
) -> MappingAuthorizationProvenanceV1:
    registry = registry or _registry(claim, _ontology(), relation_key)
    return MappingAuthorizationProvenanceV1(
        authorization_key="fixture-registry",
        authorization_version="m0-v1",
        registry_snapshot=registry,
        registry_hash=registry.registry_snapshot_hash,
        authorized_surface_predicate_sha256=hashlib.sha256(claim.raw_predicate.encode("utf-8")).hexdigest(),
        authorized_canonical_relation_key_sha256=hashlib.sha256(relation_key.encode("utf-8")).hexdigest(),
    )


def _input(
    claim=None,
    *,
    endpoint_type_binding: EndpointTypeBindingV1 | None = None,
    ontology: FrozenOntologySnapshotV1 | None = None,
    decision: bool = False,
    decision_projection=None,
    authorization_relation_key: str = "supports",
    validated_at: datetime = CREATED_AT,
    verified_evidence_ref_ids: tuple[str, ...] = ("evidence-1",),
):
    claim = claim or _plain_claim()
    ontology = ontology or _ontology()
    assert claim.ontology_snapshot_hash == ontology.ontology_snapshot_hash
    attestations = {
        reference.ref_id: EvidenceValidationAttestationV1.for_reference(reference, validated_at=validated_at)
        for reference in claim.evidence_refs
    }
    binding = endpoint_type_binding or EndpointTypeBindingV1(
        source_type_key="source_type",
        target_type_key="target_type",
    )
    registry = _registry(claim, ontology, authorization_relation_key)
    values = {
        "frozen_ontology": ontology,
        "endpoint_type_binding": binding,
        "source_endpoint_resolution": _endpoint_resolution(claim, "source", binding.source_type_key),
        "target_endpoint_resolution": _endpoint_resolution(claim, "target", binding.target_type_key),
        "verified_evidence_ref_ids": verified_evidence_ref_ids,
        "evidence_attestations": attestations,
        "authorization_registry_snapshot": registry,
        "authorization_provenance": _authorization(claim, authorization_relation_key, registry),
    }
    if decision_projection is not None:
        values["decision"] = decision_projection
    elif decision:
        values["decision"] = build_claim_decision_projection(
            claim,
            decision_kind="mapping_candidate",
            reason_code="unknown_predicate",
            proposal=MappingCandidateProposalV1(
                raw_predicate=claim.raw_predicate,
                source_mention=claim.source_mention,
                target_mention=claim.target_mention,
                surface_direction=claim.surface_direction,
                evidence_ref_ids=[reference.ref_id for reference in claim.evidence_refs],
            ),
            decision_version=1,
            created_by_kind="system",
            producer_key="m0-decision-fixture",
            producer_version="m0-v1",
            created_at=CREATED_AT,
            id_namespace=CLAIM_DECISION_ID_NAMESPACE,
            extraction_occurrence_id=claim.extraction_occurrence_id,
        )
    return CanonicalMappingInputV1.from_raw_claim_json(claim.model_dump(mode="json"), **values)


def _mapper(**changes) -> MapperProvenanceV1:
    values = {
        "mapper_key": "fixture-mapper",
        "mapper_version": "m0-v1",
        "mapper_version_hash": HASH,
        "model_provider": "fixture-provider",
        "model_version_hash": HASH,
        "prompt_version": "mapping-prompt-v1",
        "prompt_content_hash": HASH,
        "config_version": "mapping-config-v1",
        "config_hash": HASH,
    }
    values.update(changes)
    return MapperProvenanceV1(**values)


def _raw_factory_kwargs(input_value: CanonicalMappingInputV1) -> dict:
    return {
        "frozen_ontology": input_value.frozen_ontology,
        "endpoint_type_binding": input_value.endpoint_type_binding,
        "source_endpoint_resolution": input_value.source_endpoint_resolution,
        "target_endpoint_resolution": input_value.target_endpoint_resolution,
        "authorization_registry_snapshot": input_value.authorization_registry_snapshot,
        "verified_evidence_ref_ids": input_value.verified_evidence_ref_ids,
        "evidence_attestations": {
            binding.evidence_ref_id: binding.attestation for binding in input_value.evidence_bindings
        },
        "authorization_provenance": input_value.authorization_provenance,
        "decision": input_value.decision,
    }


def _source_provenance(**changes) -> MappingSourceProvenanceV1:
    values = {
        "source_kind": "raw_claim",
        "source_key": "raw-claim-contract",
        "source_version": "raw-claim-v1",
        "source_hash": HASH,
    }
    values.update(changes)
    return MappingSourceProvenanceV1(**values)


def _actor_provenance(**changes) -> MappingActorProvenanceV1:
    values = {"actor_kind": "system", "actor_key": "m0-fixture-mapper"}
    values.update(changes)
    return MappingActorProvenanceV1(**values)


def _link(role: str, mention_local_id: str) -> EntityLinkDecisionV1:
    return EntityLinkDecisionV1(
        mention_local_id=mention_local_id,
        status="resolved",
        confidence=0.95,
        entity_id=SOURCE_ENTITY_ID if role == "source" else TARGET_ENTITY_ID,
        resolver_provenance=EntityLinkResolverProvenanceV1(
            resolver_key="fixture-resolver",
            resolver_version="v1",
            resolver_version_hash=HASH,
            config_hash=HASH,
        ),
    )


def _endpoint_resolution(claim, role: str, entity_type_key: str | None) -> EndpointResolutionAttestationV1 | None:
    if entity_type_key is None:
        return None
    mention = claim.source_mention if role == "source" else claim.target_mention
    return EndpointResolutionAttestationV1(
        mention_role=role,
        mention_local_id=mention.local_id,
        mention_surface_sha256=hashlib.sha256(mention.surface.encode("utf-8")).hexdigest(),
        entity_type_key=entity_type_key,
        entity_link=_link(role, mention.local_id),
        evidence_ref_ids=(mention.evidence_ref,),
        scope=_scope(claim),
    )


def _proposal(
    input_value: CanonicalMappingInputV1,
    *,
    relation_key: str = "supports",
    endpoint_transform: str = "identity",
    predicate_transform: str = "identity",
    direction: str | None = None,
) -> CanonicalMappingProposalV1:
    source_mention = input_value.source_mention
    target_mention = input_value.target_mention
    source_type = input_value.endpoint_type_binding.source_type_key
    target_type = input_value.endpoint_type_binding.target_type_key
    assert source_type is not None and target_type is not None
    if endpoint_transform == "identity":
        source_id, target_id = source_mention.local_id, target_mention.local_id
        source_endpoint_type, target_endpoint_type = source_type, target_type
    else:
        source_id, target_id = target_mention.local_id, source_mention.local_id
        source_endpoint_type, target_endpoint_type = target_type, source_type
    if direction is None:
        if predicate_transform == "identity":
            direction = input_value.surface_direction
        elif predicate_transform == "inverse":
            direction = {"source_to_target": "target_to_source", "target_to_source": "source_to_target"}[input_value.surface_direction]
        else:
            direction = "undirected"
    source_resolution = input_value.source_endpoint_resolution
    target_resolution = input_value.target_endpoint_resolution
    assert source_resolution is not None and target_resolution is not None
    authorization = input_value.authorization_provenance
    assert authorization is not None
    return CanonicalMappingProposalV1(
        canonical_relation_key=relation_key,
        canonical_direction=direction,
        canonical_source_endpoint=CanonicalEndpointV1(
            role="source",
            mention_local_id=source_id,
            entity_type_key=source_endpoint_type,
            entity_link=(target_resolution if endpoint_transform == "swap" else source_resolution).entity_link,
            resolution_attestation=target_resolution if endpoint_transform == "swap" else source_resolution,
        ),
        canonical_target_endpoint=CanonicalEndpointV1(
            role="target",
            mention_local_id=target_id,
            entity_type_key=target_endpoint_type,
            entity_link=(source_resolution if endpoint_transform == "swap" else target_resolution).entity_link,
            resolution_attestation=source_resolution if endpoint_transform == "swap" else target_resolution,
        ),
        endpoint_transform=endpoint_transform,
        predicate_transform=predicate_transform,
        authorization_provenance=authorization,
    )


def _build(
    input_value=None,
    *,
    outcome="mapped",
    reason_code=None,
    proposal=None,
    mapping_confidence=0.9,
    semantic_status="preserved",
    mapping_version=1,
    created_at=CREATED_AT,
    mapper=None,
    source_provenance=None,
    actor_provenance=None,
    remap_provenance=None,
    auto_proposal=True,
):
    input_value = input_value or _input()
    if proposal is None and outcome == "mapped" and auto_proposal:
        proposal = _proposal(input_value)
    return build_canonical_mapping(
        input_value,
        provenance=mapper or _mapper(),
        source_provenance=source_provenance or _source_provenance(),
        actor_provenance=actor_provenance or _actor_provenance(),
        outcome=outcome,
        created_at=created_at,
        mapping_version=mapping_version,
        proposal=proposal,
        reason_code=reason_code,
        mapping_confidence=mapping_confidence,
        semantic_status=semantic_status,
        remap_provenance=remap_provenance,
    )


def _clear_mapping_identity(payload):
    for field in (
        "mapping_attempt_id",
        "mapping_attempt_fingerprint",
        "mapping_result_id",
        "mapping_result_fingerprint",
    ):
        payload[field] = None
    return payload


def test_mapped_identity_inverse_and_symmetric_are_explicit():
    input_value = _input()
    identity = _build(input_value, proposal=_proposal(input_value))
    inverse = _build(
        input_value,
        proposal=_proposal(input_value, endpoint_transform="swap", predicate_transform="inverse"),
    )
    symmetric_input = _input(authorization_relation_key="symmetric_supports")
    symmetric = _build(
        symmetric_input,
        proposal=_proposal(
            symmetric_input,
            relation_key="symmetric_supports",
            predicate_transform="symmetric",
            direction="undirected",
        ),
    )

    assert (identity.endpoint_transform, identity.predicate_transform) == ("identity", "identity")
    assert (inverse.endpoint_transform, inverse.predicate_transform) == ("swap", "inverse")
    assert (symmetric.endpoint_transform, symmetric.predicate_transform) == ("identity", "symmetric")
    assert inverse.canonical_source_endpoint.mention_local_id == input_value.target_mention.local_id
    assert inverse.canonical_target_endpoint.mention_local_id == input_value.source_mention.local_id
    assert identity.mapping_result_fingerprint != inverse.mapping_result_fingerprint
    assert inverse.mapping_result_fingerprint != symmetric.mapping_result_fingerprint


@pytest.mark.parametrize(
    ("endpoint_transform", "predicate_transform", "direction", "expected_key"),
    [
        ("swap", "identity", "source_to_target", "endpoint_transform"),
        ("identity", "inverse", "target_to_source", "predicate_transform"),
    ],
)
def test_endpoint_swap_and_predicate_inverse_are_independent(endpoint_transform, predicate_transform, direction, expected_key):
    input_value = _input()
    result = _build(
        input_value,
        proposal=_proposal(
            input_value,
            endpoint_transform=endpoint_transform,
            predicate_transform=predicate_transform,
            direction=direction,
        ),
    )
    assert getattr(result, expected_key) == ("swap" if expected_key == "endpoint_transform" else "inverse")
    assert result.mapping_result_fingerprint


def test_unknown_direction_is_ambiguous_and_never_swaps_endpoints():
    claim = _plain_claim(surface_direction="unknown")
    input_value = _input(claim)
    result = _build(
        input_value,
        outcome="ambiguous",
        reason_code="unknown_direction",
        mapping_confidence=None,
        proposal=None,
        semantic_status="ambiguous",
    )
    assert result.canonical_direction is None
    assert result.canonical_source_endpoint is None
    assert result.canonical_target_endpoint is None
    assert result.source_evidence_ref_ids == ("evidence-1",)
    assert claim.source_mention.local_id == "source-1"
    with pytest.raises((ValidationError, ValueError), match="unknown|direction|canonical"):
        _build(input_value, proposal=None, outcome="mapped")


def test_unknown_predicate_and_endpoint_types_are_blocked():
    unknown_predicate = _input(_plain_claim(raw_predicate="not_in_frozen_ontology"))
    result = _build(
        unknown_predicate,
        outcome="blocked",
        reason_code="unknown_predicate",
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    assert result.reason_code == "unknown_predicate"

    unknown_source = _input(endpoint_type_binding=EndpointTypeBindingV1(source_type_key=None, target_type_key="target_type"))
    source_result = _build(
        unknown_source,
        outcome="blocked",
        reason_code="unknown_source_type",
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    assert source_result.reason_code == "unknown_source_type"

    unknown_target = _input(endpoint_type_binding=EndpointTypeBindingV1(source_type_key="source_type", target_type_key=None))
    target_result = _build(
        unknown_target,
        outcome="blocked",
        reason_code="unknown_target_type",
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    assert target_result.reason_code == "unknown_target_type"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"negation": {"value": True, "evidence_ref": "evidence-1"}}, "unsupported_negation"),
        ({"modality": {"value": "alleged", "evidence_ref": "evidence-1"}}, "unsupported_modality"),
        ({"qualifiers": [{"key": "status", "value": "alleged", "evidence_ref": "evidence-1"}]}, "unsupported_qualifier"),
        (
            {"valid_time": {"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-1"}},
            "unsupported_valid_time",
        ),
        ({"effective_time": {"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-1"}}, "unsupported_effective_time"),
    ],
)
def test_non_lossless_semantics_are_projected_and_blocked(changes, reason):
    claim = _plain_claim(**changes)
    input_value = _input(claim)
    projection = semantic_projection_from_claim(claim)
    assert projection.semantic_projection_fingerprint
    with pytest.raises(ValueError, match="semantics"):
        _build(input_value)
    result = _build(
        input_value,
        outcome="blocked",
        reason_code=reason,
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    assert result.reason_code == reason
    assert result.semantic_projection.semantic_projection_fingerprint
    ambiguous = _build(
        input_value,
        outcome="ambiguous",
        reason_code=reason,
        proposal=None,
        mapping_confidence=None,
        semantic_status="ambiguous",
    )
    assert ambiguous.outcome == "ambiguous"


def test_evidence_binding_is_typed_scoped_and_hash_bound():
    input_value = _input()
    binding = input_value.evidence_bindings[0]
    assert isinstance(binding, EvidenceValidationBindingV1)
    assert binding.binding_version == "evidence_validation_v1"
    assert binding.provenance_status == "verified"
    assert binding.stable_evidence_identity_hash

    payload = input_value.model_dump(mode="json")
    payload["evidence_bindings"][0]["stable_evidence_identity_hash"] = "c" * 64
    with pytest.raises(ValidationError, match="binding|identity"):
        CanonicalMappingInputV1.model_validate(payload)

    payload = input_value.model_dump(mode="json")
    payload["evidence_bindings"][0]["job_id"] = str(uuid.UUID("b5000000-0000-0000-0000-000000000001"))
    with pytest.raises(ValidationError, match="binding|scope"):
        CanonicalMappingInputV1.model_validate(payload)


def test_endpoint_resolution_evidence_must_be_in_verified_subset():
    first = _claim().evidence_refs[0].model_dump(mode="json")
    second_id = uuid.UUID("60000000-0000-0000-0000-000000000002")
    second_locator = _locator().model_copy(update={"unit_id": second_id, "ordinal": 1})
    second = {
        **first,
        "ref_id": "evidence-2",
        "evidence_id": str(second_id),
        "unit_id": str(second_id),
        "chunk_id": None,
        "block_id": None,
        "locator": second_locator.model_dump(mode="json"),
    }
    claim = _plain_claim(
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-2",
        },
        evidence_refs=[first, second],
    )
    with pytest.raises((ValidationError, ValueError), match="verified evidence subset"):
        _input(claim)

    verified = _input(claim, verified_evidence_ref_ids=("evidence-2", "evidence-1"))
    assert verified.verified_evidence_ref_ids == ("evidence-1", "evidence-2")
    assert verified.target_endpoint_resolution is not None
    assert verified.target_endpoint_resolution.evidence_ref_ids == ("evidence-2",)


def test_missing_or_unverified_locator_fails_closed():
    claim_payload = _plain_claim().model_dump(mode="json")
    claim_payload["evidence_refs"][0]["locator"] = None
    claim_payload["content_scoped_claim_fingerprint"] = None
    claim_payload["extraction_occurrence_fingerprint"] = None
    claim = type(_plain_claim()).model_validate(claim_payload)
    with pytest.raises(ValueError, match="attestation|verified locator"):
        _input(claim)


def test_ontology_id_hash_and_contract_mismatch_are_rejected():
    input_value = _input()
    payload = input_value.model_dump(mode="json")
    payload["ontology_snapshot_hash"] = "c" * 64
    with pytest.raises(ValidationError, match="ontology"):
        CanonicalMappingInputV1.model_validate(payload)

    payload = input_value.model_dump(mode="json")
    payload["ontology_contract_version"] = "different-contract"
    with pytest.raises(ValidationError, match="contract"):
        CanonicalMappingInputV1.model_validate(payload)


def test_schema_hashes_and_provenance_are_explicit():
    input_value = _input(decision=True)
    result = _build(input_value)
    assert input_value.mapping_schema_hash == MAPPING_SCHEMA_HASH
    assert input_value.canonical_schema_hash == CANONICAL_SCHEMA_HASH
    assert result.mapping_schema_hash == MAPPING_SCHEMA_HASH
    assert result.canonical_schema_hash == CANONICAL_SCHEMA_HASH
    assert result.ontology_contract_version == "ontology-contract-v1"
    assert result.decision_id == input_value.decision_id
    assert result.decision_fingerprint == input_value.decision_fingerprint
    assert result.source_provenance.source_kind == "raw_claim"
    assert result.actor_provenance.actor_key == "m0-fixture-mapper"

    payload = input_value.model_dump(mode="json")
    payload["mapping_schema_hash"] = "c" * 64
    with pytest.raises(ValidationError, match="schema"):
        CanonicalMappingInputV1.model_validate(payload)


def test_status_reason_confidence_matrix_is_closed():
    input_value = _input()
    with pytest.raises(ValueError, match="outcome|reason"):
        _build(input_value, outcome="ambiguous", reason_code="unknown_predicate", proposal=None, mapping_confidence=None)
    with pytest.raises(ValueError, match="outcome|reason"):
        _build(input_value, outcome="blocked", reason_code="ambiguous_mapping", proposal=None, mapping_confidence=None)
    with pytest.raises(ValueError, match="mapped_result_fields_invalid"):
        _build(input_value, proposal=_proposal(input_value), mapping_confidence=None)

    rejected = _build(
        input_value,
        outcome="rejected",
        reason_code="ontology_relation_not_allowed",
        proposal=None,
        mapping_confidence=None,
        semantic_status="preserved",
    )
    assert rejected.outcome == "rejected"


def test_authoritative_non_mapped_reason_and_semantic_status_are_input_bound():
    input_value = _input()
    with pytest.raises((ValidationError, ValueError), match="unknown|reason|authoritative"):
        _build(
            input_value,
            outcome="ambiguous",
            reason_code="unknown_direction",
            proposal=None,
            mapping_confidence=None,
            semantic_status="ambiguous",
        )

    valid = _build(
        input_value,
        outcome="ambiguous",
        reason_code="ambiguous_mapping",
        proposal=None,
        mapping_confidence=None,
        semantic_status="ambiguous",
    )
    forged_payload = valid.model_dump(mode="json")
    forged_payload["reason_code"] = "unknown_direction"
    forged_payload["semantic_status"] = "preserved"
    forged = CanonicalMappingV1.model_validate(_clear_mapping_identity(forged_payload))
    with pytest.raises((ValidationError, ValueError), match="authoritative|unknown|semantic"):
        canonical_mapping_json(input_value, forged)

    semantic_input = _input(_plain_claim(negation={"value": True, "evidence_ref": "evidence-1"}))
    semantic_result = _build(
        semantic_input,
        outcome="blocked",
        reason_code="unsupported_negation",
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    semantic_payload = semantic_result.model_dump(mode="json")
    semantic_payload["semantic_status"] = "preserved"
    semantic_forgery = CanonicalMappingV1.model_validate(_clear_mapping_identity(semantic_payload))
    with pytest.raises((ValidationError, ValueError), match="semantic|authoritative"):
        canonical_mapping_json(semantic_input, semantic_forgery)


def test_remap_provenance_is_closed_until_m3_predecessor_lookup():
    input_value = _input()
    original = _build(input_value)
    zero_generation = _build(
        input_value,
        mapping_version=2,
        remap_provenance=MappingRemapProvenanceV1(
            remap_generation=0,
            reason_code="mapper_refresh",
        ),
    )
    assert zero_generation.mapping_result_id != original.mapping_result_id
    for generation in (1, 2):
        with pytest.raises((ValidationError, ValueError), match="closed|remap"):
            MappingRemapProvenanceV1(remap_generation=generation, reason_code="mapper_refresh")


@pytest.mark.parametrize("generation", [0, 1])
def test_ontology_refresh_remap_is_closed_for_every_m3_generation(generation):
    with pytest.raises((ValidationError, ValueError), match="ontology_refresh|unsupported"):
        MappingRemapProvenanceV1(
            remap_generation=generation,
            reason_code="ontology_refresh",
        )


def test_remap_lineage_and_dangling_predecessors_are_closed_in_m0():
    for changes in (
        {"supersedes_mapping_result_id": uuid.uuid4()},
        {"lineage_root_mapping_result_id": uuid.uuid4()},
        {"remap_generation": 2},
    ):
        with pytest.raises((ValidationError, ValueError), match="closed|remap"):
            MappingRemapProvenanceV1(
                remap_generation=changes.pop("remap_generation", 1),
                reason_code="mapper_refresh",
                **changes,
            )


def test_attempt_and_result_identity_are_deterministic_and_created_at_is_excluded():
    input_value = _input()
    first = _build(input_value, created_at=CREATED_AT)
    second = _build(input_value, created_at=CREATED_AT + timedelta(days=1))
    assert first.mapping_attempt_fingerprint == second.mapping_attempt_fingerprint
    assert first.mapping_attempt_id == second.mapping_attempt_id
    assert first.mapping_result_fingerprint == second.mapping_result_fingerprint
    assert first.mapping_result_id == second.mapping_result_id
    assert canonical_mapping_attempt_fingerprint(input_value, provenance=_mapper()) == first.mapping_attempt_fingerprint
    assert canonical_mapping_json(input_value, first) != canonical_mapping_json(input_value, second)
    assert first.mapping_attempt_id.version == 5
    assert first.mapping_result_id.version == 5
    assert first.mapping_attempt_id != uuid.uuid5(CANONICAL_MAPPING_UUID_NAMESPACE, "unrelated")


def test_evidence_validation_time_is_audit_only_and_excluded_from_identity():
    first_input = _input(validated_at=CREATED_AT)
    second_input = _input(validated_at=CREATED_AT + timedelta(hours=1))
    first = _build(first_input)
    second = _build(second_input)
    assert first_input.evidence_bindings[0].attestation.validated_at != (
        second_input.evidence_bindings[0].attestation.validated_at
    )
    assert first_input.evidence_bindings[0].binding_fingerprint == second_input.evidence_bindings[0].binding_fingerprint
    assert first.mapping_attempt_fingerprint == second.mapping_attempt_fingerprint
    assert first.mapping_result_fingerprint == second.mapping_result_fingerprint
    assert first.mapping_result_id == second.mapping_result_id
    assert first.model_dump(mode="json")["evidence_bindings"] != second.model_dump(mode="json")["evidence_bindings"]


def test_occurrence_ontology_mapper_and_transform_changes_create_new_identity():
    base_input = _input()
    base = _build(base_input)
    changed_occurrence = _build(_input(_plain_claim(extraction_occurrence_id=uuid.UUID("a1000000-0000-0000-0000-000000000002"))))
    changed_mapper = _build(base_input, mapper=_mapper(mapper_version="m0-v2"))
    changed_snapshot = _ontology(contract_version="ontology-contract-v2")
    changed_ontology = _build(
        _input(
            _plain_claim(ontology_snapshot_hash=changed_snapshot.ontology_snapshot_hash),
            ontology=changed_snapshot,
        )
    )
    changed_endpoint = _build(
        base_input,
        proposal=_proposal(base_input, endpoint_transform="swap", predicate_transform="identity", direction="source_to_target"),
    )
    changed_predicate = _build(
        base_input,
        proposal=_proposal(base_input, endpoint_transform="identity", predicate_transform="inverse", direction="target_to_source"),
    )
    assert len({
        base.mapping_attempt_id,
        changed_occurrence.mapping_attempt_id,
        changed_mapper.mapping_attempt_id,
        changed_ontology.mapping_attempt_id,
    }) == 4
    assert changed_endpoint.mapping_result_id != base.mapping_result_id
    assert changed_predicate.mapping_result_id != base.mapping_result_id


def test_raw_claim_is_unchanged_and_serialized_output_has_no_raw_text_or_paths():
    claim = _plain_claim()
    before = claim.model_dump(mode="json")
    result = _build(_input(claim))
    assert claim.model_dump(mode="json") == before
    encoded = canonical_mapping_json(_input(claim), result)
    assert "Source entity" not in encoded
    assert "A supported statement." not in encoded
    assert "fixture.txt" not in encoded
    assert "storage_path" not in encoded


def test_extra_oversize_and_non_json_safe_values_are_rejected():
    with pytest.raises(ValidationError):
        MapperProvenanceV1(**{**_mapper().model_dump(), "extra": "forbidden"})
    with pytest.raises(ValidationError):
        MapperProvenanceV1(mapper_key="x" * 129, mapper_version="v1", mapper_version_hash=HASH, model_provider="p", model_version_hash=HASH, prompt_version="p", prompt_content_hash=HASH, config_version="c", config_hash=HASH)
    with pytest.raises(ValueError, match="JSON"):
        canonical_mapping_json_value({"not_safe": object()})
    with pytest.raises(ValueError, match="JSON"):
        canonical_mapping_json_value({"not_safe": float("nan")})
    with pytest.raises((ValidationError, ValueError), match="ontology|related_to"):
        related_input = _input(authorization_relation_key="related_to")
        _build(related_input, proposal=_proposal(related_input, relation_key="related_to"))
    with pytest.raises(ValidationError, match="resolver"):
        EntityLinkResolverProvenanceV1(
            resolver_key="../secret",
            resolver_version="v1",
            resolver_version_hash=HASH,
            config_hash=HASH,
        )


def test_canonical_json_is_order_independent():
    result = _build(_input())
    input_value = _input()
    result = _build(input_value)
    first = json.loads(canonical_mapping_json(input_value, result))
    reordered = {key: first[key] for key in reversed(tuple(first))}
    assert canonical_mapping_json(input_value, result) == canonical_mapping_json(
        input_value,
        type(result).model_validate(reordered),
    )


def test_contract_has_no_runtime_path_dependencies():
    path = Path(__file__).parents[1] / "app" / "schemas" / "canonical_mapping.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_prefixes = ("app.services", "app.models", "sqlalchemy", "httpx", "openai")
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not [name for name in imports if name.startswith(forbidden_prefixes)]


def test_result_model_validate_is_input_bound_and_serializers_revalidate_forgery():
    result = _build(_input())

    with pytest.raises(TypeError, match="authoritative|CanonicalMappingInputV1|missing"):
        canonical_mapping_json(result)

    payload = result.model_dump(mode="json")
    payload["canonical_source_endpoint"]["mention_local_id"] = "forged-mention"
    payload["canonical_source_endpoint"]["entity_link"]["mention_local_id"] = "forged-mention"
    with pytest.raises((ValidationError, ValueError), match="endpoint|snapshot|transform"):
        CanonicalMappingV1.model_validate(payload)

    payload = result.model_dump(mode="json")
    payload["evidence_bindings"][0]["quote_sha256"] = "c" * 64
    with pytest.raises((ValidationError, ValueError), match="evidence|claim"):
        CanonicalMappingV1.model_validate(payload)

    payload = result.model_dump(mode="json")
    payload["frozen_ontology"]["relation_type_keys"].append("forged_relation")
    with pytest.raises((ValidationError, ValueError), match="ontology|snapshot"):
        CanonicalMappingV1.model_validate(payload)

    payload = result.model_dump(mode="json")
    payload["surface_direction"] = "unknown"
    with pytest.raises((ValidationError, ValueError), match="direction|snapshot"):
        CanonicalMappingV1.model_validate(payload)

    forged = result.model_copy(update={"canonical_relation_key": "forged_relation"})
    with pytest.raises((ValidationError, ValueError), match="authorization|fingerprint|ontology"):
        canonical_mapping_json(_input(), forged)


@pytest.mark.parametrize(
    ("forged_field", "expected_error"),
    [
        ("claim_snapshot", "authoritative_claim_snapshot_mismatch"),
        ("evidence", "authoritative_claim_snapshot_mismatch"),
        ("evidence_locator", "authoritative_claim_snapshot_mismatch"),
        ("decision", "authoritative_decision_binding_mismatch"),
        ("ontology", "authoritative_frozen_ontology_mismatch|registry"),
        ("authorization", "authoritative_authorization_provenance_mismatch|registry"),
        ("scope", "authoritative_library_id_mismatch|registry"),
    ],
)
def test_authoritative_serializer_rejects_rebound_snapshot_and_scope_forgery(forged_field, expected_error):
    input_value = _input(decision=forged_field == "decision")
    result = _build(input_value)
    payload = result.model_dump(mode="json")
    if forged_field == "claim_snapshot":
        payload["claim_snapshot"]["claim_json_sha256"] = "c" * 64
        payload["claim_snapshot"]["snapshot_fingerprint"] = None
    elif forged_field == "evidence":
        payload["claim_snapshot"]["evidence_snapshots"][0]["quote_sha256"] = "c" * 64
        payload["claim_snapshot"]["evidence_snapshots"][0]["snapshot_fingerprint"] = None
        payload["claim_snapshot"]["snapshot_fingerprint"] = None
        payload["evidence_bindings"][0]["quote_sha256"] = "c" * 64
        payload["evidence_bindings"][0]["binding_fingerprint"] = None
    elif forged_field == "evidence_locator":
        payload["claim_snapshot"]["evidence_snapshots"][0]["locator_sha256"] = "c" * 64
        payload["claim_snapshot"]["evidence_snapshots"][0]["snapshot_fingerprint"] = None
        payload["claim_snapshot"]["snapshot_fingerprint"] = None
        payload["evidence_bindings"][0]["attestation"]["locator_sha256"] = "c" * 64
        payload["evidence_bindings"][0]["attestation"]["validation_fingerprint"] = None
        payload["evidence_bindings"][0]["binding_fingerprint"] = None
    elif forged_field == "decision":
        payload["decision_binding"]["decision_payload_sha256"] = "c" * 64
        payload["decision_binding"]["binding_fingerprint"] = None
    elif forged_field == "ontology":
        changed_ontology = _ontology(contract_version="forged-contract")
        payload["frozen_ontology"] = changed_ontology.model_dump(mode="json")
        payload["ontology_snapshot_hash"] = changed_ontology.ontology_snapshot_hash
        payload["ontology_contract_version"] = changed_ontology.ontology_contract_version
    elif forged_field == "authorization":
        forged_authorization = _authorization(input_value.claim).model_copy(update={"registry_hash": "c" * 64})
        authorization_payload = forged_authorization.model_dump(mode="json")
        authorization_payload["authorization_fingerprint"] = None
        payload["authorization_provenance"] = authorization_payload
    else:
        forged_library_id = str(uuid.UUID("b6000000-0000-0000-0000-000000000001"))
        payload["library_id"] = forged_library_id
        payload["claim_snapshot"]["library_id"] = forged_library_id
        payload["claim_snapshot"]["snapshot_fingerprint"] = None
        for binding in payload["evidence_bindings"]:
            binding["library_id"] = forged_library_id
            binding["binding_fingerprint"] = None
    _clear_mapping_identity(payload)

    with pytest.raises((ValidationError, ValueError), match=expected_error):
        canonical_mapping_json(input_value, payload)


def test_nested_model_copy_forgery_is_rejected_at_factory_boundary():
    input_value = _input()
    with pytest.raises((ValidationError, ValueError), match="nested|provenance|fingerprint"):
        _build(input_value, mapper=_mapper().model_copy(update={"mapper_version_hash": "c" * 64}))
    with pytest.raises((ValidationError, ValueError), match="nested|provenance|fingerprint"):
        _build(input_value, source_provenance=_source_provenance().model_copy(update={"source_hash": "c" * 64}))
    with pytest.raises((ValidationError, ValueError), match="nested|provenance|fingerprint"):
        _build(input_value, actor_provenance=_actor_provenance().model_copy(update={"actor_key": "forged"}))
    endpoint = _proposal(input_value).canonical_source_endpoint
    forged_endpoint = endpoint.model_copy(update={"role": "target"})
    forged_proposal = _proposal(input_value).model_copy(update={"canonical_source_endpoint": forged_endpoint})
    with pytest.raises((ValidationError, ValueError), match="nested|role|fingerprint"):
        _build(input_value, proposal=forged_proposal)


def test_raw_mapping_ingress_rejects_coercible_non_native_negation_booleans():
    payload = _input().model_dump(mode="json")
    for value in (0, 1, "false"):
        forged = json.loads(json.dumps(payload))
        forged["claim"]["negation"]["value"] = value
        with pytest.raises((ValidationError, ValueError), match="boolean|negation"):
            CanonicalMappingInputV1.model_validate(forged)
        with pytest.raises((TypeError, ValidationError, ValueError), match="boolean|negation|authoritative_input"):
            canonical_mapping_input_json(forged)


def test_authoritative_factory_requires_raw_json_and_accepts_native_boolean_only():
    input_value = _input()
    raw_payload = input_value.claim.model_dump(mode="json")
    for raw_value in (0, 1, "false", "true"):
        forged = json.loads(json.dumps(raw_payload))
        forged["negation"]["value"] = raw_value
        with pytest.raises((ValidationError, ValueError), match="boolean|negation"):
            CanonicalMappingInputV1.from_raw_claim_json(forged, **_raw_factory_kwargs(input_value))

    rebuilt = CanonicalMappingInputV1.from_raw_claim_json(
        json.dumps(raw_payload).encode("utf-8"),
        **_raw_factory_kwargs(input_value),
    )
    assert rebuilt.claim.negation.value is False
    with pytest.raises(TypeError, match="raw claim JSON"):
        CanonicalMappingInputV1.from_raw_claim_json(input_value.claim, **_raw_factory_kwargs(input_value))


def test_strict_raw_json_rejects_duplicate_keys_at_every_contract_object_level():
    input_value = _input()
    payload = input_value.claim.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    duplicate_payloads = [
        raw.replace(
            f'"claim_id":"{payload["claim_id"]}"',
            f'"claim_id":"{payload["claim_id"]}","claim_id":"{payload["claim_id"]}"',
            1,
        ),
        raw.replace(
            '"negation":{"evidence_ref":"evidence-1","value":false}',
            '"negation":{"evidence_ref":"evidence-1","value":false,"value":false}',
            1,
        ),
        raw.replace('"ordinal":0', '"ordinal":0,"ordinal":0', 1),
    ]
    qualifier_input = _input(
        _plain_claim(
            qualifiers=[
                {
                    "key": "nested",
                    "value": {"evidence_refs": {"flag": True}},
                    "evidence_ref": "evidence-1",
                }
            ]
        )
    )
    qualifier_raw = json.dumps(
        qualifier_input.claim.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    duplicate_payloads.append(
        qualifier_raw.replace(
            '"value":{"evidence_refs":{"flag":true}}',
            '"value":{"evidence_refs":{"flag":true,"flag":true}}',
            1,
        )
    )
    for index, duplicate in enumerate(duplicate_payloads):
        authority = input_value if index < 3 else qualifier_input
        raw_value = duplicate.encode("utf-8") if index == 0 else duplicate
        with pytest.raises(ValueError, match="duplicate object key"):
            CanonicalMappingInputV1.from_raw_claim_json(raw_value, **_raw_factory_kwargs(authority))


def test_strict_numeric_scan_does_not_inspect_qualifier_json_keys():
    claim = _plain_claim(
        qualifiers=[
            {
                "key": "dynamic",
                "value": {
                    "evidence_refs": True,
                    "source_span": {"revision_no": False},
                    "revision_no": True,
                },
                "evidence_ref": "evidence-1",
            }
        ]
    )
    input_value = _input(claim)
    assert input_value.claim.qualifiers[0].value["revision_no"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("revision_no", True),
        lambda payload: payload["evidence_refs"][0].__setitem__("revision_no", True),
        lambda payload: payload["evidence_refs"][0]["locator"].__setitem__("ordinal", True),
    ],
)
def test_authoritative_raw_ingress_rejects_coercible_evidence_numeric_booleans(mutate):
    payload = json.loads(_input().raw_claim_authority_json)
    mutate(payload)
    with pytest.raises((ValidationError, ValueError), match="native JSON integer|revision_no|ordinal"):
        CanonicalMappingInputV1.from_raw_claim_json(payload, **_raw_factory_kwargs(_input()))


def test_raw_authority_preserves_precoercion_payload_and_binds_hash_and_identity():
    input_value = _input()
    assert json.loads(input_value.raw_claim_authority_json) == input_value.claim.model_dump(mode="json")
    assert input_value.raw_claim_authority_sha256 == hashlib.sha256(
        input_value.raw_claim_authority_json.encode("utf-8")
    ).hexdigest()
    result = _build(input_value)
    assert result.raw_claim_authority_sha256 == input_value.raw_claim_authority_sha256

    raw_payload = json.loads(input_value.raw_claim_authority_json)
    for field, raw_value in (("negation", "false"), ("revision_no", True)):
        forged_payload = json.loads(json.dumps(raw_payload))
        if field == "negation":
            forged_payload["negation"]["value"] = raw_value
        else:
            forged_payload[field] = raw_value
        coerced_claim = RawClaimV1.model_validate(forged_payload)
        forged_input = input_value.model_copy(update={"claim": coerced_claim})
        with pytest.raises((ValidationError, ValueError), match="authoritative|claim"):
            canonical_mapping_input_json(forged_input)
        with pytest.raises((ValidationError, ValueError), match="authoritative|claim"):
            canonical_mapping_json(forged_input, result)
        with pytest.raises((ValidationError, ValueError), match="authoritative|claim"):
            _build(forged_input)


def test_authoritative_input_serializer_rejects_mapping_and_rebound_authority():
    input_value = _input()
    with pytest.raises(TypeError, match="CanonicalMappingInputV1"):
        canonical_mapping_input_json(input_value.model_dump(mode="json"))

    changed_claim = _plain_claim(raw_predicate="new-surface")
    changed_raw = changed_claim.model_dump(mode="json")
    forged_authority = input_value.model_copy(
        update={
            "raw_claim_authority_json": json.dumps(changed_raw),
            "raw_claim_authority_sha256": hashlib.sha256(
                json.dumps(changed_raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
    )
    with pytest.raises((ValidationError, ValueError), match="authority|canonical|claim"):
        canonical_mapping_input_json(forged_authority)


def test_mapping_authorization_registry_manifest_is_order_independent_and_conflict_free():
    input_value = _input()
    base = input_value.authorization_registry_snapshot.entries[0]
    other = MappingAuthorizationRegistryEntryV1(
        authorization_key="other-registry",
        authorization_version="m0-v1",
        surface_predicate_sha256=hashlib.sha256(b"other-surface").hexdigest(),
        canonical_relation_key_sha256=hashlib.sha256(b"supports").hexdigest(),
        allowed_endpoint_transforms=("identity",),
        allowed_predicate_transforms=("identity",),
        allowed_canonical_directions=("source_to_target",),
        allowed_source_type_keys=("source_type",),
        allowed_target_type_keys=("target_type",),
    )
    first = MappingAuthorizationRegistrySnapshotV1.from_content(
        scope=_scope(input_value.claim),
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        entries=[base, other],
    )
    second = MappingAuthorizationRegistrySnapshotV1.from_content(
        scope=_scope(input_value.claim),
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        entries=[other, base],
    )
    assert first.registry_snapshot_hash == second.registry_snapshot_hash
    assert first.model_dump(mode="json") == second.model_dump(mode="json")

    conflicting = base.model_dump(mode="json")
    conflicting["allowed_endpoint_transforms"] = ["identity"]
    conflicting["entry_fingerprint"] = None
    with pytest.raises((ValidationError, ValueError), match="duplicate|conflicting|policy identity"):
        MappingAuthorizationRegistrySnapshotV1.from_content(
            scope=_scope(input_value.claim),
            ontology_snapshot_hash=input_value.ontology_snapshot_hash,
            entries=[base, conflicting],
        )


def test_trusted_boundaries_reject_structural_model_validate_and_model_copy_claim_forgery():
    input_value = _input()
    structural = CanonicalMappingInputV1.model_validate(input_value.model_dump(mode="json"))
    with pytest.raises(TypeError, match="raw claim JSON"):
        canonical_mapping_input_json(structural)
    with pytest.raises(TypeError, match="raw claim JSON"):
        canonical_mapping_json(structural, _build(input_value))

    forged_claim = _plain_claim(raw_predicate="forged-surface")
    forged_input = input_value.model_copy(update={"claim": forged_claim})
    with pytest.raises((ValidationError, ValueError), match="authoritative_raw_claim|claim"):
        canonical_mapping_input_json(forged_input)
    with pytest.raises((ValidationError, ValueError), match="authoritative_raw_claim|claim"):
        _build(forged_input)


def test_semantic_projection_rebuilds_raw_claim_before_projection():
    claim = _plain_claim(qualifiers=[{"key": "nested", "value": {"x": "before"}, "evidence_ref": "evidence-1"}])
    projection = semantic_projection_from_claim(claim)
    claim.qualifiers[0].value["x"] = "after"
    with pytest.raises((ValidationError, ValueError), match="fingerprint|claim"):
        semantic_projection_from_claim(claim)
    assert projection.semantic_projection_fingerprint


def test_nested_time_normalization_reaches_mapping_and_replay_projections():
    offset_claim = _plain_claim(
        valid_time={
            "start": "2026-01-01T08:00:00+08:00",
            "end": "2026-12-31T08:00:00+08:00",
            "evidence_ref": "evidence-1",
        }
    )
    utc_claim = _plain_claim(
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "evidence-1",
        }
    )

    assert offset_claim.valid_time == utc_claim.valid_time
    assert semantic_projection_from_claim(offset_claim).valid_time_hash == semantic_projection_from_claim(
        utc_claim
    ).valid_time_hash
    assert redact_raw_claim_v2(offset_claim).valid_time == redact_raw_claim_v2(utc_claim).valid_time


def test_schema_extension_decision_is_retained_but_can_never_map():
    claim = _plain_claim()
    decision = build_claim_decision_projection(
        claim,
        decision_kind="schema_extension_candidate",
        reason_code="unknown_source_type",
        proposal=SchemaExtensionCandidateProposalV1(
            raw_predicate=claim.raw_predicate,
            source_endpoint=DecisionEndpointV1(
                local_id=claim.source_mention.local_id,
                surface=claim.source_mention.surface,
                entity_type_hint=claim.source_mention.entity_type_hint,
                evidence_ref_ids=[claim.source_mention.evidence_ref],
            ),
            target_endpoint=DecisionEndpointV1(
                local_id=claim.target_mention.local_id,
                surface=claim.target_mention.surface,
                entity_type_hint=claim.target_mention.entity_type_hint,
                evidence_ref_ids=[claim.target_mention.evidence_ref],
            ),
            surface_direction=claim.surface_direction,
            evidence_ref_ids=[reference.ref_id for reference in claim.evidence_refs],
        ),
        decision_version=1,
        created_by_kind="system",
        producer_key="schema-extension-fixture",
        producer_version="m0-v1",
        created_at=CREATED_AT,
        id_namespace=CLAIM_DECISION_ID_NAMESPACE,
        extraction_occurrence_id=claim.extraction_occurrence_id,
    )
    input_value = _input(
        claim,
        endpoint_type_binding=EndpointTypeBindingV1(source_type_key=None, target_type_key="target_type"),
        decision_projection=decision,
    )
    blocked = _build(
        input_value,
        outcome="blocked",
        reason_code="unknown_source_type",
        proposal=None,
        mapping_confidence=None,
        semantic_status="blocked",
    )
    assert blocked.outcome == "blocked"
    with pytest.raises((ValidationError, ValueError), match="schema_extension|mapped"):
        _build(input_value, auto_proposal=False)

    forged_mapped = _build(_input())
    forged_payload = forged_mapped.model_dump(mode="json")
    forged_payload["decision_id"] = str(input_value.decision_id)
    forged_payload["decision_fingerprint"] = input_value.decision_fingerprint
    forged_payload["decision_kind"] = "schema_extension_candidate"
    forged_payload["decision_binding"] = input_value.decision_binding.model_dump(mode="json")
    _clear_mapping_identity(forged_payload)
    with pytest.raises((ValidationError, ValueError), match="schema_extension|mapped"):
        canonical_mapping_json(input_value, forged_payload)


def test_related_to_requires_explicit_ontology_and_authorization():
    base = _ontology()
    constraints = [item.model_dump(mode="json") for item in base.constraints]
    constraints.extend(
        [
            {
                "relation_key": "related_to",
                "source_type_key": "source_type",
                "target_type_key": "target_type",
                "direction": "source_to_target",
            }
        ]
    )
    ontology = FrozenOntologySnapshotV1.from_content(
        ontology_version_id=ONTOLOGY_ID,
        ontology_contract_version="ontology-contract-related-v1",
        entity_type_keys=base.entity_type_keys,
        relation_type_keys=(*base.relation_type_keys, "related_to"),
        constraints=constraints,
    )
    claim = _plain_claim(ontology_snapshot_hash=ontology.ontology_snapshot_hash)
    input_value = _input(claim, ontology=ontology, authorization_relation_key="related_to")
    result = _build(input_value, proposal=_proposal(input_value, relation_key="related_to"))
    assert result.canonical_relation_key == "related_to"


def test_semantic_projection_and_nested_claim_values_are_revalidated():
    claim = _plain_claim(
        qualifiers=[{"key": "nested", "value": {"level": {"value": "original"}}, "evidence_ref": "evidence-1"}]
    )
    input_value = _input(claim)
    input_value.claim.qualifiers[0].value["level"]["value"] = "mutated"
    with pytest.raises((ValidationError, ValueError), match="fingerprint|RawClaim|claim"):
        canonical_mapping_input_json(input_value)

    projection_payload = semantic_projection_from_claim(_plain_claim()).model_dump(mode="json")
    for value in (0, 1, "false"):
        projection_payload["negation_value"] = value
        projection_payload["semantic_projection_fingerprint"] = None
        with pytest.raises(ValidationError):
            CanonicalSemanticProjectionV1.model_validate(projection_payload)


def test_decision_binding_requires_the_complete_projection_and_production_identity():
    input_value = _input(decision=True)
    assert input_value.decision is not None
    assert input_value.decision_binding is not None
    payload = input_value.model_dump(mode="json")
    payload["decision"]["decision_id"] = str(uuid.uuid4())
    payload["decision"]["decision_fingerprint"] = None
    with pytest.raises((ValidationError, ValueError), match="decision"):
        CanonicalMappingInputV1.model_validate(payload)

    payload = input_value.model_dump(mode="json")
    payload["decision_id"] = str(uuid.uuid4())
    with pytest.raises((ValidationError, ValueError), match="decision"):
        CanonicalMappingInputV1.model_validate(payload)

    payload = input_value.model_dump(mode="json")
    payload["decision_kind"] = "schema_extension_candidate"
    with pytest.raises((ValidationError, ValueError), match="decision"):
        CanonicalMappingInputV1.model_validate(payload)

    payload = input_value.model_dump(mode="json")
    payload["decision_binding"]["decision_fingerprint"] = "a" * 64
    with pytest.raises((ValidationError, ValueError), match="decision"):
        CanonicalMappingInputV1.model_validate(payload)


def test_authorization_is_typed_and_enters_mapping_identity():
    input_value = _input()
    proposal = _proposal(input_value)
    forged_authorization = _authorization(input_value.claim)
    forged_authorization = forged_authorization.model_copy(update={"registry_hash": "c" * 64})
    forged_proposal = proposal.model_copy(update={"authorization_provenance": forged_authorization})
    with pytest.raises((ValidationError, ValueError), match="authorization|nested"):
        _build(input_value, proposal=forged_proposal)

    changed = _build(
        _input(authorization_relation_key="supports"),
        mapper=_mapper(mapper_version="m0-v2", mapper_algorithm_version="m0-v2"),
    )
    assert changed.mapping_attempt_fingerprint != _build(input_value).mapping_attempt_fingerprint


def test_authorization_requires_content_bound_registry_membership():
    input_value = _input()
    relation_hash = hashlib.sha256(b"other-relation").hexdigest()
    registry = MappingAuthorizationRegistrySnapshotV1.from_content(
        scope=_scope(input_value.claim),
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        entries=[
            MappingAuthorizationRegistryEntryV1(
                authorization_key="fixture-registry",
                authorization_version="m0-v1",
                surface_predicate_sha256=hashlib.sha256(input_value.surface_raw_predicate.encode("utf-8")).hexdigest(),
                canonical_relation_key_sha256=relation_hash,
                allowed_endpoint_transforms=("identity",),
                allowed_predicate_transforms=("identity",),
                allowed_canonical_directions=("source_to_target",),
                allowed_source_type_keys=("source_type",),
                allowed_target_type_keys=("target_type",),
            )
        ],
    )
    forged_input = CanonicalMappingInputV1.from_raw_claim_json(
        input_value.claim.model_dump(mode="json"),
        **{
            **_raw_factory_kwargs(input_value),
            "authorization_registry_snapshot": registry,
            "authorization_provenance": MappingAuthorizationProvenanceV1(
                authorization_key="fixture-registry",
                authorization_version="m0-v1",
                registry_snapshot=registry,
                registry_hash=registry.registry_snapshot_hash,
                authorized_surface_predicate_sha256=hashlib.sha256(
                    input_value.surface_raw_predicate.encode("utf-8")
                ).hexdigest(),
                authorized_canonical_relation_key_sha256=hashlib.sha256(b"supports").hexdigest(),
            ),
        },
    )
    with pytest.raises((ValidationError, ValueError), match="registry|authorization"):
        _build(forged_input)


def test_schema_hashes_are_generated_from_actual_frozen_json_schema():
    for model, expected in (
        (CanonicalMappingInputV1, MAPPING_SCHEMA_HASH),
        (CanonicalMappingV1, CANONICAL_SCHEMA_HASH),
    ):
        encoded = json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert hashlib.sha256(encoded).hexdigest() == expected


def test_endpoint_entity_link_subject_cannot_be_forged_after_endpoint_swap():
    endpoint = _proposal(_input()).canonical_source_endpoint
    forged_link = endpoint.entity_link.model_copy(update={"mention_local_id": "other-mention"})
    forged_endpoint = endpoint.model_copy(update={"entity_link": forged_link})
    with pytest.raises((ValidationError, ValueError), match="entity link|mention"):
        CanonicalEndpointV1.model_validate(forged_endpoint.model_dump(mode="json"))


def test_authoritative_serializer_rejects_recomputed_entity_link_forgery():
    input_value = _input()
    result = _build(input_value)
    payload = result.model_dump(mode="json")
    forged_link = payload["canonical_source_endpoint"]["entity_link"]
    forged_link["entity_id"] = str(uuid.UUID("b4000000-0000-0000-0000-000000000001"))
    forged_link["link_decision_fingerprint"] = None
    forged_attestation = payload["canonical_source_endpoint"]["resolution_attestation"]
    forged_attestation["entity_link"] = forged_link
    forged_attestation["attestation_fingerprint"] = None
    payload["canonical_source_endpoint"]["endpoint_fingerprint"] = None
    payload["source_endpoint_resolution"] = forged_attestation
    _clear_mapping_identity(payload)
    with pytest.raises((ValidationError, ValueError), match="authoritative|endpoint|entity|resolution"):
        canonical_mapping_json(input_value, payload)


def _imports_canonical_mapping(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        if node.module == "app.schemas.canonical_mapping":
            return True
        return node.module == "app.schemas" and any(
            alias.name == "canonical_mapping" for alias in node.names
        )
    if isinstance(node, ast.Import):
        return any(alias.name == "app.schemas.canonical_mapping" for alias in node.names)
    return False


@pytest.mark.parametrize(
    "source",
    [
        "from app.schemas import canonical_mapping",
        "from app.schemas import canonical_mapping as mapping_contract",
        "import app.schemas.canonical_mapping",
        "import app.schemas.canonical_mapping as mapping_contract",
        "from app.schemas.canonical_mapping import CanonicalMappingInputV1",
    ],
)
def test_caller_guard_detects_all_canonical_mapping_import_shapes(source):
    tree = ast.parse(source)
    assert any(_imports_canonical_mapping(node) for node in ast.walk(tree))


def test_repository_has_no_production_canonical_mapping_callers():
    root = Path(__file__).parents[1]
    callers: list[str] = []
    for scan_root in (root / "app", root / "tests"):
        for path in scan_root.rglob("*.py"):
            if path.name == "canonical_mapping.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if _imports_canonical_mapping(node):
                    callers.append(str(path))
    allowed_callers = {
        str(root / "app" / "services" / "canonical_mapping_builder.py"),
        str(root / "app" / "services" / "canonical_mapping_comparison.py"),
        str(root / "app" / "services" / "canonical_mapping_persistence.py"),
        str(root / "app" / "services" / "canonical_mapping_quarantine.py"),
        str(root / "app" / "services" / "canonical_mapping_rollout.py"),
        str(root / "app" / "services" / "canonical_mapping_shadow.py"),
        str(root / "tests" / "test_canonical_mapping.py"),
        str(root / "tests" / "test_canonical_mapping_builder.py"),
        str(root / "tests" / "test_canonical_mapping_quarantine.py"),
        str(root / "tests" / "test_strict_datetime_ingress.py"),
        str(root / "tests" / "test_canonical_mapping_comparison.py"),
        str(root / "tests" / "test_canonical_mapping_persistence.py"),
        str(root / "tests" / "test_canonical_mapping_persistence_pg.py"),
    }
    assert set(callers) == allowed_callers


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_contract_is_domain_neutral_without_an_allowlist(domain):
    claim = _plain_claim(raw_predicate=f"{domain}_surface_predicate")
    result = _build(_input(claim))
    assert result.outcome == "mapped"
