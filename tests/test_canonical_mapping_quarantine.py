from __future__ import annotations

import ast
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from app.schemas.canonical_mapping import (
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    EndpointResolutionAttestationV1,
    EntityLinkDecisionV1,
    FrozenOntologySnapshotV1,
    canonical_mapping_input_json,
    canonical_mapping_json,
    canonical_mapping_json_value,
)
from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.canonical_mapping_quarantine import (
    CanonicalMappingBridgeConfigV1,
    CanonicalMappingBridgeEffect,
    CanonicalMappingBridgeError,
    CanonicalMappingBridgeErrorCode,
    CanonicalMappingBridgeReason,
    CanonicalMappingBridgeStage,
    DEFAULT_CANONICAL_MAPPING_BRIDGE_MIN_CONFIDENCE,
    project_canonical_mapping_to_candidate_sidecar,
)
from app.services.graph_candidate_routing import ConfidencePolicyV1
from tests.test_canonical_mapping import (
    HASH,
    _build,
    _input,
    _ontology,
    _plain_claim,
    _proposal,
    _raw_factory_kwargs,
)


def _enabled(*, minimum_confidence: float | None = None) -> CanonicalMappingBridgeConfigV1:
    values = {"enabled": True}
    if minimum_confidence is not None:
        values["minimum_mapping_confidence"] = minimum_confidence
    return CanonicalMappingBridgeConfigV1(**values)


def _candidate_endpoint_input(
    role: str = "source",
    *,
    link_confidence: float = 0.8,
) -> CanonicalMappingInputV1:
    base = _input()
    resolution = (
        base.source_endpoint_resolution if role == "source" else base.target_endpoint_resolution
    )
    assert resolution is not None
    link_payload = resolution.entity_link.model_dump(mode="json")
    link_payload.update(
        {
            "status": "candidate",
            "confidence": link_confidence,
            "entity_id": None,
            "entity_candidate_key_hash": HASH,
            "link_decision_fingerprint": None,
        }
    )
    candidate_link = EntityLinkDecisionV1.model_validate(link_payload)
    resolution_payload = resolution.model_dump(mode="json")
    resolution_payload.update(
        {
            "entity_link": candidate_link.model_dump(mode="json"),
            "attestation_fingerprint": None,
        }
    )
    candidate_resolution = EndpointResolutionAttestationV1.model_validate(resolution_payload)
    kwargs = _raw_factory_kwargs(base)
    kwargs[f"{role}_endpoint_resolution"] = candidate_resolution
    return CanonicalMappingInputV1.from_raw_claim_json(
        base.raw_claim_authority_json,
        **kwargs,
    )


def _fallback_mapping():
    base = _ontology()
    ontology = FrozenOntologySnapshotV1.from_content(
        ontology_version_id=base.ontology_version_id,
        ontology_contract_version=base.ontology_contract_version,
        entity_type_keys=base.entity_type_keys,
        relation_type_keys=(*base.relation_type_keys, "related_to"),
        constraints=(
            *base.constraints,
            {
                "relation_key": "related_to",
                "source_type_key": "source_type",
                "target_type_key": "target_type",
                "direction": "source_to_target",
            },
        ),
    )
    claim = _plain_claim(ontology_snapshot_hash=ontology.ontology_snapshot_hash)
    input_value = _input(
        claim,
        ontology=ontology,
        authorization_relation_key="related_to",
    )
    result = _build(
        input_value,
        proposal=_proposal(input_value, relation_key="related_to"),
    )
    return input_value, result


def test_bridge_is_default_disabled_and_does_not_emit_a_sidecar():
    input_value = _input()
    result = _build(input_value)

    projected = project_canonical_mapping_to_candidate_sidecar(input_value, result)

    assert projected.effect == CanonicalMappingBridgeEffect.DISABLED
    assert projected.reason_code == CanonicalMappingBridgeReason.BRIDGE_DISABLED
    assert projected.mapping_outcome == "mapped"
    assert projected.sidecar is None


@pytest.mark.parametrize(
    ("outcome", "reason_code", "confidence", "semantic_status", "expected_reason"),
    [
        (
            "ambiguous",
            "ambiguous_mapping",
            0.4,
            "ambiguous",
            CanonicalMappingBridgeReason.AMBIGUOUS_RESULT,
        ),
        (
            "blocked",
            "no_explicit_mapping",
            None,
            "blocked",
            CanonicalMappingBridgeReason.BLOCKED_RESULT,
        ),
        (
            "rejected",
            "ontology_relation_not_allowed",
            None,
            "preserved",
            CanonicalMappingBridgeReason.REJECTED_RESULT,
        ),
    ],
)
def test_non_mapped_statuses_have_explicit_quarantine_effects(
    outcome,
    reason_code,
    confidence,
    semantic_status,
    expected_reason,
):
    input_value = _input()
    result = _build(
        input_value,
        outcome=outcome,
        reason_code=reason_code,
        mapping_confidence=confidence,
        semantic_status=semantic_status,
        auto_proposal=False,
    )

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    assert projected.effect == CanonicalMappingBridgeEffect.QUARANTINED
    assert projected.reason_code == expected_reason
    assert projected.mapping_outcome == outcome
    assert projected.sidecar is None


def test_mapped_sidecar_preserves_complete_authority_and_direct_entity_references():
    input_value = _input(decision=True)
    result = _build(input_value)

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(minimum_confidence=0.8),
    )

    assert projected.effect == CanonicalMappingBridgeEffect.PROJECTED
    assert projected.reason_code is None
    sidecar = projected.sidecar
    assert sidecar is not None
    assert sidecar.authoritative_input_json == canonical_mapping_input_json(input_value)
    assert sidecar.mapping_result_json == canonical_mapping_json(input_value, result)
    input_payload = json.loads(sidecar.authoritative_input_json)
    result_payload = json.loads(sidecar.mapping_result_json)
    assert input_payload["raw_claim_authority_json"]
    assert result_payload["decision_binding"]
    assert sidecar.mapping_result_id == result.mapping_result_id
    assert sidecar.mapping_result_fingerprint == result.mapping_result_fingerprint
    assert result_payload["mapping_attempt_id"] == str(result.mapping_attempt_id)
    assert result_payload["mapping_attempt_fingerprint"] == result.mapping_attempt_fingerprint
    assert result_payload["claim_id"] == str(input_value.claim_id)
    assert result_payload["extraction_occurrence_id"] == str(input_value.extraction_occurrence_id)
    assert result_payload["decision_id"] == str(input_value.decision_id)
    assert result_payload["decision_fingerprint"] == input_value.decision_fingerprint
    assert result_payload["verified_evidence_identity_hashes"] == list(
        result.verified_evidence_identity_hashes
    )
    assert sidecar.source_endpoint.reference_kind == "entity"
    assert sidecar.source_endpoint.entity_id is not None
    assert sidecar.target_endpoint.reference_kind == "entity"
    assert sidecar.target_endpoint.entity_id is not None


@pytest.mark.parametrize("candidate_role", ["source", "target"])
def test_explicit_entity_candidate_endpoint_reference_is_preserved(candidate_role):
    input_value = _candidate_endpoint_input(candidate_role)
    result = _build(input_value, proposal=_proposal(input_value))

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    sidecar = projected.sidecar
    assert sidecar is not None
    endpoint = getattr(sidecar, f"{candidate_role}_endpoint")
    assert endpoint.reference_kind == "entity_candidate"
    assert endpoint.entity_id is None
    assert endpoint.entity_candidate_key_hash == HASH


def test_legal_swap_and_inverse_preserve_canonical_endpoint_roles():
    input_value = _input()
    result = _build(
        input_value,
        proposal=_proposal(
            input_value,
            endpoint_transform="swap",
            predicate_transform="inverse",
        ),
    )

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    candidate = projected.sidecar
    assert candidate.endpoint_transform == "swap"
    assert candidate.predicate_transform == "inverse"
    assert candidate.source_endpoint.role == "source"
    assert candidate.source_endpoint.mention_local_id == input_value.target_mention.local_id
    assert candidate.target_endpoint.role == "target"
    assert candidate.target_endpoint.mention_local_id == input_value.source_mention.local_id


def test_unknown_direction_is_quarantined_without_any_endpoint_swap():
    input_value = _input(_plain_claim(surface_direction="unknown"))
    result = _build(
        input_value,
        outcome="ambiguous",
        reason_code="unknown_direction",
        mapping_confidence=None,
        semantic_status="ambiguous",
        auto_proposal=False,
    )

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    assert projected.reason_code == CanonicalMappingBridgeReason.AMBIGUOUS_RESULT
    assert projected.sidecar is None
    assert input_value.source_mention.local_id == result.claim_snapshot.source_mention_local_id
    assert input_value.target_mention.local_id == result.claim_snapshot.target_mention_local_id


@pytest.mark.parametrize(
    ("confidence", "expected_effect"),
    [
        (0.0, CanonicalMappingBridgeEffect.QUARANTINED),
        (0.849999, CanonicalMappingBridgeEffect.QUARANTINED),
        (0.85, CanonicalMappingBridgeEffect.PROJECTED),
        (0.850001, CanonicalMappingBridgeEffect.PROJECTED),
    ],
)
def test_default_mapping_confidence_boundary_is_conservative(confidence, expected_effect):
    input_value = _input()
    result = _build(input_value, mapping_confidence=confidence)

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    assert DEFAULT_CANONICAL_MAPPING_BRIDGE_MIN_CONFIDENCE == (
        ConfidencePolicyV1().relation_draft_threshold
    )
    assert projected.effect == expected_effect
    if expected_effect == CanonicalMappingBridgeEffect.QUARANTINED:
        assert projected.reason_code == CanonicalMappingBridgeReason.LOW_CONFIDENCE
        assert projected.sidecar is None
    else:
        assert projected.reason_code is None
        assert projected.sidecar is not None


@pytest.mark.parametrize("reference_kind", ["entity", "entity_candidate"])
def test_endpoint_link_confidence_cannot_bypass_low_mapping_confidence(reference_kind):
    input_value = (
        _input()
        if reference_kind == "entity"
        else _candidate_endpoint_input(link_confidence=1.0)
    )
    result = _build(
        input_value,
        mapping_confidence=0.0,
        proposal=_proposal(input_value),
    )

    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )

    assert input_value.source_endpoint_resolution.entity_link.confidence >= 0.95
    assert projected.effect == CanonicalMappingBridgeEffect.QUARANTINED
    assert projected.reason_code == CanonicalMappingBridgeReason.LOW_CONFIDENCE
    assert projected.sidecar is None


def test_generic_fallback_relation_is_quarantined():
    fallback_input, fallback_result = _fallback_mapping()
    fallback = project_canonical_mapping_to_candidate_sidecar(
        fallback_input,
        fallback_result,
        config=_enabled(),
    )
    assert fallback.reason_code == CanonicalMappingBridgeReason.FALLBACK_RELATION
    assert fallback.sidecar is None


@pytest.mark.parametrize(
    "forged_result",
    [
        lambda value: value.model_copy(update={"mapping_result_fingerprint": "b" * 64}),
        lambda value: value.model_copy(update={"ontology_snapshot_hash": "b" * 64}),
        lambda value: value.model_copy(update={"canonical_relation_key": "unknown_relation"}),
        lambda value: value.model_copy(update={"mapping_confidence": True}),
        lambda value: value.model_copy(
            update={
                "canonical_source_endpoint": value.canonical_source_endpoint.model_copy(
                    update={"entity_type_key": "unfrozen_type"}
                )
            }
        ),
        lambda value: value.model_copy(update={"source_endpoint_resolution": None}),
        lambda value: value.model_copy(update={"mapping_evidence_ref_ids": ("missing",)}),
        lambda value: value.model_copy(
            update={
                "evidence_bindings": (
                    value.evidence_bindings[0].model_copy(update={"library_id": uuid.uuid4()}),
                )
            }
        ),
        lambda value: value.model_copy(
            update={
                "evidence_bindings": (
                    value.evidence_bindings[0].model_copy(
                        update={
                            "attestation": value.evidence_bindings[0].attestation.model_copy(
                                update={"locator_sha256": "b" * 64}
                            )
                        }
                    ),
                )
            }
        ),
        lambda value: value.model_copy(
            update={
                "source_endpoint_resolution": value.source_endpoint_resolution.model_copy(
                    update={
                        "scope": value.source_endpoint_resolution.scope.model_copy(
                            update={"library_id": uuid.uuid4()}
                        )
                    }
                )
            }
        ),
    ],
    ids=[
        "stale-result-fingerprint",
        "ontology-mismatch",
        "unknown-relation",
        "invalid-confidence",
        "unfrozen-endpoint-type",
        "missing-endpoint-resolution",
        "missing-evidence",
        "cross-scope-evidence",
        "invalid-locator-attestation",
        "endpoint-scope-mismatch",
    ],
)
def test_forged_mapping_authority_fails_closed_before_projection(forged_result):
    input_value = _input()
    result = _build(input_value)

    with pytest.raises(CanonicalMappingBridgeError) as error:
        project_canonical_mapping_to_candidate_sidecar(
            input_value,
            forged_result(result),
            config=_enabled(),
        )

    assert error.value.code == CanonicalMappingBridgeErrorCode.INVALID_AUTHORITY
    assert error.value.stage == CanonicalMappingBridgeStage.RESULT_AUTHORITY


def test_model_construct_and_forged_input_fail_closed_at_typed_stages():
    input_value = _input()
    result = _build(input_value)
    payload = dict(result.__dict__)
    payload["mapping_result_fingerprint"] = "c" * 64
    constructed = CanonicalMappingV1.model_construct(**payload)

    with pytest.raises(CanonicalMappingBridgeError) as result_error:
        project_canonical_mapping_to_candidate_sidecar(
            input_value,
            constructed,
            config=_enabled(),
        )
    assert result_error.value.stage == CanonicalMappingBridgeStage.RESULT_AUTHORITY

    forged_input = input_value.model_copy(update={"claim_id": uuid.uuid4()})
    with pytest.raises(CanonicalMappingBridgeError) as input_error:
        project_canonical_mapping_to_candidate_sidecar(
            forged_input,
            result,
            config=_enabled(),
        )
    assert input_error.value.stage == CanonicalMappingBridgeStage.INPUT_AUTHORITY


@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf")])
def test_forged_config_values_are_typed_argument_errors(value):
    input_value = _input()
    result = _build(input_value)
    config = CanonicalMappingBridgeConfigV1.model_construct(
        enabled=True,
        minimum_mapping_confidence=value,
    )

    with pytest.raises(CanonicalMappingBridgeError) as error:
        project_canonical_mapping_to_candidate_sidecar(
            input_value,
            result,
            config=config,
        )

    assert error.value.code == CanonicalMappingBridgeErrorCode.INVALID_ARGUMENT
    assert error.value.stage == CanonicalMappingBridgeStage.ARGUMENTS
    assert str(error.value) == "invalid_argument"


def test_projection_is_deterministic_and_does_not_mutate_any_input():
    input_value = _input(decision=True)
    result = _build(input_value)
    before_input = canonical_mapping_input_json(input_value)
    before_result = canonical_mapping_json(input_value, result)

    first = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(minimum_confidence=0.8),
    )
    second = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(minimum_confidence=0.8),
    )

    assert first == second
    assert canonical_mapping_json_value(first.model_dump(mode="json")) == canonical_mapping_json_value(
        second.model_dump(mode="json")
    )
    assert canonical_mapping_input_json(input_value) == before_input
    assert canonical_mapping_json(input_value, result) == before_result


def test_graph_extraction_payload_cannot_replace_the_authority_sidecar():
    input_value = _input(decision=True)
    result = _build(input_value)
    projected = project_canonical_mapping_to_candidate_sidecar(
        input_value,
        result,
        config=_enabled(),
    )
    sidecar = projected.sidecar
    assert sidecar is not None

    with pytest.raises(ValidationError):
        GraphExtractionPayload.model_validate(
            {
                "entities": [],
                "relations": [],
                "mapping_result_id": str(sidecar.mapping_result_id),
                "raw_claim_authority_sha256": json.loads(sidecar.mapping_result_json)[
                    "raw_claim_authority_sha256"
                ],
            }
        )


def test_bridge_calls_no_candidate_write_materializer_publication_or_fact_writer():
    input_value = _input()
    result = _build(input_value)
    targets = (
        "app.services.graph_candidate_aggregation.stage_unit_candidate_occurrences",
        "app.services.graph_extraction_materializer.materialize_graph_extraction_job",
        "app.services.graph_publication_planner.plan_graph_publication",
        "app.services.graph_publication_activation.activate_graph_publication",
        "app.services.graph_entities.create_entity",
        "app.services.graph_relations.create_relation",
    )
    spies: list[Mock] = []

    with ExitStack() as stack:
        for target in targets:
            spy = Mock(side_effect=AssertionError("forbidden M4 write path"))
            stack.enter_context(patch(target, spy))
            spies.append(spy)
        projected = project_canonical_mapping_to_candidate_sidecar(
            input_value,
            result,
            config=_enabled(),
        )

    assert projected.effect == CanonicalMappingBridgeEffect.PROJECTED
    assert all(spy.call_count == 0 for spy in spies)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _imported_modules(tree: ast.AST) -> set[str]:
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return modules


def test_bridge_module_has_strict_db_and_write_path_isolation():
    module_path = Path(__file__).parents[1] / "app" / "services" / "canonical_mapping_quarantine.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = _imported_modules(tree)
    app_imports = {module for module in imported_modules if module.startswith("app.")}
    assert app_imports == {"app.schemas.canonical_mapping"}
    assert not any(
        module == "sqlalchemy" or module.startswith(("sqlalchemy.", "app.models"))
        for module in imported_modules
    )

    forbidden_calls = {
        "KnowledgeRelation",
        "Session",
        "AsyncSession",
        "sessionmaker",
        "select",
        "insert",
        "update",
        "delete",
        "stage_unit_candidate_occurrences",
        "materialize_graph_extraction_job",
        "plan_graph_publication",
        "activate_graph_publication",
        "create_entity",
        "create_relation",
    }
    calls = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if (name := _dotted_name(node.func)) is not None
    }
    assert not {name.rsplit(".", 1)[-1] for name in calls} & forbidden_calls

    forbidden_identifiers = {"db", "session", "async_session", "engine"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not (names | attributes) & forbidden_identifiers


def test_bridge_import_callers_are_limited_to_contract_tests():
    root = Path(__file__).parents[1]
    callers: set[str] = set()
    target = "app.services.canonical_mapping_quarantine"
    for path in (*root.joinpath("app").rglob("*.py"), *root.joinpath("tests").rglob("*.py")):
        if path == root / "app" / "services" / "canonical_mapping_quarantine.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        imports_target = target in imported or any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.services"
            and any(alias.name == "canonical_mapping_quarantine" for alias in node.names)
            for node in ast.walk(tree)
        )
        if imports_target:
            callers.add(path.relative_to(root).as_posix())
    assert callers == {
        "app/services/canonical_mapping_rollout.py",
        "tests/test_canonical_mapping_quarantine.py",
    }


def test_phase0_counts_are_historical_e2e_evidence_not_reexecuted_by_m4():
    plan = (
        Path(__file__).parents[1]
        / "docs"
        / "superpowers"
        / "plans"
        / "2026-08-07-evidence-centered-hierarchical-graphrag.md"
    ).read_text(encoding="utf-8")
    assert "ce9f4c0a-922a-4e4d-96d4-51b386a2a05d" in plan
    assert "91 entities、27 relations" in plan
    assert "ee813b2e-1462-40c5-baf3-b48ee618a039" in plan
    assert "3 个实体 · 2 条关系" in plan
