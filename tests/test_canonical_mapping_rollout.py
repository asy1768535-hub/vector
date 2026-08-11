from __future__ import annotations

import ast
import asyncio
import inspect
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.services.canonical_mapping_rollout import (
    MAX_LEAK_SENTINEL_BYTES,
    MAX_LEAK_SENTINEL_TOTAL_BYTES,
    CanonicalMappingCanaryCaseV1,
    CanonicalMappingCanaryFailure,
    CanonicalMappingCanaryLimitsV1,
    CanonicalMappingCanaryMapperOutputV1,
    CanonicalMappingRolloutAuthorizationV1,
    CanonicalMappingRolloutError,
    canonical_mapping_json_value,
    canonical_mapping_input_json,
    canonical_mapping_json,
    canonical_mapping_canary_report_json,
    canonical_mapping_canary_report_sha256,
    rollback_canonical_mapping_rollout,
    run_canonical_mapping_canary_suite,
)
from tests.test_canonical_mapping_comparison import _input_for, _mapped
from tests.test_canonical_mapping import _build, _input, _plain_claim
from tests.test_claim_shadow_raw_scorer import _gold, _gold_fixture
from tests.test_claim_shadow_replay_assembler import (
    _assemble as _assemble_replay,
    _claim as _replay_claim,
)


def _cases():
    rows = []
    for ordinal, domain in enumerate(("asset", "legal", "medical"), start=1):
        input_value = _input_for(domain, ordinal)
        rows.append(
            CanonicalMappingCanaryCaseV1(
                case_key=domain,
                input=input_value,
                expected=_mapped(input_value),
            )
        )
    return tuple(rows)


def _authorization(library_id, *, read=True, bridge=True):
    return CanonicalMappingRolloutAuthorizationV1(
        global_execution_enabled=True,
        authorized_library_ids=(library_id,),
        read_visibility_enabled=read,
        bridge_visibility_enabled=bridge,
    )


def _replay_fixture():
    claims = tuple(
        _replay_claim(
            claim_seed=f"m5-{domain}",
            occurrence_seed=f"m5-{domain}",
            job_seed=f"m5-{domain}",
            unit_seed=f"m5-{domain}",
        )
        for domain in ("asset", "legal", "medical")
    )
    artifact = _assemble_replay(claims)
    gold = _gold_fixture([_gold(record) for record in artifact.raw_claims])
    return artifact, gold


class _FakeMapper:
    def __init__(self, *, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    async def __call__(self, case):
        assert not hasattr(case, "expected")
        self.calls.append(case.case_key)
        override = self.overrides.get(case.case_key)
        if callable(override):
            return await override(case)
        if override is not None:
            return override
        return CanonicalMappingCanaryMapperOutputV1(
            result=_mapped(case.input),
            latency_ms=10,
            cost_microunits=100,
        )


class _FakePersistence:
    def __init__(self, *, fail_case_id=None):
        self.calls = []
        self.fail_case_id = fail_case_id

    async def __call__(self, input_value, result):
        self.calls.append(result.mapping_result_fingerprint)
        if result.mapping_result_id == self.fail_case_id:
            raise RuntimeError("synthetic persistence failure")


def _run(*, authorization=None, cases=None, mapper=None, persistence=None, **kwargs):
    typed_cases = cases or _cases()
    library_id = typed_cases[0].input.library_id
    return asyncio.run(
        run_canonical_mapping_canary_suite(
            authorization=authorization or CanonicalMappingRolloutAuthorizationV1(),
            library_id=library_id,
            cases=typed_cases,
            mapper=mapper or _FakeMapper(),
            persistence=persistence,
            **kwargs,
        )
    )


def test_global_and_library_rollout_are_independently_default_off():
    cases = _cases()
    mapper = _FakeMapper()
    persistence = _FakePersistence()
    artifact, gold = _replay_fixture()

    report = _run(
        cases=cases,
        mapper=mapper,
        persistence=persistence,
        replay_artifact=artifact,
        raw_gold=gold,
    )

    assert report.status == "disabled"
    assert report.publication_handoff == "not_authorized"
    assert report.canonical_mapping_metrics is None
    assert report.canonical_metrics_unavailable_reason == "mapping_rollout_disabled"
    assert report.operational_metrics.mapper_call_count == 0
    assert report.operational_metrics.persistence_call_count == 0
    assert mapper.calls == []
    assert persistence.calls == []
    assert all(row.execution_status == "not_run" for row in report.outcomes)


def test_phase2_schema_and_candidate_facts_are_not_rollout_authorization_inputs():
    parameters = inspect.signature(run_canonical_mapping_canary_suite).parameters
    assert "phase2_shadow_enabled" not in parameters
    assert "schema_confirmed" not in parameters
    assert "candidate_status" not in parameters
    assert CanonicalMappingRolloutAuthorizationV1().global_execution_enabled is False


def test_global_only_or_library_only_cannot_enable_mapping():
    cases = _cases()
    library_id = cases[0].input.library_id
    authorizations = (
        CanonicalMappingRolloutAuthorizationV1(global_execution_enabled=True),
        CanonicalMappingRolloutAuthorizationV1(authorized_library_ids=(library_id,)),
    )
    for authorization in authorizations:
        mapper = _FakeMapper()
        report = _run(authorization=authorization, cases=cases, mapper=mapper)
        assert report.status == "disabled"
        assert mapper.calls == []


@pytest.mark.parametrize(
    "authorized_library_ids",
    [
        lambda value: [value],
        lambda value: (str(value),),
        lambda value: str(value),
        lambda _value: (True,),
        lambda _value: (1,),
    ],
)
def test_authorization_python_ingress_requires_exact_uuid_tuple(authorized_library_ids):
    library_id = uuid.uuid4()
    with pytest.raises(ValueError):
        CanonicalMappingRolloutAuthorizationV1(
            authorized_library_ids=authorized_library_ids(library_id)
        )

    authorization = CanonicalMappingRolloutAuthorizationV1(
        authorized_library_ids=(library_id,)
    )
    assert authorization.authorized_library_ids == (library_id,)
    with pytest.raises(ValueError):
        CanonicalMappingRolloutAuthorizationV1(
            authorized_library_ids=(library_id, library_id)
        )


def test_authorization_json_ingress_accepts_only_canonical_uuid_array_strings():
    library_id = uuid.uuid4()
    payload = {
        "schema_version": "canonical_mapping_rollout_authorization_v1",
        "global_execution_enabled": True,
        "authorized_library_ids": [str(library_id)],
        "read_visibility_enabled": True,
        "bridge_visibility_enabled": True,
        "authorization_version": 1,
    }
    rebuilt = CanonicalMappingRolloutAuthorizationV1.model_validate_json(
        json.dumps(payload, separators=(",", ":"))
    )
    assert rebuilt.authorized_library_ids == (library_id,)
    assert CanonicalMappingRolloutAuthorizationV1.model_validate_json(
        canonical_mapping_json_value(rebuilt.model_dump(mode="json"))
    ) == rebuilt

    attacks = (
        {**payload, "authorized_library_ids": str(library_id)},
        {**payload, "authorized_library_ids": [str(library_id).upper()]},
        {**payload, "authorized_library_ids": [f"{{{library_id}}}"]},
        {**payload, "authorized_library_ids": [f"urn:uuid:{library_id}"]},
        {**payload, "authorized_library_ids": [1]},
        {**payload, "authorized_library_ids": [True]},
        {**payload, "authorized_library_ids": [str(library_id), str(library_id)]},
        {**payload, "authorization_version": "1"},
        {**payload, "authorization_version": 0},
    )
    for attack in attacks:
        with pytest.raises(ValueError):
            CanonicalMappingRolloutAuthorizationV1.model_validate_json(json.dumps(attack))


def test_forged_authorization_collection_cannot_cross_runtime_boundary():
    cases = _cases()
    forged = _authorization(cases[0].input.library_id).model_copy(
        update={"authorized_library_ids": [str(cases[0].input.library_id)]}
    )
    with pytest.raises(CanonicalMappingRolloutError, match="authorization_invalid"):
        _run(authorization=forged, cases=cases)


def test_canary_suite_requires_exact_three_named_identities_once_each():
    cases = _cases()
    library_id = cases[0].input.library_id
    mapper = _FakeMapper()
    forged = tuple(
        case.model_copy(update={"case_key": key})
        for case, key in zip(cases, ("alpha", "beta", "gamma"), strict=True)
    )
    with pytest.raises(CanonicalMappingRolloutError, match="canary_case_identity_invalid"):
        _run(authorization=_authorization(library_id), cases=forged, mapper=mapper)
    assert mapper.calls == []

    duplicated = (cases[0], cases[1].model_copy(update={"case_key": "asset"}), cases[2])
    with pytest.raises(CanonicalMappingRolloutError, match="canary_case_duplicate"):
        _run(authorization=_authorization(library_id), cases=duplicated, mapper=mapper)
    assert mapper.calls == []

    for wrong_count in (cases[:2], (*cases, cases[0])):
        with pytest.raises(CanonicalMappingRolloutError, match="canary_case_count_invalid"):
            _run(authorization=_authorization(library_id), cases=wrong_count, mapper=mapper)
    assert mapper.calls == []


def test_isolated_three_domain_canary_is_bounded_and_keeps_metric_families_separate():
    cases = _cases()
    library_id = cases[0].input.library_id
    mapper = _FakeMapper()
    persistence = _FakePersistence()
    artifact, gold = _replay_fixture()

    report = _run(
        authorization=_authorization(library_id),
        cases=tuple(reversed(cases)),
        mapper=mapper,
        persistence=persistence,
        replay_artifact=artifact,
        raw_gold=gold,
    )

    assert report.status == "succeeded"
    assert tuple(row.case_key for row in report.outcomes) == ("asset", "legal", "medical")
    assert mapper.calls == ["asset", "legal", "medical"]
    assert len(persistence.calls) == 3
    assert all(row.bridge_effect == "projected" for row in report.outcomes)
    raw = report.raw_extraction_metrics.optional_raw_gold_metrics.metrics
    assert raw is not None
    assert raw.overall.raw_relation_precision.value == 1
    assert raw.overall.raw_relation_recall.value == 1
    canonical = report.canonical_mapping_metrics
    assert canonical is not None
    assert canonical.mapping_accuracy.value == 1.0
    assert canonical.mapping_coverage.value == 1.0
    assert canonical.overall.ignored_decision_candidate_count == 0
    operational = report.operational_metrics
    assert operational.mapper_call_count == 3
    assert operational.persistence_call_count == 3
    assert operational.mapped_count == 3
    assert operational.endpoint_validity.value == 1.0
    assert operational.direction_validity.value == 1.0
    assert operational.evidence_validity.value == 1.0
    assert operational.total_latency_ms == 30
    assert operational.total_cost_microunits == 300
    assert report.replay_report is not None
    assert report.replay_report.canonical_metrics.metrics is not None


def test_report_order_bytes_and_hash_are_deterministic_and_leak_scanned():
    cases = _cases()
    library_id = cases[0].input.library_id
    artifact, gold = _replay_fixture()
    kwargs = {
        "authorization": _authorization(library_id),
        "replay_artifact": artifact,
        "raw_gold": gold,
    }

    first = _run(cases=cases, mapper=_FakeMapper(), **kwargs)
    second = _run(cases=tuple(reversed(cases)), mapper=_FakeMapper(), **kwargs)
    first_json = canonical_mapping_canary_report_json(
        first,
        forbidden_values=("Alpha supports Beta", "fixture.txt"),
    )
    second_json = canonical_mapping_canary_report_json(second)

    assert first_json == second_json
    assert canonical_mapping_canary_report_sha256(first) == canonical_mapping_canary_report_sha256(
        second
    )
    assert "Alpha supports Beta" not in first_json
    assert "fixture.txt" not in first_json
    with pytest.raises(CanonicalMappingRolloutError, match="artifact_leak_detected"):
        canonical_mapping_canary_report_json(first, forbidden_values=("not_authorized",))


def _report_with_metric_value(report, value):
    section = report.replay_report.canonical_metrics.model_copy(
        update={"metrics": {"nested": value}, "unavailable_reason": None}
    )
    replay_report = report.replay_report.model_copy(update={"canonical_metrics": section})
    return report.model_copy(update={"replay_report": replay_report})


@pytest.mark.parametrize(
    "secret",
    [
        'quoted "secret"',
        "path\\secret\\value",
        "line one\nline two",
        "密钥-不可泄漏",
        "postgresql://user:password@127.0.0.1/private",
        "synthetic exception: private payload",
    ],
)
def test_leak_scan_walks_decoded_nested_strings(secret):
    cases = _cases()
    report = _run(
        authorization=_authorization(cases[0].input.library_id),
        cases=cases,
        mapper=_FakeMapper(),
        replay_artifact=_replay_fixture()[0],
    )
    forged = _report_with_metric_value(report, f"prefix {secret} suffix")
    with pytest.raises(CanonicalMappingRolloutError, match="artifact_leak_detected"):
        canonical_mapping_canary_report_json(forged, forbidden_values=(secret,))


@pytest.mark.parametrize("as_object", [False, True])
@pytest.mark.parametrize("payload_kind", ["input", "result"])
def test_leak_scan_rejects_complete_canonical_payload_as_string_or_subtree(
    as_object,
    payload_kind,
):
    cases = _cases()
    input_value = cases[0].input
    payload = (
        canonical_mapping_input_json(input_value)
        if payload_kind == "input"
        else canonical_mapping_json(input_value, cases[0].expected)
    )
    report = _run(
        authorization=_authorization(input_value.library_id),
        cases=cases,
        mapper=_FakeMapper(),
        replay_artifact=_replay_fixture()[0],
    )
    forged = _report_with_metric_value(report, json.loads(payload) if as_object else payload)
    with pytest.raises(CanonicalMappingRolloutError, match="artifact_leak_detected"):
        canonical_mapping_canary_report_json(forged, forbidden_values=(payload,))


def test_leak_sentinel_budget_is_bounded_without_fingerprint_false_positive():
    cases = _cases()
    report = _run(
        authorization=_authorization(cases[0].input.library_id),
        cases=cases,
        mapper=_FakeMapper(),
    )
    unit = "x" * MAX_LEAK_SENTINEL_BYTES
    canonical_mapping_canary_report_json(report, forbidden_values=(unit, unit))
    assert MAX_LEAK_SENTINEL_TOTAL_BYTES == 2 * MAX_LEAK_SENTINEL_BYTES
    with pytest.raises(CanonicalMappingRolloutError, match="leak_sentinels_invalid"):
        canonical_mapping_canary_report_json(report, forbidden_values=(unit + "x",))
    with pytest.raises(CanonicalMappingRolloutError, match="leak_sentinels_invalid"):
        canonical_mapping_canary_report_json(
            report,
            forbidden_values=(unit, "y" * (MAX_LEAK_SENTINEL_BYTES - 1), "zz"),
        )
    canonical_mapping_canary_report_json(
        report,
        forbidden_values=("f" * 64, str(uuid.uuid4())),
    )


async def _timeout(_case):
    await asyncio.sleep(0.05)
    raise AssertionError("cancelled timeout must not resume")


@pytest.mark.parametrize("mapper_mode", ["swallow_once", "late_return"])
def test_hard_timeout_cleans_mapper_without_external_release(mapper_mode):
    cases = _cases()
    persistence = _FakePersistence()
    bridge = Mock()

    async def scenario():
        first_cancelled = asyncio.Event()
        second_cancelled = asyncio.Event()
        cancel_count = 0
        late_finished = False

        async def mapper(request):
            nonlocal cancel_count, late_finished
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancel_count += 1
                first_cancelled.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancel_count += 1
                    second_cancelled.set()
                    if mapper_mode == "swallow_once":
                        raise
                    late_finished = True
                    return CanonicalMappingCanaryMapperOutputV1(
                        result=_mapped(request.input),
                        latency_ms=1,
                        cost_microunits=1,
                    )

        started = asyncio.get_running_loop().time()
        report = await asyncio.wait_for(
            run_canonical_mapping_canary_suite(
                authorization=_authorization(cases[0].input.library_id),
                library_id=cases[0].input.library_id,
                cases=cases,
                mapper=mapper,
                persistence=persistence,
                limits=CanonicalMappingCanaryLimitsV1(
                    timeout_seconds=0.01,
                    max_mapper_calls=1,
                ),
            ),
            timeout=1.5,
        )
        elapsed = asyncio.get_running_loop().time() - started
        frozen = canonical_mapping_canary_report_json(report)
        assert report.outcomes[0].failure_reason == CanonicalMappingCanaryFailure.MAPPER_TIMEOUT
        assert first_cancelled.is_set()
        assert second_cancelled.is_set()
        assert cancel_count == 2
        assert persistence.calls == []
        assert elapsed < 1.5
        assert late_finished is (mapper_mode == "late_return")
        assert report.outcomes[0].mapping_result_id is None
        assert report.outcomes[0].mapping_result_fingerprint is None
        assert report.operational_metrics.mapped_count == 0
        assert report.operational_metrics.persistence_call_count == 0
        assert report.canonical_mapping_metrics is None
        assert canonical_mapping_canary_report_json(report) == frozen
        assert bridge.call_count == 0
        assert asyncio.all_tasks() == {asyncio.current_task()}

    with patch(
        "app.services.canonical_mapping_rollout.project_canonical_mapping_to_candidate_sidecar",
        bridge,
    ):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        (_timeout, CanonicalMappingCanaryFailure.MAPPER_TIMEOUT),
        ({"result": "not typed"}, CanonicalMappingCanaryFailure.MALFORMED_OUTPUT),
    ],
)
def test_timeout_and_malformed_output_fail_closed_without_fake_canonical_metrics(
    override,
    expected_reason,
):
    cases = _cases()
    library_id = cases[0].input.library_id
    mapper = _FakeMapper(overrides={"asset": override})
    artifact, gold = _replay_fixture()

    report = _run(
        authorization=_authorization(library_id),
        cases=cases,
        mapper=mapper,
        limits=CanonicalMappingCanaryLimitsV1(timeout_seconds=0.01),
        replay_artifact=artifact,
        raw_gold=gold,
    )

    assert report.status == "failed"
    assert report.outcomes[0].failure_reason == expected_reason
    assert report.canonical_mapping_metrics is None
    assert report.canonical_metrics_unavailable_reason == "canary_execution_failed"
    assert report.replay_report is not None
    assert report.replay_report.canonical_metrics.metrics is None
    assert report.replay_report.canonical_metrics.unavailable_reason == (
        "canonical_metrics_not_provided"
    )
    assert report.raw_extraction_metrics.optional_raw_gold_metrics.metrics is not None


def test_evidence_authority_and_persistence_failures_are_isolated():
    cases = _cases()
    library_id = cases[0].input.library_id
    forged = cases[0].expected.model_copy(update={"mapping_evidence_ref_ids": ("missing",)})
    mapper = _FakeMapper(
        overrides={
            "asset": CanonicalMappingCanaryMapperOutputV1(
                result=forged,
                latency_ms=1,
                cost_microunits=2,
            )
        }
    )
    report = _run(
        authorization=_authorization(library_id),
        cases=cases,
        mapper=mapper,
    )
    assert report.outcomes[0].failure_reason == (
        CanonicalMappingCanaryFailure.RESULT_AUTHORITY_INVALID
    )

    persistence = _FakePersistence(fail_case_id=cases[1].expected.mapping_result_id)
    report = _run(
        authorization=_authorization(library_id),
        cases=cases,
        mapper=_FakeMapper(),
        persistence=persistence,
    )
    assert report.outcomes[1].failure_reason == CanonicalMappingCanaryFailure.PERSISTENCE_FAILED
    assert report.operational_metrics.persistence_call_count == 3
    assert report.canonical_mapping_metrics is None


def test_unknown_ambiguity_blocked_and_rejection_are_retained_as_business_outcomes():
    ordinary = _cases()
    unknown_input = _input(
        _plain_claim(
            claim_id=uuid.uuid4(),
            extraction_occurrence_id=uuid.uuid4(),
            surface_direction="unknown",
        )
    )
    unknown = _build(
        unknown_input,
        outcome="ambiguous",
        reason_code="unknown_direction",
        mapping_confidence=None,
        semantic_status="ambiguous",
        auto_proposal=False,
    )
    blocked = _build(
        ordinary[1].input,
        outcome="blocked",
        reason_code="no_explicit_mapping",
        mapping_confidence=None,
        semantic_status="blocked",
        auto_proposal=False,
    )
    rejected = _build(
        ordinary[2].input,
        outcome="rejected",
        reason_code="ontology_relation_not_allowed",
        mapping_confidence=None,
        semantic_status="preserved",
        auto_proposal=False,
    )
    cases = (
        CanonicalMappingCanaryCaseV1(case_key="asset", input=unknown_input, expected=unknown),
        CanonicalMappingCanaryCaseV1(
            case_key="legal",
            input=ordinary[1].input,
            expected=blocked,
        ),
        CanonicalMappingCanaryCaseV1(
            case_key="medical",
            input=ordinary[2].input,
            expected=rejected,
        ),
    )
    report = _run(
        authorization=_authorization(unknown_input.library_id),
        cases=cases,
        mapper=_FakeMapper(
            overrides={
                "asset": CanonicalMappingCanaryMapperOutputV1(
                    result=unknown,
                    latency_ms=1,
                    cost_microunits=1,
                ),
                "legal": CanonicalMappingCanaryMapperOutputV1(
                    result=blocked,
                    latency_ms=1,
                    cost_microunits=1,
                ),
                "medical": CanonicalMappingCanaryMapperOutputV1(
                    result=rejected,
                    latency_ms=1,
                    cost_microunits=1,
                ),
            }
        ),
    )

    assert report.status == "succeeded"
    assert [row.mapping_outcome for row in report.outcomes] == [
        "ambiguous",
        "blocked",
        "rejected",
    ]
    assert report.operational_metrics.ambiguous_count == 1
    assert report.operational_metrics.blocked_count == 1
    assert report.operational_metrics.rejected_count == 1
    assert report.operational_metrics.unknown_retained_count == 1
    assert report.operational_metrics.failure_count == 0
    assert report.canonical_mapping_metrics is None
    assert report.canonical_metrics_unavailable_reason == "canonical_gold_not_provided"


def test_missing_canonical_gold_is_unavailable_not_zero():
    base = _cases()
    cases = tuple(
        CanonicalMappingCanaryCaseV1(case_key=case.case_key, input=case.input)
        for case in base
    )
    outputs = {
        case.case_key: CanonicalMappingCanaryMapperOutputV1(
            result=base[index].expected,
            latency_ms=1,
            cost_microunits=1,
        )
        for index, case in enumerate(cases)
    }
    report = _run(
        authorization=_authorization(base[0].input.library_id),
        cases=cases,
        mapper=_FakeMapper(overrides=outputs),
    )

    assert report.status == "succeeded"
    assert report.canonical_mapping_metrics is None
    assert report.canonical_metrics_unavailable_reason == "canonical_gold_not_provided"


def test_call_limit_stops_before_an_unbounded_mapper_run():
    cases = _cases()
    library_id = cases[0].input.library_id
    mapper = _FakeMapper()

    report = _run(
        authorization=_authorization(library_id),
        cases=cases,
        mapper=mapper,
        limits=CanonicalMappingCanaryLimitsV1(max_mapper_calls=2),
    )

    assert mapper.calls == ["asset", "legal"]
    assert report.outcomes[2].failure_reason == CanonicalMappingCanaryFailure.CALL_LIMIT_REACHED
    assert report.operational_metrics.mapper_call_count == 2


def test_scope_leakage_and_forged_authorization_fail_before_mapper_call():
    cases = _cases()
    mapper = _FakeMapper()
    with pytest.raises(CanonicalMappingRolloutError, match="canary_scope_leakage"):
        asyncio.run(
            run_canonical_mapping_canary_suite(
                authorization=_authorization(cases[0].input.library_id),
                library_id=uuid.uuid4(),
                cases=cases,
                mapper=mapper,
            )
        )
    assert mapper.calls == []

    forged = CanonicalMappingRolloutAuthorizationV1.model_construct(
        global_execution_enabled=True,
        authorized_library_ids=(cases[0].input.library_id,),
        read_visibility_enabled=False,
        bridge_visibility_enabled=True,
        authorization_version=1,
        schema_version="canonical_mapping_rollout_authorization_v1",
    )
    with pytest.raises(CanonicalMappingRolloutError, match="authorization_invalid"):
        _run(authorization=forged, cases=cases, mapper=mapper)
    assert mapper.calls == []


def test_rollback_disables_execution_read_and_bridge_idempotently():
    cases = _cases()
    authorization = _authorization(cases[0].input.library_id)

    first = rollback_canonical_mapping_rollout(authorization)
    second = rollback_canonical_mapping_rollout(first)

    assert first == second
    assert first.global_execution_enabled is False
    assert first.authorized_library_ids == ()
    assert first.read_visibility_enabled is False
    assert first.bridge_visibility_enabled is False
    mapper = _FakeMapper()
    report = _run(authorization=second, cases=cases, mapper=mapper)
    assert report.status == "disabled"
    assert mapper.calls == []


def test_canary_calls_no_candidate_materializer_publication_or_fact_writer():
    cases = _cases()
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
            spy = Mock(side_effect=AssertionError("forbidden M5 write path"))
            stack.enter_context(patch(target, spy))
            spies.append(spy)
        report = _run(
            authorization=_authorization(cases[0].input.library_id),
            cases=cases,
            mapper=_FakeMapper(),
        )
    assert report.status == "succeeded"
    assert all(spy.call_count == 0 for spy in spies)


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def test_rollout_module_has_no_db_provider_worker_or_writer_dependency():
    module = Path(__file__).parents[1] / "app" / "services" / "canonical_mapping_rollout.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module == "sqlalchemy" or module.startswith("app.models") for module in imports)
    assert not any(
        token in module
        for module in imports
        for token in ("provider", "worker", "materializer", "publication", "graph_candidate")
    )
    forbidden = {
        "stage_unit_candidate_occurrences",
        "materialize_graph_extraction_job",
        "plan_graph_publication",
        "activate_graph_publication",
        "create_entity",
        "create_relation",
        "select",
        "insert",
        "update",
        "delete",
        "commit",
        "flush",
    }
    calls = {
        name.rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if (name := _dotted_name(node.func)) is not None
    }
    assert not calls & forbidden


def test_rollout_module_has_no_production_import_caller():
    root = Path(__file__).parents[1]
    target = "app.services.canonical_mapping_rollout"
    callers = set()
    for path in (*root.joinpath("app").rglob("*.py"), *root.joinpath("tests").rglob("*.py")):
        if path == root / "app" / "services" / "canonical_mapping_rollout.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom) and node.module == target
            or isinstance(node, ast.Import)
            and any(alias.name == target for alias in node.names)
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(root).as_posix())
    assert callers == {
        "app/services/canonical_mapping_shadow.py",
        "tests/test_canonical_mapping_rollout.py",
    }
