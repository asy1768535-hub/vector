from __future__ import annotations

import ast
import uuid
from datetime import timedelta
from typing import Any
from pathlib import Path

import pytest

from app.schemas.canonical_mapping import (
    CLAIM_DECISION_ID_NAMESPACE,
    CanonicalMappingContractError,
    CanonicalMappingInputV1,
    EndpointResolutionAttestationV1,
    EndpointTypeBindingV1,
    EntityLinkDecisionV1,
)
from app.schemas.claim_decision import (
    DecisionEndpointV1,
    SchemaExtensionCandidateProposalV1,
)
import app.services.canonical_mapping_builder as builder_module
from app.services.canonical_mapping_builder import (
    CanonicalMappingBuilderError,
    CanonicalMappingBuilderErrorCode,
    CanonicalMappingBuilderStage,
    build_canonical_mapping,
)
from app.services.claim_decision_builder import build_claim_decision_projection
from tests.test_canonical_mapping import (
    CREATED_AT,
    HASH,
    _actor_provenance,
    _input,
    _mapper,
    _ontology,
    _plain_claim,
    _proposal,
    _raw_factory_kwargs,
    _source_provenance,
)


class _FloatSubclass(float):
    pass


def _build(input_value, *, proposal=None, created_at=CREATED_AT, first_created_at=None, **changes: Any):
    values = {
        "provenance": _mapper(),
        "source_provenance": _source_provenance(),
        "actor_provenance": _actor_provenance(),
        "created_at": created_at,
        "mapping_confidence": 0.9 if proposal is not None else None,
        "proposal": proposal,
    }
    values.update(changes)
    return build_canonical_mapping(input_value, first_created_at=first_created_at, **values)


def _candidate_endpoint_input() -> CanonicalMappingInputV1:
    base = _input()
    resolution = base.source_endpoint_resolution
    assert resolution is not None
    link_payload = resolution.entity_link.model_dump(mode="json")
    link_payload["status"] = "candidate"
    link_payload["entity_id"] = None
    link_payload["entity_candidate_key_hash"] = HASH
    link_payload["link_decision_fingerprint"] = None
    candidate_link = EntityLinkDecisionV1.model_validate(link_payload)
    resolution_payload = resolution.model_dump(mode="json")
    resolution_payload["entity_link"] = candidate_link.model_dump(mode="json")
    resolution_payload["attestation_fingerprint"] = None
    candidate_resolution = EndpointResolutionAttestationV1.model_validate(resolution_payload)
    kwargs = _raw_factory_kwargs(base)
    kwargs["source_endpoint_resolution"] = candidate_resolution
    return CanonicalMappingInputV1.from_raw_claim_json(
        base.raw_claim_authority_json,
        **kwargs,
    )


def _schema_extension_input() -> CanonicalMappingInputV1:
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
        producer_key="m1-schema-extension-fixture",
        producer_version="m1-v1",
        created_at=CREATED_AT,
        id_namespace=CLAIM_DECISION_ID_NAMESPACE,
        extraction_occurrence_id=claim.extraction_occurrence_id,
    )
    return _input(
        claim,
        endpoint_type_binding=EndpointTypeBindingV1(
            source_type_key=None,
            target_type_key="target_type",
        ),
        decision_projection=decision,
    )


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_explicit_mapping_is_domain_neutral_and_evidence_bound(domain):
    claim = _plain_claim(raw_predicate=f"{domain}_supports")
    input_value = _input(claim)
    result = _build(input_value, proposal=_proposal(input_value))

    assert result.outcome == "mapped"
    assert result.canonical_relation_key == "supports"
    assert result.source_evidence_ref_ids == (claim.source_mention.evidence_ref,)
    assert result.target_evidence_ref_ids == (claim.target_mention.evidence_ref,)
    assert result.mapping_evidence_ref_ids == ("evidence-1",)


def test_builder_requires_explicit_proposal_and_returns_stable_failure():
    result = _build(_input(), proposal=None)
    assert (result.outcome, result.reason_code, result.semantic_status) == (
        "blocked",
        "no_explicit_mapping",
        "blocked",
    )


@pytest.mark.parametrize(
    ("input_value", "outcome", "reason"),
    [
        (_input(_plain_claim(raw_predicate="not_in_frozen_ontology")), "blocked", "unknown_predicate"),
        (_input(_plain_claim(surface_direction="unknown")), "ambiguous", "unknown_direction"),
        (
            _input(endpoint_type_binding=EndpointTypeBindingV1(source_type_key=None, target_type_key="target_type")),
            "blocked",
            "unknown_source_type",
        ),
        (
            _input(endpoint_type_binding=EndpointTypeBindingV1(source_type_key="source_type", target_type_key=None)),
            "blocked",
            "unknown_target_type",
        ),
    ],
)
def test_unknown_mapping_facts_fail_closed_with_stable_result(input_value, outcome, reason):
    result = _build(input_value, proposal=None)
    assert (result.outcome, result.reason_code) == (outcome, reason)
    assert result.canonical_relation_key is None
    assert result.canonical_source_endpoint is None
    assert result.canonical_target_endpoint is None


def test_ambiguous_endpoint_never_becomes_mapped():
    input_value = _candidate_endpoint_input()
    result = _build(input_value, proposal=_proposal(input_value), mapping_confidence=None)
    assert (result.outcome, result.reason_code) == ("ambiguous", "ambiguous_endpoint")
    assert result.canonical_relation_key is None

    explicit_confidence = _build(input_value, proposal=_proposal(input_value), mapping_confidence=0.4)
    assert explicit_confidence.mapping_confidence == 0.4


def test_schema_extension_decision_cannot_map():
    input_value = _schema_extension_input()
    result = _build(input_value, proposal=None)
    assert (result.outcome, result.reason_code) == ("blocked", "unknown_source_type")


def test_unknown_type_and_direction_facts_precede_endpoint_ambiguity():
    known_input = _input()
    known_proposal = _proposal(known_input)

    unknown_source = _input(
        endpoint_type_binding=EndpointTypeBindingV1(
            source_type_key=None,
            target_type_key="target_type",
        )
    )
    source_result = _build(unknown_source, proposal=known_proposal, mapping_confidence=None)
    assert (source_result.outcome, source_result.reason_code) == ("blocked", "unknown_source_type")

    unknown_target = _input(
        endpoint_type_binding=EndpointTypeBindingV1(
            source_type_key="source_type",
            target_type_key=None,
        )
    )
    target_result = _build(unknown_target, proposal=known_proposal, mapping_confidence=None)
    assert (target_result.outcome, target_result.reason_code) == ("blocked", "unknown_target_type")

    unknown_direction = _input(_plain_claim(surface_direction="unknown"))
    direction_result = _build(unknown_direction, proposal=known_proposal, mapping_confidence=None)
    assert (direction_result.outcome, direction_result.reason_code) == ("ambiguous", "unknown_direction")

    unknown_predicate = _input(_plain_claim(raw_predicate="not_in_frozen_ontology"))
    predicate_result = _build(unknown_predicate, proposal=None)
    assert (predicate_result.outcome, predicate_result.reason_code) == ("blocked", "unknown_predicate")


def test_invalid_authority_is_rejected_without_sensitive_error_text():
    base = _input()
    forged_scope = base.model_copy(update={"library_id": uuid.uuid4()})
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(forged_scope, proposal=_proposal(base))
    assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
    assert error.value.stage is CanonicalMappingBuilderStage.INPUT_AUTHORITY
    assert str(error.value) == "mapper_error"
    assert error.value.__cause__ is not None

    forged_hash = base.model_copy(update={"raw_claim_authority_sha256": "b" * 64})
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(forged_hash, proposal=_proposal(base))
    assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
    assert error.value.stage is CanonicalMappingBuilderStage.INPUT_AUTHORITY

    forged_binding = base.evidence_bindings[0].model_copy(update={"job_id": uuid.uuid4()})
    forged_evidence = base.model_copy(update={"evidence_bindings": (forged_binding,)})
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(forged_evidence, proposal=_proposal(base))
    assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
    assert error.value.stage is CanonicalMappingBuilderStage.INPUT_AUTHORITY
    assert str(error.value) == "mapper_error"


def test_invalid_explicit_proposal_is_a_typed_mapper_error():
    input_value = _input()
    proposal = _proposal(input_value)
    forged_endpoint = proposal.canonical_source_endpoint.model_copy(
        update={"mention_local_id": "forged", "endpoint_fingerprint": None}
    )
    forged_proposal = proposal.model_copy(
        update={"canonical_source_endpoint": forged_endpoint, "proposal_fingerprint": None}
    )
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(input_value, proposal=forged_proposal)
    assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
    assert error.value.stage is CanonicalMappingBuilderStage.FACTORY
    assert str(error.value) == "mapper_error"


@pytest.mark.parametrize(
    "confidence",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(10**10000, id="huge-int"),
        pytest.param(_FloatSubclass(0.5), id="float-subclass"),
        pytest.param("0.9", id="string"),
        pytest.param(True, id="bool"),
    ],
)
def test_invalid_confidence_is_a_typed_argument_error(confidence):
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(_input(), proposal=_proposal(_input()), mapping_confidence=confidence)
    assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
    assert error.value.stage is CanonicalMappingBuilderStage.ARGUMENTS
    assert str(error.value) == "mapper_error"


def test_confidence_is_checked_against_the_typed_outcome():
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(_input(), proposal=_proposal(_input()), mapping_confidence=None)
    assert error.value.stage is CanonicalMappingBuilderStage.ARGUMENTS

    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(_input(), proposal=None, mapping_confidence=0.2)
    assert error.value.stage is CanonicalMappingBuilderStage.ARGUMENTS

    blocked = _build(_input(), proposal=None, mapping_confidence=None)
    assert blocked.outcome == "blocked"
    assert blocked.mapping_confidence is None


def test_dict_proposal_and_forged_provenance_are_typed_mapper_errors():
    input_value = _input()
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(input_value, proposal={})
    assert error.value.stage is CanonicalMappingBuilderStage.ARGUMENTS

    for kwargs in (
        {"provenance": _mapper().model_copy(update={"mapper_version_hash": "b" * 64})},
        {"source_provenance": _source_provenance().model_copy(update={"source_hash": "b" * 64})},
        {"actor_provenance": _actor_provenance().model_copy(update={"actor_key": "forged"})},
    ):
        with pytest.raises(CanonicalMappingBuilderError) as error:
            _build(input_value, proposal=_proposal(input_value), **kwargs)
        assert error.value.code is CanonicalMappingBuilderErrorCode.MAPPER_ERROR
        assert error.value.stage is CanonicalMappingBuilderStage.FACTORY


def test_m0_factory_and_result_serializer_failures_are_not_downgraded(monkeypatch):
    input_value = _input()
    proposal = _proposal(input_value)

    def raise_contract(*args, **kwargs):
        raise CanonicalMappingContractError("sensitive contract detail")

    monkeypatch.setattr(builder_module, "build_contract_mapping", raise_contract)
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(input_value, proposal=proposal)
    assert error.value.stage is CanonicalMappingBuilderStage.FACTORY
    assert str(error.value) == "mapper_error"
    assert error.value.__cause__ is not None

    monkeypatch.undo()

    def raise_serializer(*args, **kwargs):
        raise ValueError("sensitive serializer detail")

    monkeypatch.setattr(builder_module, "canonical_mapping_json", raise_serializer)
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(input_value, proposal=proposal)
    assert error.value.stage is CanonicalMappingBuilderStage.RESULT_SERIALIZER
    assert str(error.value) == "mapper_error"


def test_failure_result_construction_errors_are_not_swallowed(monkeypatch):
    def raise_contract(*args, **kwargs):
        raise CanonicalMappingContractError("sensitive failure result detail")

    monkeypatch.setattr(builder_module, "build_contract_mapping", raise_contract)
    with pytest.raises(CanonicalMappingBuilderError) as error:
        _build(_input(), proposal=None)
    assert error.value.stage is CanonicalMappingBuilderStage.FACTORY
    assert str(error.value) == "mapper_error"


def test_swap_and_inverse_are_explicit_and_raw_claim_is_unchanged():
    input_value = _input()
    before = input_value.claim.model_dump(mode="json")
    result = _build(
        input_value,
        proposal=_proposal(
            input_value,
            endpoint_transform="swap",
            predicate_transform="inverse",
        ),
    )
    assert result.outcome == "mapped"
    assert (result.endpoint_transform, result.predicate_transform) == ("swap", "inverse")
    assert result.canonical_source_endpoint.mention_local_id == input_value.target_mention.local_id
    assert result.canonical_target_endpoint.mention_local_id == input_value.source_mention.local_id
    assert input_value.claim.model_dump(mode="json") == before


def test_retry_reuses_identity_and_explicit_first_created_at():
    input_value = _input()
    proposal = _proposal(input_value)
    first = _build(input_value, proposal=proposal, created_at=CREATED_AT)
    retry = _build(
        input_value,
        proposal=proposal,
        created_at=CREATED_AT + timedelta(days=1),
        first_created_at=first.created_at,
    )
    assert retry.created_at == first.created_at
    assert retry.mapping_attempt_id == first.mapping_attempt_id
    assert retry.mapping_result_id == first.mapping_result_id
    assert retry.mapping_attempt_fingerprint == first.mapping_attempt_fingerprint
    assert retry.mapping_result_fingerprint == first.mapping_result_fingerprint


def test_proposal_version_occurrence_ontology_and_provenance_change_identity():
    base_input = _input()
    base = _build(base_input, proposal=_proposal(base_input))
    changed_proposal = _build(
        base_input,
        proposal=_proposal(base_input, endpoint_transform="swap", predicate_transform="inverse"),
    )
    changed_version = _build(base_input, proposal=_proposal(base_input), mapping_version=2)
    changed_mapper = _build(
        base_input,
        proposal=_proposal(base_input),
        provenance=_mapper(mapper_version="m1-v2"),
    )
    changed_model = _build(
        base_input,
        proposal=_proposal(base_input),
        provenance=_mapper(model_version_hash="b" * 64),
    )
    changed_prompt = _build(
        base_input,
        proposal=_proposal(base_input),
        provenance=_mapper(prompt_content_hash="b" * 64),
    )
    changed_config = _build(
        base_input,
        proposal=_proposal(base_input),
        provenance=_mapper(config_hash="b" * 64),
    )
    changed_occurrence_input = _input(
        _plain_claim(extraction_occurrence_id=uuid.UUID("a1000000-0000-0000-0000-000000000002"))
    )
    changed_occurrence = _build(
        changed_occurrence_input,
        proposal=_proposal(changed_occurrence_input),
    )
    changed_ontology_snapshot = _ontology(contract_version="ontology-contract-m1")
    changed_ontology_input = _input(
        _plain_claim(ontology_snapshot_hash=changed_ontology_snapshot.ontology_snapshot_hash),
        ontology=changed_ontology_snapshot,
    )
    changed_ontology = _build(
        changed_ontology_input,
        proposal=_proposal(changed_ontology_input),
    )

    assert changed_proposal.mapping_result_id != base.mapping_result_id
    assert changed_version.mapping_attempt_id != base.mapping_attempt_id
    assert changed_mapper.mapping_attempt_id != base.mapping_attempt_id
    assert changed_model.mapping_attempt_id != base.mapping_attempt_id
    assert changed_prompt.mapping_attempt_id != base.mapping_attempt_id
    assert changed_config.mapping_attempt_id != base.mapping_attempt_id
    assert changed_occurrence.mapping_attempt_id != base.mapping_attempt_id
    assert changed_ontology.mapping_attempt_id != base.mapping_attempt_id


def test_builder_has_no_runtime_boundary_imports():
    source = Path(__file__).resolve().parents[1] / "app" / "services" / "canonical_mapping_builder.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = ("app.workers", "app.models", "app.db", "provider", "candidate", "materializer", "publication")
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.startswith(forbidden)]
