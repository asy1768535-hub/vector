from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.services.graph_retrieval as graph_retrieval_module
from app.services.graph_retrieval import HealthyGraphSnapshot

from eval.entity_linking.contracts import (
    GATE_ID_ORDER,
    CalibrationArtifact,
    FrozenPolicy,
    GateDecision,
    GridResult,
    FeasibilityManifest,
    EvaluationCase,
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
    load_canonical_jsonl,
)
from eval.entity_linking.reference_scorer import (
    EntityCandidate,
    _prepare_candidate_normalized_value,
    character_bigram_dice_micros,
    prepare_candidates,
    resolve_mention,
    score_candidate,
    score_normalized_pair,
)
from eval.entity_linking.runtime import (
    CALIBRATION_RUN_ID,
    FINAL_RUNTIME_PROTECTED_PATHS,
    FROZEN_EVALUATOR_COMMIT,
    G2_APPROVAL_COMMIT,
    G2_SPECIFICATION_TREE_SHA256,
    EntityLinkingEvalError,
    _embedding_determinism_preflight,
    _embedding_vector_float32_sha256,
    _embedding_vector_sha256,
    _publication_projection_cache,
    _performance_cases,
    _verify_frozen_dependency_records,
    _validate_historical_family_disjoint,
    _validate_references,
    assert_private_data_absent,
    build_dependency_closure,
    build_gate_decisions,
    discover_external_distribution_records,
    build_grid_results,
    external_distribution_records,
    load_dataset,
    load_candidate_projection,
    normalize_service_origin,
    paired_stratified_bootstrap,
    qdrant_collection_identity,
    repository_module_is_local,
    select_calibration_threshold,
    upgrade_database,
    verify_g2_approval,
    require_replacement_calibration_reference,
    run_calibration,
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
        database_id="vkt_v07_el_eval_calibration_v11_20260721_01",
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
    embedding = value["environment"]["embedding"]
    embedding["vector_encoding_version"] = "ternary-deadzone-0.005-v1"
    embedding["embedding_fingerprint_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in embedding.items()
            if key != "embedding_fingerprint_sha256"
        }
    )
    value["embedding_fingerprint_sha256"] = embedding["embedding_fingerprint_sha256"]
    environment = value["environment"]
    environment["environment_fingerprint_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in environment.items()
            if key != "environment_fingerprint_sha256"
        }
    )
    value["environment_fingerprint_sha256"] = environment["environment_fingerprint_sha256"]
    return value


def _release_value() -> dict[str, object]:
    refs = [
        {
            "ordinal": ordinal,
            "run_id": f"v07-el-post-freeze-v12-20260721-0{ordinal}",
            "artifact_ref": _artifact_ref_value(
                f"eval/entity_linking/results/v07-el-post-freeze-v12-20260721-0{ordinal}.json"
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
        "dataset_manifest_ref": _artifact_ref_value("eval/entity_linking/manifests/feasibility_v4.json"),
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
            "eval/entity_linking/results/v07-el-calibration-v12-20260721-01.json"
        ),
        "policy_ref": _artifact_ref_value("eval/entity_linking/link_policy_v11.json"),
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
    assert dataset.gold.dataset_id == "feasibility-v5"
    assert dataset.manifest.dataset_id == "feasibility-v5"
    assert dataset.manifest_path == ROOT / "eval/entity_linking/manifests/feasibility_v5.json"
    assert dataset.manifest.g2_approval_commit == G2_APPROVAL_COMMIT
    assert dataset.manifest.accepted_dependency_closure_path_count == 63
    assert dataset.manifest.control_config.hybrid_candidate_k == 50
    assert dataset.manifest.control_config.vector_search_exact is True
    assert dataset.manifest.control_config.vector_tie_completion_version == (
        "score-desc-chunk-id-asc-probe-51-102-201-tail-complete-fail-closed-v2"
    )
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
    assert all(case.case_id.startswith("case-v5-") for case in dataset.cases)
    assert all(
        all(mention.is_exact_control for mention in case.mentions)
        for case in dataset.cases
        if case.cohort == "utility"
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


def test_v5_decoy_and_historical_family_mutations_are_rejected():
    dataset = load_dataset(ROOT)
    utility_case = next(case for case in dataset.cases if case.cohort == "utility")
    decoy_chunk = next(
        chunk for chunk in dataset.gold.chunks if chunk.chunk_key == utility_case.decoy_chunk_keys[0]
    )
    decoy_document = next(
        document
        for document in dataset.gold.documents
        if document.document_key == decoy_chunk.document_key
    )
    mutated_documents = tuple(
        document.model_copy(update={"title": "query removed"})
        if document.document_key == decoy_document.document_key
        else document
        for document in dataset.gold.documents
    )
    with pytest.raises(ValueError, match="retrieval-competitive decoy invalid"):
        _validate_references(
            dataset.gold.model_copy(update={"documents": mutated_documents}),
            dataset.cases,
        )

    historical_cases = tuple(
        case
        for case in load_canonical_jsonl(
            ROOT,
            ROOT / "eval/entity_linking/cases_v2.jsonl",
            EvaluationCase,
        )
        if isinstance(case, EvaluationCase)
    )
    mutated_current = (
        dataset.cases[0].model_copy(
            update={"entity_family_keys": historical_cases[0].entity_family_keys}
        ),
        *dataset.cases[1:],
    )
    with pytest.raises(EntityLinkingEvalError, match="family_split_overlap"):
        _validate_historical_family_disjoint(mutated_current, historical_cases)


def test_linked_performance_fixture_resolves_full_frozen_mix():
    dataset = load_dataset(ROOT)
    threshold, _reason = select_calibration_threshold(build_grid_results(dataset))
    assert threshold is not None
    _mixed, linked = _performance_cases(dataset, threshold)
    entities = {row.entity_key: row for row in dataset.gold.entities}
    publication = next(
        row for row in dataset.gold.publications if row.publication_key == "publication-primary"
    )
    candidates = tuple(
        EntityCandidate(
            entity_id=str(uuid.uuid5(uuid.UUID(dataset.gold.uuid_namespace), f"entity:{key}")),
            entity_key=key,
            entity_type_key=entities[key].entity_type_key,
            canonical_name=entities[key].canonical_name,
            normalized_name=entities[key].normalized_name,
        )
        for key in publication.entity_keys
    )
    observed = []
    for mention in linked.mentions:
        decision = resolve_mention(
            mention.text,
            candidates,
            entity_type_key=mention.entity_type_key,
            min_score_micros=threshold.min_score_micros,
            min_margin_micros=threshold.min_margin_micros,
        )
        assert decision.status == "linked"
        assert decision.selected is not None
        assert decision.selected.candidate.entity_key == mention.gold_entity_key
        candidate = next(row for row in candidates if row.entity_key == mention.gold_entity_key)
        features = score_candidate(mention.text, candidate).features
        observed.append(
            "exact"
            if mention.is_exact_control
            else "boundary"
            if features.boundary_omission_micros
            else "abbreviation"
        )
    assert observed.count("exact") == 2
    assert observed.count("boundary") == 4
    assert observed.count("abbreviation") == 4


def test_canonical_loader_rejects_pretty_json_duplicate_keys_crlf_and_extra(tmp_path):
    source = ROOT / "eval/entity_linking/gold_v5.json"
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


def test_prepared_candidate_cache_is_bounded_and_identity_free():
    first = EntityCandidate(
        entity_id="00000000-0000-0000-0000-000000000001",
        entity_key="candidate-first",
        entity_type_key="organization",
        canonical_name="Shared Canonical",
        normalized_name="shared canonical",
    )
    second = EntityCandidate(
        entity_id="00000000-0000-0000-0000-000000000002",
        entity_key="candidate-second",
        entity_type_key="organization",
        canonical_name="Shared Canonical",
        normalized_name="shared canonical",
    )
    prepared = prepare_candidates((first, second))
    assert prepared[0].lexical is prepared[1].lexical
    assert not hasattr(prepared[0].lexical, "entity_id")
    assert _prepare_candidate_normalized_value.cache_info().maxsize == 10_000


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
        min_margin_micros=120000,
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


def test_frozen_dependency_closure_accepts_platform_newlines_and_rejects_content_drift(
    monkeypatch,
):
    manifest = load_dataset(ROOT).manifest
    records = tuple(
        record.model_dump(mode="json") for record in manifest.accepted_dependency_closure_records
    )
    target = ROOT / "app/db.py"
    original_read_bytes = Path.read_bytes
    target_body = original_read_bytes(target).replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    def lf_checkout(path):
        if path == target:
            return target_body
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", lf_checkout)
    _verify_frozen_dependency_records(ROOT, FROZEN_EVALUATOR_COMMIT, records)

    def crlf_checkout(path):
        if path == target:
            return target_body.replace(b"\n", b"\r\n")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", crlf_checkout)
    _verify_frozen_dependency_records(ROOT, FROZEN_EVALUATOR_COMMIT, records)

    def tampered_checkout(path):
        if path == target:
            return target_body + b"# content drift\n"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tampered_checkout)
    with pytest.raises(EntityLinkingEvalError, match="dependency_closure_unresolved"):
        _verify_frozen_dependency_records(ROOT, FROZEN_EVALUATOR_COMMIT, records)


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
    assert collection.collection_name == "vkt_v07_el_cal_c3927fe9d2ca"
    assert collection.collection_name_sha256 == (
        "4ad02b67f6b5969a4f1571555ed6a70bf4ab11ba6992b7514e84935aefa68ea3"
    )
    assert normalize_service_origin("HTTP://Example.COM", qdrant=True) == "http://example.com:80"
    assert (
        normalize_service_origin("http://[2001:0DB8:0:0::1]:6333/", qdrant=True)
        == "http://[2001:db8::1]:6333"
    )
    with pytest.raises(EntityLinkingEvalError):
        normalize_service_origin("http://user@example.com/", qdrant=True)


def test_embedding_ternary_identity_has_stable_deadzone_order_and_dimension():
    baseline = [-0.006, -0.005, 0.005, 0.006] + [0.0] * 1020
    jittered = [-0.0061, -0.0049, 0.0049, 0.0061] + [0.0001] * 1020
    crossed_deadzone = list(baseline)
    crossed_deadzone[1] = -0.0051
    assert _embedding_vector_sha256(baseline) == _embedding_vector_sha256(jittered)
    assert _embedding_vector_sha256(baseline) == (
        "207f33e324840669ec8b52692a5e28534dd2ff05041a8eba350e9d7e712b7da9"
    )
    assert _embedding_vector_sha256(baseline) != _embedding_vector_sha256(crossed_deadzone)
    with pytest.raises(EntityLinkingEvalError, match="embedding_identity_mismatch"):
        _embedding_vector_sha256(baseline[:-1])
    with pytest.raises(EntityLinkingEvalError, match="embedding_identity_mismatch"):
        _embedding_vector_sha256([float("nan")] + baseline[1:])


def test_embedding_determinism_uses_all_float32_bits_and_exact_8_by_8_probes(monkeypatch):
    import eval.entity_linking.runtime as runtime

    vector = [0.0] * 1024
    response_bytes = json.dumps({"data": [{"embedding": vector}]}).encode("utf-8")
    calls: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, _url, *, json, headers):
            calls.append(json["input"][0])
            return SimpleNamespace(status_code=200, content=response_bytes)

    full_hash = _embedding_vector_float32_sha256(vector)
    expected_result = hashlib.sha256((full_hash * 8).encode("ascii")).hexdigest()
    monkeypatch.setattr(runtime.httpx, "AsyncClient", Client)
    monkeypatch.setattr(runtime, "EMBEDDING_DETERMINISM_RESULT_SHA256", expected_result)
    result = asyncio.run(_embedding_determinism_preflight("http://embedding", "bge-m3", ""))
    assert calls == [probe for probe in runtime.EMBEDDING_DETERMINISM_PROBES for _ in range(8)]
    assert result == {
        "probe_count": 8,
        "repetitions_per_probe": 8,
        "stable_call_count": 64,
        "probe_set_sha256": expected_result,
    }
    assert _embedding_vector_float32_sha256(vector) != _embedding_vector_float32_sha256(
        [1e-9, *vector[1:]]
    )
    assert _embedding_vector_sha256(vector) == _embedding_vector_sha256([1e-9, *vector[1:]])


def test_embedding_determinism_fails_on_input_or_repeated_vector_drift(monkeypatch):
    import eval.entity_linking.runtime as runtime

    original_probes = runtime.EMBEDDING_DETERMINISM_PROBES
    monkeypatch.setattr(runtime, "EMBEDDING_DETERMINISM_PROBES", (*original_probes[:-1], "changed"))
    with pytest.raises(EntityLinkingEvalError, match="embedding_determinism_probe_mismatch"):
        asyncio.run(_embedding_determinism_preflight("http://embedding", "bge-m3", ""))
    monkeypatch.setattr(runtime, "EMBEDDING_DETERMINISM_PROBES", original_probes)

    call_count = 0

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, _url, *, json, headers):
            nonlocal call_count
            call_count += 1
            vector = [0.0] * 1024
            if call_count == 2:
                vector[0] = 1e-9
            body = json_module.dumps({"data": [{"embedding": vector}]}).encode("utf-8")
            return SimpleNamespace(status_code=200, content=body)

    json_module = json
    monkeypatch.setattr(runtime.httpx, "AsyncClient", Client)
    with pytest.raises(EntityLinkingEvalError, match="embedding_nondeterministic"):
        asyncio.run(_embedding_determinism_preflight("http://embedding", "bge-m3", ""))
    assert call_count == 64


def test_calibration_embedding_failure_precedes_resource_creation(monkeypatch, tmp_path):
    import eval.entity_linking.runtime as runtime

    resource_calls: list[str] = []

    async def fail_preflight():
        raise EntityLinkingEvalError("embedding_nondeterministic")

    async def create_database(*args, **kwargs):
        resource_calls.append("database")
        return "unused"

    monkeypatch.setattr(runtime, "load_dataset", lambda _root: object())
    monkeypatch.setattr(runtime, "implementation_identity", lambda _root: ("a" * 40, "b" * 64))
    monkeypatch.setattr(runtime, "preflight_live_dependencies", fail_preflight)
    monkeypatch.setattr(runtime, "create_database", create_database)
    monkeypatch.setenv("VECTOR_KB_PG_TEST_DSN", "postgresql://local/test")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://embedding")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    with pytest.raises(EntityLinkingEvalError, match="embedding_nondeterministic"):
        asyncio.run(
            run_calibration(
                tmp_path,
                run_id=CALIBRATION_RUN_ID,
                database_id="vkt_v07_el_eval_calibration_v12_20260721_01",
                allow_create_drop_eval_db=True,
                allow_create_drop_qdrant_collection=True,
            )
        )
    assert resource_calls == []


def test_publication_projection_cache_keys_evicts_and_preserves_fences(monkeypatch):
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    current = {
        "snapshot": HealthyGraphSnapshot(
            publication_id=publication_id,
            library_id=library_id,
            ontology_version_id=ontology_id,
            manifest_version="graph-publication-manifest-v1",
            manifest_hash="a" * 64,
            activated_at=datetime.now(timezone.utc),
            entity_count=1,
            relation_count=0,
        ),
        "fence_error": False,
    }
    calls = {"snapshots": 0, "fences": 0}

    async def load_snapshot(*args, **kwargs):
        calls["snapshots"] += 1
        return current["snapshot"]

    async def end_fence(*args, **kwargs):
        calls["fences"] += 1
        if current["fence_error"]:
            raise RuntimeError("publication changed")

    class Result:
        def all(self):
            return [
                SimpleNamespace(
                    id=entity_id,
                    canonical_name="Cache Candidate",
                    normalized_name="cache candidate",
                    entity_type_key="concept",
                    item_hash="b" * 64,
                )
            ]

    class Database:
        def __init__(self):
            self.execute_count = 0

        async def execute(self, statement):
            self.execute_count += 1
            return Result()

    monkeypatch.setattr(graph_retrieval_module, "load_healthy_graph_snapshot", load_snapshot)
    monkeypatch.setattr(graph_retrieval_module, "assert_graph_snapshot_still_current", end_fence)
    db = Database()
    logical = {str(entity_id): "entity-cache-candidate"}

    async def exercise():
        first = await load_candidate_projection(
            db, SimpleNamespace(id=library_id), ontology_id, publication_id, logical
        )
        second = await load_candidate_projection(
            db, SimpleNamespace(id=library_id), ontology_id, publication_id, logical
        )
        assert first[1] is second[1]
        assert db.execute_count == 1
        assert calls == {"snapshots": 2, "fences": 2}

        current["snapshot"] = replace(current["snapshot"], manifest_hash="c" * 64)
        await load_candidate_projection(
            db, SimpleNamespace(id=library_id), ontology_id, publication_id, logical
        )
        assert db.execute_count == 2
        assert len(_publication_projection_cache) == 1
        retained_snapshot = current["snapshot"]

        added_snapshots = []
        for index in range(7):
            snapshot = HealthyGraphSnapshot(
                publication_id=uuid.uuid4(),
                library_id=uuid.uuid4(),
                ontology_version_id=uuid.uuid4(),
                manifest_version="graph-publication-manifest-v1",
                manifest_hash=f"{index + 1:064x}",
                activated_at=datetime.now(timezone.utc),
                entity_count=1,
                relation_count=0,
            )
            added_snapshots.append(snapshot)
            current["snapshot"] = snapshot
            await load_candidate_projection(
                db,
                SimpleNamespace(id=snapshot.library_id),
                snapshot.ontology_version_id,
                snapshot.publication_id,
                logical,
            )
        assert len(_publication_projection_cache) == 8

        current["snapshot"] = retained_snapshot
        await load_candidate_projection(
            db,
            SimpleNamespace(id=retained_snapshot.library_id),
            retained_snapshot.ontology_version_id,
            retained_snapshot.publication_id,
            logical,
        )
        assert db.execute_count == 9

        newest_snapshot = replace(
            retained_snapshot,
            publication_id=uuid.uuid4(),
            library_id=uuid.uuid4(),
            ontology_version_id=uuid.uuid4(),
            manifest_hash="f" * 64,
        )
        current["snapshot"] = newest_snapshot
        await load_candidate_projection(
            db,
            SimpleNamespace(id=newest_snapshot.library_id),
            newest_snapshot.ontology_version_id,
            newest_snapshot.publication_id,
            logical,
        )
        assert any(key[2] == retained_snapshot.publication_id for key in _publication_projection_cache)
        assert not any(
            key[2] == added_snapshots[0].publication_id for key in _publication_projection_cache
        )

        current["fence_error"] = True
        with pytest.raises(RuntimeError, match="publication changed"):
            await load_candidate_projection(
                db,
                SimpleNamespace(id=current["snapshot"].library_id),
                current["snapshot"].ontology_version_id,
                current["snapshot"].publication_id,
                logical,
            )
        assert not any(key[2] == current["snapshot"].publication_id for key in _publication_projection_cache)

    _publication_projection_cache.clear()
    try:
        asyncio.run(exercise())
    finally:
        _publication_projection_cache.clear()


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
        "eval/entity_linking/results/v07-el-calibration-v12-20260721-01.json"
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
        "dataset_manifest_ref": _artifact_ref_value("eval/entity_linking/manifests/feasibility_v5.json"),
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
        "run_id": "v07-el-post-freeze-v12-20260721-01",
        "artifact_ref": _artifact_ref_value(
            "eval/entity_linking/results/v07-el-post-freeze-v12-20260721-01.json"
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
        run_id="v07-el-post-freeze-v12-20260721-01",
        database_id="vkt_v07_el_eval_post_freeze_v12_20260721_01",
        grid_results=None,
        candidate_thresholds=None,
        selection_reason=None,
        policy_ref=_artifact_ref_value("eval/entity_linking/link_policy_v11.json"),
        qdrant_collection=qdrant_collection_identity("v07-el-post-freeze-v12-20260721-01", "pf1").model_dump(
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
    policy_path = ROOT / "eval/entity_linking/link_policy_v11.json"
    assert historical_policy_path.is_file()
    policy_bytes = policy_path.read_bytes()
    with pytest.raises(EntityLinkingEvalError, match="invalidated_calibration_artifact"):
        require_replacement_calibration_reference(
            ROOT,
            ROOT / "eval/entity_linking/results/v07-el-calibration-v1-20260717-01.json",
            expected_canonical_sha256=("96757dcd901439828b61605c473a34c2c838ed6755db6f129d030561b156894d"),
            expected_file_sha256=("c35210075e179ebce197275041e0ff69403297185ec2ebcc16a663d7611e9225"),
        )
    assert policy_path.read_bytes() == policy_bytes


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


def test_historical_chains_and_accepted_v12_outputs_are_preserved():
    base = ROOT / "eval/entity_linking"
    assert (base / "link_policy_v1.json").is_file()
    assert (base / "link_policy_v8.json").is_file()
    assert (base / "release_evidence_v8.json").is_file()
    accepted_policy = base / "link_policy_v11.json"
    accepted_evidence = base / "release_evidence_v11.json"
    assert hashlib.sha256(accepted_policy.read_bytes()).hexdigest() == (
        "b80026b8b5e3f6d691f3bcb7481c94004a762b3b714a383c01d296970003bcec"
    )
    assert canonical_sha256(json.loads(accepted_policy.read_text(encoding="utf-8"))) == (
        "01cf3495e12f8a20a163eb862e4c4c368c9a50038073eab10ea63d83d7df60c8"
    )
    assert hashlib.sha256(accepted_evidence.read_bytes()).hexdigest() == (
        "adf87992e2868ed5895083703863007ebb9a642ce58dab664be4715ae2d5a624"
    )
    evidence_value = json.loads(accepted_evidence.read_text(encoding="utf-8"))
    assert canonical_sha256(evidence_value) == (
        "9e546b9dc17f4fde296c28416ad601e52c06d2f042bb1ff6fb64174902609607"
    )
    assert evidence_value["final_hard_and_decision"] == "GO_ELIGIBLE"
    results = base / "results"
    historical = results / "v07-el-calibration-v3-20260720-01.json"
    historical_value = json.loads(historical.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_value) == (
        "e5e658a9bfd11d2fc36e64197dedbd272b85d3c9d4f1693047dbc80c43316874"
    )
    assert hashlib.sha256(historical.read_bytes()).hexdigest() == (
        "e912cc72b818fc21f374fda11f4d859bf931accd5fc71759a7f6d46d45591411"
    )
    historical_v4 = results / "v07-el-calibration-v4-20260720-01.json"
    historical_v4_value = json.loads(historical_v4.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_v4_value) == (
        "4eea72699aee9cc4f09b4a1ca9a279240be0d0b071ea424c4b012ba50b36bad7"
    )
    assert hashlib.sha256(historical_v4.read_bytes()).hexdigest() == (
        "f1ba27f37fb6a019939373bf045897f22c0e01f0be9b2f8aa5578a11abe196f0"
    )
    historical_v5 = results / "v07-el-calibration-v5-20260720-01.json"
    historical_v5_value = json.loads(historical_v5.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_v5_value) == (
        "6b18b5edc8fa5566ef770546332f00a04f25a0c0460d1fc51a4efd34323fe511"
    )
    assert hashlib.sha256(historical_v5.read_bytes()).hexdigest() == (
        "24d5d9bd4d2cbcca3cbc1d7dce77d3b6820b5c9679038bb4ed72e52c9ef53498"
    )
    historical_v6 = results / "v07-el-calibration-v6-20260720-01.json"
    historical_v6_value = json.loads(historical_v6.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_v6_value) == (
        "c2e4a2d37c1caf34446769037b799488314a832521f103fdbe11485805b416ac"
    )
    assert hashlib.sha256(historical_v6.read_bytes()).hexdigest() == (
        "419052ec9c72a0f8bb4de3e58b5bb0b608063af07fd796aac4bd6a9bd035f5a3"
    )
    historical_v7 = results / "v07-el-calibration-v7-20260720-01.json"
    historical_v7_value = json.loads(historical_v7.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_v7_value) == (
        "2c46a322e6541b53d27da1f85092534812c5c121ebbf3cec8371d99f2f51321b"
    )
    assert hashlib.sha256(historical_v7.read_bytes()).hexdigest() == (
        "131af9b1cd60881d252d4c34d85ecb8eee15bcb0381c01cd57d06326b5b45663"
    )
    historical_policy = base / "link_policy_v6.json"
    historical_policy_value = json.loads(historical_policy.read_text(encoding="utf-8"))
    assert canonical_sha256(historical_policy_value) == (
        "87cca4ed0715cb89efc51199eeada8287d5adefbb774915ff39bf868f48b59cf"
    )
    assert hashlib.sha256(historical_policy.read_bytes()).hexdigest() == (
        "02b327271d2935ba97f30dfafe8051fcc639e7eeaadfe9e83498031e390be558"
    )
    historical_ordinal = results / "v07-el-post-freeze-v7-20260720-01.json"
    historical_ordinal_value = json.loads(historical_ordinal.read_text(encoding="utf-8"))
    assert historical_ordinal_value["status"] == "no_go"
    assert canonical_sha256(historical_ordinal_value) == (
        "da64236da6b2b320710912f8f33f7a9a7082e378c5a5a0834704c85342a4fbfe"
    )
    assert hashlib.sha256(historical_ordinal.read_bytes()).hexdigest() == (
        "0f0becf67805b23f38728bda642ce7dbb3fb552c33ffa742f51ec9a695aa3d25"
    )
    immutable_v10_chain = {
        "results/v07-el-calibration-v11-20260721-01.json": (
            "e39f23c53382e9808775aaacf3f564e381c56b52db2a475229c0368379373c51",
            "fd0f5fe5db56ebceb07fd19d447be8542a68963d14a8d2a50fb5d8627775deec",
        ),
        "link_policy_v10.json": (
            "364c30461ad739a1cac57722f0f42b1f5dc435796969318807cd3e716af16575",
            "a698cf60be77e11f559757f3c8546d7fb97a8744275af263c7ce659544697ceb",
        ),
        "results/v07-el-post-freeze-v11-20260721-01.json": (
            "13dbc55ddedf8bf4168a1915731a287226de9ab80d753e064ed1d8da6f46350e",
            "726bdf55dbbb2cc410574f2224c10b043a2e9921c0d42a7d6707f3a6bc280cbe",
        ),
        "results/v07-el-post-freeze-v11-20260721-02.json": (
            "b8f5181e2f5a6c05bfb75ce3a8fdca92241adce204695d3d50ee956ed21d552d",
            "79b3d5c99fa7f2158f9b0529cad0ee7219205850d73ed92250fd241b673ddc71",
        ),
        "results/v07-el-post-freeze-v11-20260721-03.json": (
            "276ba5f95389236417371d0a8da7ccabbd03614107c21d16d009600c20f7cba9",
            "817fa16a05fcf561e3aa8cd8d7aaa28bf56383dc9e973cddddb13bf5c80e3af2",
        ),
        "release_evidence_v10.json": (
            "19e1f508c0156918ec856285d15356b6e2038ac852618fc724017806a16538d3",
            "2b05773802be50fca721f881c0f5f33144d2d635918be655c20b3c1571ce9374",
        ),
    }
    for relative_path, (canonical_hash, file_hash) in immutable_v10_chain.items():
        path = base / relative_path
        value = json.loads(path.read_text(encoding="utf-8"))
        assert canonical_sha256(value) == canonical_hash
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file_hash
    assert json.loads((base / "release_evidence_v10.json").read_text(encoding="utf-8"))[
        "final_hard_and_decision"
    ] == "NO_GO"
    accepted_post_freeze = {
        "v07-el-post-freeze-v12-20260721-01.json": "e3a181522964ed0b5253179b2cdc4bc3b73cb5017fcf895710df0ead22b604be",
        "v07-el-post-freeze-v12-20260721-02.json": "fc45096e920dcabba5ecb00712292cd45e0fee472807fb0e67446beb9a0bd995",
        "v07-el-post-freeze-v12-20260721-03.json": "a36f9df4acf6ecaa58a0cf37c6c1abfbc33020e9f60b9b47540bba16d79f7af1",
    }
    for name, expected_sha256 in accepted_post_freeze.items():
        assert hashlib.sha256((results / name).read_bytes()).hexdigest() == expected_sha256


def test_accepted_v07_route_default_off_and_no_new_migration():
    from app.config import Settings
    from app.main import app

    v07_paths = [path for path in app.openapi()["paths"] if "/v07/" in path]
    assert v07_paths == ["/libraries/{slug}/v07/entity-links/resolve"]
    config = Settings(_env_file=None)
    assert config.entity_linking_enabled is False
    assert config.entity_linking_policy_path == "eval/entity_linking/link_policy_v11.json"
    assert config.entity_linking_policy_sha256 == ""
    heads = list((ROOT / "alembic/versions").glob("0024*.py"))
    assert not heads


def test_final_runtime_tree_is_content_bound():
    expected = {
        ".env.example": "23b68320bf408aaec746549c30a3fc27d50a2088d265f4ac4b6e55f3d856260d",
        "app/api/v07_entity_linking.py": "2a1e2b5a4fd4b8c5eb537216315ccf842e61b75413ee9cb9d39bf0ced7b00496",
        "app/config.py": "48c3219216da9aaacf810878f77cda67984dd4c8b65c085894e6bc0e0fe25a48",
        "app/main.py": "2fe2338917c88ebc0effeb364d2ef1706b51d6d9950441c4e34cb0a26f256a03",
        "app/schemas/v07_entity_linking.py": "8d1caebbb9b6ba3668d75baf1d1e32af7d8d2f7e6752980345884d91b21e53e1",
        "app/services/entity_linking.py": "26c5a84083199fea839faee3fec69f8402c6d1879182553b76a79778227b42ac",
        "app/services/entity_linking_observability.py": (
            "9c91a2ac2d658ecfc8fff123b6d4905832b1ab53e0273c859fa59b175bd6d654"
        ),
        "app/services/entity_linking_scorer.py": (
            "108b17a91d2e8740cccb8e8ca8d5909427e0af9cd18ad15c60846ab9df2d5fe9"
        ),
    }
    assert set(expected) == set(FINAL_RUNTIME_PROTECTED_PATHS)
    records = []
    for path in sorted(FINAL_RUNTIME_PROTECTED_PATHS):
        body = (ROOT / path).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        observed = hashlib.sha256(body).hexdigest()
        assert observed == expected[path]
        records.append({"repository_relative_path": path, "canonical_lf_sha256": observed})
    assert canonical_sha256(records) == (
        "836aacc964a3d8dd62214f54925c1f15c6b8b3c1196aaf00305207e2fccb99ba"
    )
