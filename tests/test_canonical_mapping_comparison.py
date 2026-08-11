from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path
from uuid import UUID

import pytest

from app.schemas.canonical_mapping import EndpointTypeBindingV1
from app.schemas.claim_shadow_replay_artifact import (
    canonical_claim_shadow_replay_v2_json,
    redact_raw_claim_v2,
)
from app.services.canonical_mapping_builder import build_canonical_mapping
from app.services.canonical_mapping_comparison import (
    CanonicalComparisonMetricV1,
    CanonicalMappingComparisonCaseV1,
    CanonicalMappingComparisonError,
    compare_canonical_mappings,
)
from tests.test_canonical_mapping import (
    CREATED_AT,
    _actor_provenance,
    _input,
    _mapper,
    _plain_claim,
    _proposal,
    _source_provenance,
)
from tests.test_claim_shadow_replay_assembler import (
    _assemble as _assemble_replay,
    _claim as _replay_claim,
)
from tests.test_claim_shadow_raw_scorer import _gold, _gold_fixture


@pytest.fixture(scope="module")
def sanitized_m5h_artifact_bytes() -> bytes:
    artifact = _assemble_replay(
        (
            _replay_claim(
                claim_seed="m5h-synthetic",
                occurrence_seed="m5h-synthetic",
                job_seed="m5h-synthetic",
                unit_seed="m5h-synthetic",
            ),
        )
    )
    payload = canonical_claim_shadow_replay_v2_json(artifact).encode("utf-8")
    assert len(payload) == 4003
    assert hashlib.sha256(payload).hexdigest() == (
        "16d4c190f33f4313c6b251f1d9b6af360f3faa45bd48a852ccf330d9c40d4f4c"
    )
    assert artifact.artifact_id_sha256 == (
        "c7c5c1d70c5dec4416ab6158afd0b223ef40c29b1dc1f97ed9428b94d4cadb1c"
    )
    return payload + b"\n"


def _claim_for(domain: str, ordinal: int = 1):
    return _plain_claim(
        raw_predicate=f"{domain}_surface_predicate",
        claim_id=UUID(f"a0000000-0000-0000-0000-{ordinal:012d}"),
        extraction_occurrence_id=UUID(f"a1000000-0000-0000-0000-{ordinal:012d}"),
    )


def _input_for(domain: str, ordinal: int = 1):
    return _input(_claim_for(domain, ordinal))


def _mapped(
    input_value,
    *,
    created_at=CREATED_AT,
    provenance=None,
    mapping_version=1,
    **proposal_changes,
):
    return build_canonical_mapping(
        input_value,
        provenance=provenance or _mapper(),
        source_provenance=_source_provenance(),
        actor_provenance=_actor_provenance(),
        created_at=created_at,
        mapping_version=mapping_version,
        mapping_confidence=0.9,
        proposal=_proposal(input_value, **proposal_changes),
    )


def _blocked(input_value):
    return build_canonical_mapping(
        input_value,
        provenance=_mapper(),
        source_provenance=_source_provenance(),
        actor_provenance=_actor_provenance(),
        created_at=CREATED_AT,
        mapping_confidence=None,
        proposal=None,
    )


def _case(input_value, *, expected=None, predicted=None):
    return CanonicalMappingComparisonCaseV1(
        input=input_value,
        expected=expected,
        predicted=predicted,
    )


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_perfect_domain_neutral_mapping_has_separate_canonical_metrics(domain):
    input_value = _input_for(domain)
    result = _mapped(input_value)

    report = compare_canonical_mappings([_case(input_value, expected=result, predicted=result)])

    metrics = report.canonical_mapping_metrics
    assert metrics.overall.mapping_accuracy.value == 1
    assert metrics.overall.mapping_coverage.value == 1
    assert metrics.overall.predicted_unique_mapping_count == 1
    assert metrics.overall.gold_unique_mapping_count == 1
    assert metrics.overall.matched_unique_mapping_count == 1
    assert report.raw_extraction_metrics.structural_raw_metrics.metrics is None
    assert report.raw_extraction_metrics.structural_raw_metrics.unavailable_reason == (
        "raw_artifact_not_provided"
    )


def test_missing_extra_wrong_direction_and_surface_predicate_are_not_forgiven():
    input_value = _input_for("asset")
    expected = _mapped(input_value)

    missing = compare_canonical_mappings([_case(input_value, expected=expected)])
    assert missing.canonical_mapping_metrics.overall.mapping_accuracy.value is None
    assert missing.canonical_mapping_metrics.overall.mapping_accuracy.unavailable_reason == (
        "no_predicted_canonical_mappings"
    )
    assert missing.canonical_mapping_metrics.overall.mapping_coverage.value == 0

    extra = compare_canonical_mappings([_case(input_value, predicted=expected)])
    assert extra.canonical_mapping_metrics.overall.mapping_accuracy.value == 0
    assert extra.canonical_mapping_metrics.overall.mapping_coverage.value is None
    assert extra.canonical_mapping_metrics.overall.mapping_coverage.unavailable_reason == (
        "no_gold_canonical_mappings"
    )

    swapped = _mapped(input_value, endpoint_transform="swap", predicate_transform="inverse")
    wrong_transform = compare_canonical_mappings(
        [_case(input_value, expected=expected, predicted=swapped)]
    )
    assert wrong_transform.canonical_mapping_metrics.overall.matched_unique_mapping_count == 0
    assert wrong_transform.canonical_mapping_metrics.overall.mapping_accuracy.value == 0
    assert wrong_transform.canonical_mapping_metrics.overall.mapping_coverage.value == 0

    wrong_surface_input = _input_for("wrong_predicate")
    wrong_surface_gold = _mapped(wrong_surface_input)
    wrong_surface_prediction = _blocked(wrong_surface_input)
    wrong_surface = compare_canonical_mappings(
        [
            _case(
                wrong_surface_input,
                expected=wrong_surface_gold,
                predicted=wrong_surface_prediction,
            )
        ]
    )
    assert wrong_surface.canonical_mapping_metrics.overall.missing_unique_mapping_count == 1
    assert wrong_surface.canonical_mapping_metrics.overall.unknown_predicate_retention.value == 1


def test_duplicate_occurrences_deduplicate_core_but_keep_occurrence_count():
    first_input = _input_for("asset", 1)
    second_input = _input_for("asset", 2)
    first_result = _mapped(first_input)
    second_result = _mapped(second_input)

    report = compare_canonical_mappings(
        [
            _case(first_input, expected=first_result, predicted=first_result),
            _case(second_input, expected=second_result, predicted=second_result),
        ]
    )
    overall = report.canonical_mapping_metrics.overall
    assert overall.occurrence_count == 2
    assert overall.unique_claim_count == 1
    assert overall.duplicate_occurrence_count == 1
    assert overall.predicted_unique_mapping_count == 1
    assert overall.gold_unique_mapping_count == 1
    assert overall.mapping_accuracy.value == 1

    reversed_report = compare_canonical_mappings(
        [
            _case(second_input, expected=second_result, predicted=second_result),
            _case(first_input, expected=first_result, predicted=first_result),
        ]
    )
    assert report.canonical_mapping_metrics == reversed_report.canonical_mapping_metrics

    repeated = compare_canonical_mappings(
        [
            _case(first_input, expected=first_result, predicted=first_result),
            _case(first_input, expected=first_result, predicted=first_result),
        ]
    )
    assert repeated.canonical_mapping_metrics.overall.occurrence_count == 1


def test_occurrence_identity_ignores_created_and_attestation_timestamps():
    input_value = _input_for("asset", 2)
    later = CREATED_AT.replace(hour=13)
    validated_later = _input(input_value.claim, validated_at=later)
    result = _mapped(input_value)
    created_later = _mapped(input_value, created_at=later)
    both_later = _mapped(validated_later, created_at=later)
    assert created_later.mapping_attempt_fingerprint == result.mapping_attempt_fingerprint
    assert created_later.mapping_result_fingerprint == result.mapping_result_fingerprint
    assert both_later.mapping_attempt_fingerprint == result.mapping_attempt_fingerprint
    assert both_later.mapping_result_fingerprint == result.mapping_result_fingerprint

    for alternate_input, alternate_result in (
        (input_value, created_later),
        (validated_later, result),
        (validated_later, both_later),
    ):
        report = compare_canonical_mappings(
            [
                _case(input_value, expected=result, predicted=result),
                _case(alternate_input, expected=alternate_result, predicted=alternate_result),
            ]
        )
        assert report.canonical_mapping_metrics.overall.occurrence_count == 1


def test_occurrence_identity_rejects_stable_attempt_result_status_or_relation_changes():
    input_value = _input_for("asset", 3)
    baseline = _mapped(input_value)
    changed_attempt = _mapped(input_value, mapping_version=2)
    changed_relation = _mapped(
        input_value,
        endpoint_transform="swap",
        predicate_transform="inverse",
    )
    changed_status = _blocked(input_value)

    for changed in (changed_attempt, changed_relation, changed_status):
        with pytest.raises(CanonicalMappingComparisonError) as error:
            compare_canonical_mappings(
                [
                    _case(input_value, expected=baseline, predicted=baseline),
                    _case(input_value, expected=baseline, predicted=changed),
                ]
            )
        assert error.value.code == "duplicate_occurrence_input"


def test_occurrence_identity_is_global_across_revision_scope():
    first_input = _input_for("asset", 60)
    base_reference = first_input.claim.evidence_refs[0]
    revision_id = UUID("30000000-0000-0000-0000-000000000060")
    revision_locator = base_reference.locator.model_copy(
        update={"document_revision_id": revision_id, "revision_no": 2}
    )
    revision_reference = base_reference.model_copy(
        update={
            "document_revision_id": revision_id,
            "revision_no": 2,
            "locator": revision_locator,
        }
    )
    second_input = _input(
        _plain_claim(
            raw_predicate="asset_surface_predicate",
            claim_id=UUID("a0000000-0000-0000-0000-000000000061"),
            extraction_occurrence_id=first_input.extraction_occurrence_id,
            document_revision_id=revision_id,
            revision_no=2,
            evidence_refs=[revision_reference],
        )
    )
    first_result = _mapped(first_input)
    second_result = _mapped(second_input)

    with pytest.raises(CanonicalMappingComparisonError) as error:
        compare_canonical_mappings(
            [
                _case(first_input, expected=first_result, predicted=first_result),
                _case(second_input, expected=second_result, predicted=second_result),
            ]
        )
    assert error.value.code == "occurrence_identity_conflict"


@pytest.mark.parametrize("scope_field", ["job_id", "extraction_unit_id", "document_revision_id"])
def test_occurrence_identity_binds_job_unit_and_revision(scope_field):
    first_input = _input_for("legal", 61)
    new_scope_value = UUID(
        f"30000000-0000-0000-0000-{62 if scope_field == 'job_id' else 63:012d}"
    )
    reference = first_input.claim.evidence_refs[0]
    reference_updates = {scope_field: new_scope_value}
    claim_updates = {scope_field: new_scope_value}
    if scope_field == "document_revision_id":
        claim_updates["revision_no"] = 2
        reference_updates["revision_no"] = 2
        reference_updates["locator"] = reference.locator.model_copy(
            update={"document_revision_id": new_scope_value, "revision_no": 2}
        )
    second_input = _input(
        _plain_claim(
            raw_predicate="legal_surface_predicate",
            claim_id=UUID("a0000000-0000-0000-0000-000000000062"),
            extraction_occurrence_id=first_input.extraction_occurrence_id,
            evidence_refs=[reference.model_copy(update=reference_updates)],
            **claim_updates,
        )
    )
    first_result = _mapped(first_input)
    second_result = _mapped(second_input)

    with pytest.raises(CanonicalMappingComparisonError) as error:
        compare_canonical_mappings(
            [
                _case(first_input, expected=first_result, predicted=first_result),
                _case(second_input, expected=second_result, predicted=second_result),
            ]
        )
    assert error.value.code == "occurrence_identity_conflict"


def test_cross_revision_cases_have_separate_scope_rows():
    first_input = _input_for("legal", 10)
    base_claim = first_input.claim
    revision_id = UUID("30000000-0000-0000-0000-000000000002")
    base_reference = base_claim.evidence_refs[0]
    assert base_reference.locator is not None
    revision_locator = base_reference.locator.model_copy(
        update={"document_revision_id": revision_id, "revision_no": 2}
    )
    revision_reference = base_reference.model_copy(
        update={
            "document_revision_id": revision_id,
            "revision_no": 2,
            "locator": revision_locator,
        }
    )
    second_input = _input(
        _plain_claim(
            raw_predicate="legal_surface_predicate",
            claim_id=UUID("a0000000-0000-0000-0000-000000000011"),
            extraction_occurrence_id=UUID("a1000000-0000-0000-0000-000000000011"),
            document_revision_id=revision_id,
            revision_no=2,
            evidence_refs=[revision_reference],
        )
    )
    first_result = _mapped(first_input)
    second_result = _mapped(second_input)

    report = compare_canonical_mappings(
        [
            _case(first_input, expected=first_result, predicted=first_result),
            _case(second_input, expected=second_result, predicted=second_result),
        ]
    )
    metrics = report.canonical_mapping_metrics
    assert len(metrics.scope_metrics) == 2
    assert sorted(row.revision_no for row in metrics.scope_metrics) == [1, 2]
    assert metrics.overall.unique_claim_count == 2
    assert metrics.overall.predicted_unique_mapping_count == 2


def test_unknown_predicate_direction_and_endpoint_retention_is_explicit():
    unknown_predicate = _input_for("unknown_predicate", 20)
    unknown_direction = _input(_plain_claim(surface_direction="unknown", claim_id=UUID("a0000000-0000-0000-0000-000000000021"), extraction_occurrence_id=UUID("a1000000-0000-0000-0000-000000000021")))
    unknown_endpoint = _input(
        _claim_for("unknown_endpoint", 22),
        endpoint_type_binding=EndpointTypeBindingV1(source_type_key=None, target_type_key="target_type"),
    )
    dropped_predicate = _input_for("dropped_predicate", 23)

    report = compare_canonical_mappings(
        [
            _case(unknown_predicate, predicted=_blocked(unknown_predicate)),
            _case(unknown_direction, predicted=_blocked(unknown_direction)),
            _case(unknown_endpoint, predicted=_blocked(unknown_endpoint)),
            _case(dropped_predicate),
        ]
    )
    overall = report.canonical_mapping_metrics.overall
    assert overall.unknown_predicate_retention.value == pytest.approx(1 / 3)
    assert overall.unknown_direction_retention.value == 1
    assert overall.unknown_endpoint_retention.value == 1

    dropped = compare_canonical_mappings([_case(dropped_predicate)])
    assert dropped.canonical_mapping_metrics.overall.mapping_accuracy.value is None
    assert dropped.canonical_mapping_metrics.overall.mapping_coverage.value is None


def test_decision_candidate_is_diagnostic_only_and_never_a_canonical_relation():
    input_value = _input_for("asset", 30,)
    candidate_input = _input(
        input_value.claim,
        decision=True,
    )
    candidate = _mapped(candidate_input)

    report = compare_canonical_mappings([_case(candidate_input, predicted=candidate)])

    overall = report.canonical_mapping_metrics.overall
    assert overall.predicted_unique_mapping_count == 0
    assert overall.gold_unique_mapping_count == 0
    assert overall.ignored_decision_candidate_count == 1
    assert overall.mapping_accuracy.value is None
    assert overall.mapping_accuracy.unavailable_reason == "no_predicted_canonical_mappings"


def test_invalid_locator_is_an_explicit_unavailable_diagnostic():
    report = compare_canonical_mappings([], unavailable_reasons=("invalid_locator",))
    assert report.canonical_mapping_metrics.unavailable_reasons == ("invalid_locator",)
    assert report.canonical_mapping_metrics.overall.mapping_accuracy.value is None


@pytest.mark.parametrize("value", [1, True, "0.5", math.nan, math.inf, -0.1, 1.1])
def test_canonical_ratios_are_strict_finite_native_floats(value):
    with pytest.raises(ValueError):
        CanonicalComparisonMetricV1(value=value)


@pytest.mark.parametrize(
    "reasons",
    [
        ({"sensitive": "source text"},),
        (["invalid_locator"],),
        (True,),
        (1,),
        ("C:\\private\\source.txt",),
    ],
)
def test_unavailable_reason_inputs_fail_with_stable_comparison_error(reasons):
    with pytest.raises(CanonicalMappingComparisonError) as error:
        compare_canonical_mappings([], unavailable_reasons=reasons)
    assert error.value.code == "unavailable_reason_invalid"


def test_sanitized_m5h_artifact_is_loaded_as_raw_structure_only(
    sanitized_m5h_artifact_bytes,
):
    report = compare_canonical_mappings(
        [], replay_artifact=sanitized_m5h_artifact_bytes
    )

    raw = report.raw_extraction_metrics
    assert raw.optional_raw_gold_metrics.metrics is None
    assert raw.optional_raw_gold_metrics.unavailable_reason == "raw_gold_not_provided"
    assert report.canonical_mapping_metrics.overall.gold_unique_mapping_count == 0

    replay_report = report.to_claim_shadow_replay_report(sanitized_m5h_artifact_bytes)
    assert replay_report.canonical_metrics.metrics is not None
    assert replay_report.optional_raw_gold_metrics.metrics is None
    assert replay_report.optional_raw_gold_metrics.unavailable_reason == "raw_gold_not_provided"
    assert replay_report.canonical_metrics.metrics["schema_version"] == "canonical_mapping_metrics_v1"


def test_replay_artifact_binding_is_explicit_and_rejects_unbound_or_mismatched_reports(
    sanitized_m5h_artifact_bytes,
):
    artifact = sanitized_m5h_artifact_bytes
    unbound = compare_canonical_mappings([])
    assert unbound.artifact_binding is None
    assert unbound.artifact_binding_status == "unbound_sidecar"
    with pytest.raises(CanonicalMappingComparisonError) as error:
        unbound.to_claim_shadow_replay_report(artifact)
    assert error.value.code == "comparison_report_unbound"

    bound = compare_canonical_mappings([], replay_artifact=artifact)
    assert bound.artifact_binding is not None
    assert bound.artifact_binding_status == "bound"
    other_artifact = _assemble_replay(
        (
            _replay_claim(
                claim_seed="comparison-binding-other",
                occurrence_seed="comparison-binding-other",
                job_seed="comparison-binding-other",
                unit_seed="comparison-binding-other",
            ),
        )
    )
    with pytest.raises(CanonicalMappingComparisonError) as error:
        bound.to_claim_shadow_replay_report(other_artifact)
    assert error.value.code == "replay_artifact_identity_mismatch"
    assert bound.to_claim_shadow_replay_report(artifact).artifact_id_sha256 == (
        bound.artifact_binding.artifact_id_sha256
    )


@pytest.mark.parametrize("metric_update", [{"value": 2.0}, {"value": math.nan}])
def test_replay_forwarding_rejects_model_copy_metric_forgery(
    metric_update,
    sanitized_m5h_artifact_bytes,
):
    report = compare_canonical_mappings(
        [], replay_artifact=sanitized_m5h_artifact_bytes
    )
    forged_metric = report.canonical_mapping_metrics.overall.mapping_accuracy.model_copy(
        update=metric_update
    )
    forged_overall = report.canonical_mapping_metrics.overall.model_copy(
        update={"mapping_accuracy": forged_metric}
    )
    forged_metrics = report.canonical_mapping_metrics.model_copy(
        update={"overall": forged_overall, "mapping_accuracy": forged_metric}
    )
    forged_report = report.model_copy(update={"canonical_mapping_metrics": forged_metrics})

    with pytest.raises(CanonicalMappingComparisonError) as error:
        forged_report.to_claim_shadow_replay_report(sanitized_m5h_artifact_bytes)
    assert error.value.code == "comparison_report_forged"


def test_replay_forwarding_rejects_model_copy_negative_count(
    sanitized_m5h_artifact_bytes,
):
    report = compare_canonical_mappings(
        [], replay_artifact=sanitized_m5h_artifact_bytes
    )
    forged_overall = report.canonical_mapping_metrics.overall.model_copy(
        update={"occurrence_count": -1}
    )
    forged_metrics = report.canonical_mapping_metrics.model_copy(
        update={"overall": forged_overall}
    )
    forged_report = report.model_copy(update={"canonical_mapping_metrics": forged_metrics})

    with pytest.raises(CanonicalMappingComparisonError) as error:
        forged_report.to_claim_shadow_replay_report(sanitized_m5h_artifact_bytes)
    assert error.value.code == "comparison_report_forged"


def test_raw_gold_stays_in_raw_family_when_canonical_metrics_are_present():
    replay_claim = _replay_claim(
        claim_seed="comparison-raw",
        occurrence_seed="comparison-raw",
        job_seed="comparison-raw",
        unit_seed="comparison-raw",
    )
    artifact = _assemble_replay((replay_claim,))
    redacted = redact_raw_claim_v2(replay_claim)
    raw_gold = _gold_fixture([_gold(redacted)])
    input_value = _input_for("medical", 50)
    result = _mapped(input_value)

    report = compare_canonical_mappings(
        [_case(input_value, expected=result, predicted=result)],
        replay_artifact=artifact,
        raw_gold=raw_gold,
    )
    assert report.raw_extraction_metrics.structural_raw_metrics.metrics is not None
    assert report.raw_extraction_metrics.optional_raw_gold_metrics.metrics is not None
    assert report.canonical_mapping_metrics.overall.mapping_accuracy.value == 1
    assert report.raw_extraction_metrics.optional_raw_gold_metrics.metrics.overall.raw_relation_precision.value == 1
    assert report.raw_extraction_metrics.optional_raw_gold_metrics.metrics.overall.raw_relation_recall.value == 1


def test_comparison_requires_typed_m1_inputs_and_outputs_without_sensitive_errors():
    with pytest.raises(CanonicalMappingComparisonError) as error:
        compare_canonical_mappings([{}])
    assert error.value.code == "case_not_typed"
    assert str(error.value) == "case_not_typed"

    input_value = _input_for("asset", 40)
    with pytest.raises(CanonicalMappingComparisonError) as error:
        compare_canonical_mappings([_case(input_value, predicted={})])
    assert error.value.code == "output_not_m1_typed"
    assert str(error.value) == "output_not_m1_typed"


def test_comparison_adapter_has_no_runtime_write_or_model_boundaries():
    source = Path(__file__).resolve().parents[1] / "app" / "services" / "canonical_mapping_comparison.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = (
        "app.api",
        "app.db",
        "app.models",
        "app.workers",
        "candidate",
        "materializer",
        "migration",
        "publication",
        "provider",
    )
    assert not [name for name in imported if name.startswith(forbidden) or name in forbidden]
