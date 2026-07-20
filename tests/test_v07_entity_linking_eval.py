from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eval.entity_linking.contracts import (
    GATE_ID_ORDER,
    CalibrationArtifact,
    FrozenPolicy,
    GateDecision,
    GridResult,
    FeasibilityManifest,
    GoldDataset,
    IdentityTimeCleanupDecision,
    OrdinalArtifactRef,
    OrdinalResponseHash,
    PolicyApprovalPayload,
    PostFreezeArtifact,
    ReducedRational,
    ReleaseEvidence,
    RunGateDecision,
    Threshold,
    canonical_sha256,
    load_canonical_json,
)
from eval.entity_linking.reference_scorer import (
    EntityCandidate,
    character_bigram_dice_micros,
    resolve_mention,
    score_normalized_pair,
)
from eval.entity_linking.runtime import (
    CALIBRATION_RUN_ID,
    G2_APPROVAL_COMMIT,
    G2_SPECIFICATION_TREE_SHA256,
    EntityLinkingEvalError,
    assert_private_data_absent,
    build_dependency_closure,
    build_gate_decisions,
    discover_external_distribution_records,
    build_grid_results,
    external_distribution_records,
    load_dataset,
    normalize_service_origin,
    paired_stratified_bootstrap,
    qdrant_collection_identity,
    repository_module_is_local,
    select_calibration_threshold,
    upgrade_database,
    verify_g2_approval,
    require_replacement_calibration_reference,
)
from scripts.entity_linking_feasibility import _parser


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
COMMIT = "a" * 40


def _artifact_ref_value(path: str) -> dict[str, str]:
    return {
        "repository_relative_path": path,
        "canonical_sha256": SHA,
        "exact_file_sha256": SHA,
    }


def _passing_gate_values() -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for gate_id in GATE_ID_ORDER:
        value: dict[str, object] = {
            "gate_id": gate_id,
            "value_type": "integer",
            "comparison": "eq",
            "observed_integer": 0,
            "threshold_integer": 0,
            "observed_rational": None,
            "threshold_rational": None,
            "observed_boolean": None,
            "threshold_boolean": None,
            "observed_sha256": None,
            "threshold_sha256": None,
            "passed": True,
        }
        if gate_id == "bootstrap-ci-lower":
            value.update(
                value_type="rational",
                comparison="gt",
                observed_integer=None,
                threshold_integer=None,
                observed_rational={"numerator": 1, "denominator": 1},
                threshold_rational={"numerator": 0, "denominator": 1},
            )
        elif gate_id == "deterministic-response-hash":
            value.update(
                value_type="sha256",
                comparison="all_equal",
                observed_integer=None,
                threshold_integer=None,
                observed_sha256=SHA,
                threshold_sha256=SHA,
            )
        values.append(value)
    return values


def _v2_calibration_value() -> dict[str, object]:
    value = json.loads(
        (ROOT / "eval/entity_linking/results/v07-el-calibration-v2-20260720-01.json").read_text(
            encoding="utf-8"
        )
    )
    value.update(
        schema_version="entity-linking-eval-result-v2",
        run_id=CALIBRATION_RUN_ID,
        database_id="vkt_v07_el_eval_calibration_v3_20260720_01",
        g2_approval_commit=G2_APPROVAL_COMMIT,
        g2_specification_tree_sha256=G2_SPECIFICATION_TREE_SHA256,
        selection_reason="tie-higher-score",
    )
    for row in value["grid_results"]:
        row["utility_question_execution_coverage"] = {
            "numerator": 40,
            "denominator": 40,
            "value_micros": 1_000_000,
        }
    value["performance"].update(
        linker_scenario="linker-mixed-10",
        link_graph_scenario="link-graph-linked-10",
        control_scenario="link-graph-linked-10",
        linker_graph_execution_count=0,
        link_graph_execution_count=30,
    )
    link_graph_samples = [sample + 1 for sample in value["performance"]["link_graph"]["samples_us"]]
    ordered = sorted(link_graph_samples)
    value["performance"]["link_graph"].update(
        samples_us=link_graph_samples,
        p50_us=ordered[14],
        p95_us=ordered[28],
        max_us=ordered[-1],
    )
    return value


def _release_value() -> dict[str, object]:
    refs = [
        {
            "ordinal": ordinal,
            "run_id": f"v07-el-post-freeze-v3-20260720-0{ordinal}",
            "artifact_ref": _artifact_ref_value(
                f"eval/entity_linking/results/v07-el-post-freeze-v3-20260720-0{ordinal}.json"
            ),
        }
        for ordinal in (1, 2, 3)
    ]
    decisions = [
        {
            **ref,
            "gate_decisions": _passing_gate_values(),
            "all_passed": True,
        }
        for ref in refs
    ]
    identities = {field: True for field in IdentityTimeCleanupDecision.model_fields}
    return {
        "schema_version": "entity-linking-release-evidence-v2",
        "status": "passed",
        "g2_approval_commit": COMMIT,
        "g2_specification_tree_sha256": SHA,
        "dataset_manifest_ref": _artifact_ref_value("eval/entity_linking/manifests/feasibility_v2.json"),
        "dataset_content_sha256": SHA,
        "evaluation_config_sha256": SHA,
        "ontology_schema_set_hash": SHA,
        "code_commit": COMMIT,
        "evaluation_tree_sha256": SHA,
        "accepted_dependency_closure_sha256": SHA,
        "reference_scorer_sha256": SHA,
        "external_distribution_set_sha256": SHA,
        "control_config_sha256": SHA,
        "environment_fingerprint_sha256": SHA,
        "pg_cluster_fingerprint_sha256": SHA,
        "qdrant_fingerprint_sha256": SHA,
        "embedding_fingerprint_sha256": SHA,
        "calibration_ref": _artifact_ref_value(
            "eval/entity_linking/results/v07-el-calibration-v3-20260720-01.json"
        ),
        "policy_ref": _artifact_ref_value("eval/entity_linking/link_policy_v2.json"),
        "post_freeze_refs": refs,
        "canonical_response_set_sha256_by_ordinal": [
            {
                "ordinal": ref["ordinal"],
                "run_id": ref["run_id"],
                "canonical_response_set_sha256": SHA,
            }
            for ref in refs
        ],
        "run_gate_decisions": decisions,
        "identity_time_cleanup_decisions": identities,
        "final_hard_and_decision": "GO_ELIGIBLE",
    }


def test_g2_approval_identity_specification_tree_and_ancestry_are_frozen():
    identity = verify_g2_approval(ROOT)
    assert identity == {
        "g2_approval_commit": G2_APPROVAL_COMMIT,
        "g2_specification_tree_sha256": G2_SPECIFICATION_TREE_SHA256,
    }


def test_fixed_dataset_is_canonical_complete_and_family_disjoint():
    dataset = load_dataset(ROOT)
    assert dataset.manifest.g2_approval_commit == G2_APPROVAL_COMMIT
    assert dataset.manifest.accepted_dependency_closure_path_count == 63
    assert dataset.manifest.counts.questions == 200
    assert dataset.manifest.counts.calibration_cases == 80
    assert dataset.manifest.counts.release_cases == 120
    assert dataset.manifest.counts.calibration_safety_cases == 40
    assert dataset.manifest.counts.calibration_utility_cases == 40
    assert dataset.manifest.counts.release_safety_cases == 40
    assert dataset.manifest.counts.release_utility_cases == 80
    assert all(
        len(case.decoy_chunk_keys) == (12 if case.cohort == "utility" else 0)
        for case in dataset.cases
    )
    assert dataset.manifest.counts.linkable_mentions >= 240
    assert dataset.manifest.counts.ambiguous_unlinkable_mentions >= 60
    assert min(dataset.manifest.release_category_counts.root.values()) >= 20
    assert min(dataset.manifest.release_stratum_counts.root.values()) >= 20
    assert len(dataset.dataset_content_sha256) == 64
    assert len(dataset.evaluation_config_sha256) == 64
    chunks = {row.chunk_key: row for row in dataset.gold.chunks}
    documents = {row.document_key: row for row in dataset.gold.documents}
    for case in dataset.cases:
        for chunk_key in case.decoy_chunk_keys:
            chunk = chunks[chunk_key]
            visible = f"{documents[chunk.document_key].title} {chunk.text}".casefold()
            assert "gold" not in visible
            assert "decoy" not in visible


def test_calibration_dataset_load_does_not_score_release_holdout(monkeypatch):
    import eval.entity_linking.runtime as runtime

    observed_splits = []
    original = runtime.recompute_case_categories

    def record_split(case, gold):
        observed_splits.append(case.split)
        return original(case, gold)

    monkeypatch.setattr(runtime, "recompute_case_categories", record_split)
    runtime.load_dataset.cache_clear()
    runtime.load_dataset(ROOT)
    runtime.load_dataset.cache_clear()
    assert observed_splits == ["calibration"] * 80


def test_canonical_loader_rejects_pretty_json_duplicate_keys_crlf_and_extra(tmp_path):
    source = ROOT / "eval/entity_linking/gold_v2.json"
    value = json.loads(source.read_text(encoding="utf-8"))
    pretty = tmp_path / "pretty.json"
    pretty.write_bytes((json.dumps(value, indent=2) + "\n").encode("utf-8"))
    with pytest.raises(ValueError, match="noncanonical"):
        load_canonical_json(tmp_path, pretty, GoldDataset)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(b'{"schema_version":"a","schema_version":"b"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        load_canonical_json(tmp_path, duplicate, GoldDataset)

    crlf = tmp_path / "crlf.json"
    crlf.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="CRLF"):
        load_canonical_json(tmp_path, crlf, GoldDataset)

    value["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        GoldDataset.model_validate(value)


def test_reference_scorer_uses_integer_features_exact_first_and_stable_abstention():
    assert character_bigram_dice_micros("a", "a") == 1_000_000
    score = score_normalized_pair("alpha beta", "beta alpha")
    assert score.token_jaccard_micros == 1_000_000
    assert score.score_micros == 1_000_000
    boundary = score_normalized_pair("star lab", "star laboratory")
    assert boundary.boundary_omission_micros >= 850_000
    abbreviation = score_normalized_pair("aramberor", "alderamberorbit")
    assert abbreviation.ordered_abbreviation_micros >= 850_000
    candidate = EntityCandidate(
        entity_id="00000000-0000-0000-0000-000000000001",
        entity_key="candidate-alpha",
        entity_type_key="organization",
        canonical_name="Alpha Unit",
        normalized_name="alpha unit",
    )
    exact = resolve_mention(
        "  ALPHA   UNIT ",
        (candidate,),
        min_score_micros=950000,
        min_margin_micros=200000,
    )
    assert exact.status == "linked"
    assert exact.method == "exact_canonical"
    assert exact.selected is not None
    assert exact.selected.features.score_micros == 1_000_000


def test_conformance_grid_and_selection_are_deterministic():
    dataset = load_dataset(ROOT)
    grid = build_grid_results(dataset)
    assert len(grid) == 25
    assert [row.grid_index for row in grid] == list(range(25))
    selected, reason = select_calibration_threshold(grid)
    assert reason in {
        "max-question-execution-coverage",
        "tie-non-exact-coverage",
        "tie-higher-score",
        "tie-higher-margin",
        "no-valid-candidate",
    }
    assert (selected is None) == (reason == "no-valid-candidate")
    assert selected == Threshold(
        min_score_micros=920000,
        min_margin_micros=200000,
        candidate_floor_micros=500000,
        max_candidates=10,
    )
    assert next(row for row in grid if row.thresholds == selected).utility_question_execution_coverage.numerator == 40
    assert len(dataset.conformance.decision_cases) == 30
    selected_row = next(row for row in grid if row.thresholds == selected)
    mutated = selected_row.model_dump(mode="json")
    mutated["utility_question_execution_coverage"] = {
        "numerator": 31,
        "denominator": 40,
        "value_micros": 775000,
    }
    with pytest.raises(ValidationError, match="selection eligibility"):
        GridResult.model_validate(mutated)


def test_dependency_closure_and_distribution_identity_are_exact():
    closure = build_dependency_closure(ROOT)
    assert len(closure) == 63
    assert closure[0]["repository_relative_path"] == "app/__init__.py"
    assert repository_module_is_local(ROOT, "app")
    assert repository_module_is_local(ROOT, "eval")
    assert repository_module_is_local(ROOT, "scripts")
    assert repository_module_is_local(ROOT, "tests")
    assert not repository_module_is_local(ROOT, "pydantic_settings")
    distributions = discover_external_distribution_records(ROOT)
    assert distributions == external_distribution_records()
    assert len(distributions) == 12
    assert [row["distribution_name"] for row in distributions] == sorted(
        row["distribution_name"] for row in distributions
    )
    assert {row["distribution_name"] for row in distributions} >= {
        "alembic",
        "fastapi-users-db-sqlalchemy",
        "pydantic-settings",
    }


def test_distribution_manifest_rejects_length_order_duplicates_and_wrong_version():
    dataset = load_dataset(ROOT)
    valid = dataset.manifest.model_dump(mode="json")
    records = valid["external_distribution_records"]

    for mutated_records in (
        records[:-1],
        [*records, records[-1]],
        [records[1], records[0], *records[2:]],
        [records[0], *records[2:], records[-1]],
    ):
        mutated = {**valid, "external_distribution_records": mutated_records}
        with pytest.raises(ValidationError):
            FeasibilityManifest.model_validate(mutated)

    wrong_version = [dict(record) for record in records]
    target = next(
        record for record in wrong_version if record["distribution_name"] == "fastapi-users-db-sqlalchemy"
    )
    target["exact_version"] = "7.0.1"
    with pytest.raises(ValidationError):
        FeasibilityManifest.model_validate({**valid, "external_distribution_records": wrong_version})


def test_alembic_upgrade_is_bound_to_the_disposable_database(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["cwd"] = kwargs["cwd"]
        observed["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("eval.entity_linking.runtime.subprocess.run", fake_run)
    upgrade_database(
        ROOT,
        "postgresql+asyncpg://eval-user:eval-password@127.0.0.1:5544/vkt_v07_el_eval_preflight_20260717_01",
    )

    assert observed["command"][-2:] == ("upgrade", "0023")
    assert observed["cwd"] == ROOT
    assert observed["environment"]["DB_HOST"] == "127.0.0.1"
    assert observed["environment"]["DB_PORT"] == "5544"
    assert observed["environment"]["DB_USER"] == "eval-user"
    assert observed["environment"]["DB_NAME"] == "vkt_v07_el_eval_preflight_20260717_01"

    upgrade_database(
        ROOT,
        "postgresql+asyncpg://eval-user@127.0.0.1:5544/vkt_v07_el_eval_preflight_20260717_01",
    )
    assert observed["environment"]["DB_PASSWORD"] == ""


def test_paired_bootstrap_is_exact_rational_and_reproducible():
    candidate = {}
    control = {}
    strata = {}
    for index in range(80):
        case_id = f"release-{index:03d}"
        candidate[case_id] = 800000
        control[case_id] = 600000
        strata[case_id] = (
            "one-hop-cjk",
            "one-hop-latin-mixed",
            "two-hop-cjk",
            "two-hop-latin-mixed",
        )[index % 4]
    left = paired_stratified_bootstrap(
        candidate_recall_micros=candidate,
        control_recall_micros=control,
        case_strata=strata,
        best_control="dense",
    )
    right = paired_stratified_bootstrap(
        candidate_recall_micros=candidate,
        control_recall_micros=control,
        case_strata=strata,
        best_control="dense",
    )
    assert left == right
    assert left.point_estimate_micros == ReducedRational(numerator=200000, denominator=1)
    assert left.ci_lower_micros.numerator > 0
    assert left.passed


def test_qdrant_collection_and_service_origin_conformance_vectors():
    collection = qdrant_collection_identity(CALIBRATION_RUN_ID, "cal")
    assert collection.collection_name == "vkt_v07_el_cal_c8388773952f"
    assert collection.collection_name_sha256 == (
        "d6aedd17cc7a671053b580d8646a92716200b7f15a3a24268658156ea1b64166"
    )
    assert normalize_service_origin("HTTP://Example.COM", qdrant=True) == "http://example.com:80"
    assert (
        normalize_service_origin("http://[2001:0DB8:0:0::1]:6333/", qdrant=True)
        == "http://[2001:db8::1]:6333"
    )
    with pytest.raises(EntityLinkingEvalError):
        normalize_service_origin("http://user@example.com/", qdrant=True)


def test_privacy_scanner_rejects_dataset_values_canaries_and_private_fields():
    dataset = load_dataset(ROOT)
    assert_private_data_absent(
        {
            "endpoint_origin_sha256": "a" * 64,
            "per_mention_sql_count": 0,
            "run_id": "v07-el-calibration-v1-20260717-01",
            "status": "passed",
        },
        dataset=dataset,
    )
    with pytest.raises(EntityLinkingEvalError, match="privacy_leak_detected"):
        assert_private_data_absent(
            {"question": dataset.cases[0].question},
            dataset=dataset,
        )
    with pytest.raises(EntityLinkingEvalError, match="privacy_leak_detected"):
        assert_private_data_absent(
            {"properties": {"safe": "value"}},
            dataset=dataset,
        )


def test_policy_approval_and_frozen_policy_contracts_are_strict():
    calibration_ref = _artifact_ref_value(
        "eval/entity_linking/results/v07-el-calibration-v3-20260720-01.json"
    )
    thresholds = {
        "min_score_micros": 850000,
        "min_margin_micros": 80000,
        "candidate_floor_micros": 500000,
        "max_candidates": 10,
    }
    approval = PolicyApprovalPayload.model_validate(
        {
            "schema_version": "entity-linking-policy-approval-v2",
            "calibration_ref": calibration_ref,
            "approved_thresholds": thresholds,
            "approved_by": "alice",
            "approved_at": "2026-07-20T10:00:00.000000Z",
            "approval_reference": "v07-gate-b-review",
        }
    )
    policy_value = {
        "schema_version": "entity-linking-policy-v2",
        "policy_version": "entity-linking-policy-v2",
        "algorithm_version": "lexical-score-v2",
        "normalization_version": "normalize_graph_name_v1",
        "g2_approval_commit": COMMIT,
        "g2_specification_tree_sha256": SHA,
        "calibration_ref": calibration_ref,
        "approved_thresholds": thresholds,
        "approval_payload_sha256": canonical_sha256(approval),
        "dataset_manifest_ref": _artifact_ref_value("eval/entity_linking/manifests/feasibility_v2.json"),
        "dataset_content_sha256": SHA,
        "evaluation_config_sha256": SHA,
        "ontology_schema_set_hash": SHA,
        "code_commit": COMMIT,
        "evaluation_tree_sha256": SHA,
        "accepted_dependency_closure_sha256": SHA,
        "reference_scorer_sha256": SHA,
        "external_distribution_set_sha256": SHA,
        "control_config_sha256": SHA,
        "environment_fingerprint_sha256": SHA,
        "pg_cluster_fingerprint_sha256": SHA,
        "qdrant_fingerprint_sha256": SHA,
        "embedding_fingerprint_sha256": SHA,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
        "approval_reference": approval.approval_reference,
    }
    assert FrozenPolicy.model_validate(policy_value).approved_thresholds == Threshold(**thresholds)
    with pytest.raises(ValidationError):
        PolicyApprovalPayload.model_validate({**approval.model_dump(mode="json"), "unexpected": True})
    with pytest.raises(ValidationError):
        PolicyApprovalPayload.model_validate(
            {
                **approval.model_dump(mode="json"),
                "approved_at": "2026-99-20T10:00:00.000000Z",
            }
        )
    with pytest.raises(ValidationError):
        FrozenPolicy.model_validate({**policy_value, "approved_by": ""})


def test_gate_decisions_reject_wrong_typed_pairs_order_and_derived_values():
    gates = tuple(GateDecision.model_validate(row) for row in _passing_gate_values())
    assert tuple(gate.gate_id for gate in gates) == GATE_ID_ORDER
    invalid_pair = _passing_gate_values()[0]
    invalid_pair["observed_boolean"] = True
    with pytest.raises(ValidationError):
        GateDecision.model_validate(invalid_pair)
    wrong_result = _passing_gate_values()[0]
    wrong_result["observed_integer"] = 1
    with pytest.raises(ValidationError):
        GateDecision.model_validate(wrong_result)

    run_value = {
        "ordinal": 1,
        "run_id": "v07-el-post-freeze-v3-20260720-01",
        "artifact_ref": _artifact_ref_value(
            "eval/entity_linking/results/v07-el-post-freeze-v3-20260720-01.json"
        ),
        "gate_decisions": _passing_gate_values(),
        "all_passed": True,
    }
    assert RunGateDecision.model_validate(run_value).all_passed
    with pytest.raises(ValidationError):
        RunGateDecision.model_validate(
            {**run_value, "gate_decisions": list(reversed(_passing_gate_values()))}
        )

    calibration = CalibrationArtifact.model_validate(_v2_calibration_value())
    computed = build_gate_decisions(
        calibration.metrics,
        calibration.performance,
        canonical_response_set_sha256=calibration.canonical_response_set_sha256,
        response_set_threshold_sha256=calibration.canonical_response_set_sha256,
    )
    assert tuple(row.gate_id for row in computed) == GATE_ID_ORDER


def test_post_freeze_contract_requires_ordinal_policy_and_null_calibration_fields():
    value = _v2_calibration_value()
    value.update(
        phase="post_freeze_release",
        ordinal=1,
        run_id="v07-el-post-freeze-v3-20260720-01",
        database_id="vkt_v07_el_eval_post_freeze_v3_20260720_01",
        grid_results=None,
        candidate_thresholds=None,
        selection_reason=None,
        policy_ref=_artifact_ref_value("eval/entity_linking/link_policy_v2.json"),
        qdrant_collection=qdrant_collection_identity("v07-el-post-freeze-v3-20260720-01", "pf1").model_dump(
            mode="json"
        ),
    )
    artifact = PostFreezeArtifact.model_validate(value)
    assert artifact.ordinal == 1
    with pytest.raises(ValidationError):
        PostFreezeArtifact.model_validate({**value, "ordinal": 4})
    with pytest.raises(ValidationError):
        PostFreezeArtifact.model_validate({**value, "grid_results": []})
    copied = _v2_calibration_value()
    copied["performance"]["link_graph"] = copied["performance"]["linker"]
    with pytest.raises(ValidationError, match="independently measured"):
        CalibrationArtifact.model_validate(copied)


def test_release_evidence_rejects_reordered_refs_false_identity_and_extra_fields():
    value = _release_value()
    evidence = ReleaseEvidence.model_validate(value)
    assert evidence.final_hard_and_decision == "GO_ELIGIBLE"
    assert [row.ordinal for row in evidence.post_freeze_refs] == [1, 2, 3]
    assert isinstance(evidence.post_freeze_refs[0], OrdinalArtifactRef)
    assert isinstance(evidence.canonical_response_set_sha256_by_ordinal[0], OrdinalResponseHash)

    reordered = dict(value)
    reordered["post_freeze_refs"] = list(reversed(value["post_freeze_refs"]))
    with pytest.raises(ValidationError):
        ReleaseEvidence.model_validate(reordered)

    false_identity = json.loads(json.dumps(value))
    false_identity["identity_time_cleanup_decisions"]["privacy_scans_passed"] = False
    with pytest.raises(ValidationError):
        ReleaseEvidence.model_validate(false_identity)

    with pytest.raises(ValidationError):
        ReleaseEvidence.model_validate({**value, "self_sha256": SHA})


def test_invalidated_v1_calibration_is_rejected_before_policy_write():
    historical_policy_path = ROOT / "eval/entity_linking/link_policy_v1.json"
    policy_path = ROOT / "eval/entity_linking/link_policy_v2.json"
    assert historical_policy_path.is_file()
    assert not policy_path.exists()
    with pytest.raises(EntityLinkingEvalError, match="invalidated_calibration_artifact"):
        require_replacement_calibration_reference(
            ROOT,
            ROOT / "eval/entity_linking/results/v07-el-calibration-v1-20260717-01.json",
            expected_canonical_sha256=("96757dcd901439828b61605c473a34c2c838ed6755db6f129d030561b156894d"),
            expected_file_sha256=("c35210075e179ebce197275041e0ff69403297185ec2ebcc16a663d7611e9225"),
        )
    assert not policy_path.exists()


def test_complete_cli_surface_is_frozen_before_replacement_calibration():
    parser = _parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    assert tuple(subparsers.choices) == (
        "validate-dataset",
        "verify-reference-scorer",
        "verify-boundary",
        "preflight-live-dependencies",
        "calibrate",
        "verify-calibration",
        "freeze-policy",
        "verify-policy",
        "post-freeze",
        "assemble-release-evidence",
        "verify-release",
    )


def test_no_policy_post_freeze_or_release_artifacts_exist_before_human_approval():
    base = ROOT / "eval/entity_linking"
    assert (base / "link_policy_v1.json").is_file()
    assert not (base / "link_policy_v2.json").exists()
    assert not (base / "release_evidence_v2.json").exists()
    results = base / "results"
    forbidden = (
        "v07-el-calibration-v3-20260720-01.json",
        "v07-el-post-freeze-v3-20260720-01.json",
        "v07-el-post-freeze-v3-20260720-02.json",
        "v07-el-post-freeze-v3-20260720-03.json",
    )
    assert all(not (results / name).exists() for name in forbidden)


def test_no_v07_route_or_migration_and_protected_runtime_remains_unchanged():
    from app.main import app

    assert not any("/v07/" in path for path in app.openapi()["paths"])
    heads = list((ROOT / "alembic/versions").glob("0024*.py"))
    assert not heads
