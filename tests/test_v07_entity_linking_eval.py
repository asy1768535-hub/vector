from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eval.entity_linking.contracts import (
    FeasibilityManifest,
    GoldDataset,
    ReducedRational,
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
)


ROOT = Path(__file__).resolve().parents[1]


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
    assert dataset.manifest.counts.linkable_mentions >= 240
    assert dataset.manifest.counts.ambiguous_unlinkable_mentions >= 60
    assert min(dataset.manifest.release_category_counts.root.values()) >= 20
    assert min(dataset.manifest.release_stratum_counts.root.values()) >= 20
    assert len(dataset.dataset_content_sha256) == 64
    assert len(dataset.evaluation_config_sha256) == 64


def test_canonical_loader_rejects_pretty_json_duplicate_keys_crlf_and_extra(tmp_path):
    source = ROOT / "eval/entity_linking/gold_v1.json"
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
        "max-non-exact-coverage",
        "tie-higher-score",
        "tie-higher-margin",
        "no-valid-candidate",
    }
    assert (selected is None) == (reason == "no-valid-candidate")
    assert len(dataset.conformance.decision_cases) == 30


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
        record
        for record in wrong_version
        if record["distribution_name"] == "fastapi-users-db-sqlalchemy"
    )
    target["exact_version"] = "7.0.1"
    with pytest.raises(ValidationError):
        FeasibilityManifest.model_validate(
            {**valid, "external_distribution_records": wrong_version}
        )


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
        "postgresql+asyncpg://eval-user:eval-password@127.0.0.1:5544/"
        "vkt_v07_el_eval_preflight_20260717_01",
    )

    assert observed["command"][-2:] == ("upgrade", "0023")
    assert observed["cwd"] == ROOT
    assert observed["environment"]["DB_HOST"] == "127.0.0.1"
    assert observed["environment"]["DB_PORT"] == "5544"
    assert observed["environment"]["DB_USER"] == "eval-user"
    assert observed["environment"]["DB_NAME"] == "vkt_v07_el_eval_preflight_20260717_01"


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
    assert collection.collection_name == "vkt_v07_el_cal_8899ccb64f9c"
    assert collection.collection_name_sha256 == (
        "3734920c8af5a4c5557e147c53fb9d6ce681ab7f35b19ff38ce5ebb70c1ee006"
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


def test_no_policy_post_freeze_or_release_artifacts_exist_before_human_approval():
    base = ROOT / "eval/entity_linking"
    assert not (base / "link_policy_v1.json").exists()
    assert not (base / "release_evidence_v1.json").exists()
    results = base / "results"
    assert not results.exists() or not tuple(results.glob("*.json"))


def test_no_v07_route_or_migration_and_protected_runtime_remains_unchanged():
    from app.main import app

    assert not any("/v07/" in path for path in app.openapi()["paths"])
    heads = list((ROOT / "alembic/versions").glob("0024*.py"))
    assert not heads
