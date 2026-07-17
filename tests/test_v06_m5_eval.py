from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.graph_canonical import canonical_graph_json_v1
from app.services.graph_retrieval_eval import (
    GraphRetrievalCaseScore,
    GraphRetrievalEvalCase,
    GraphRetrievalEvalGold,
    GraphRetrievalExplainStats,
    GraphRetrievalLatencyStats,
    assert_sanitized_graph_retrieval_artifact,
    build_graph_retrieval_metric_report,
    canonical_graph_retrieval_response,
    canonical_graph_retrieval_response_hash,
    load_graph_retrieval_eval_dataset,
    nearest_rank_latency,
    rate,
)
from app.services.graph_retrieval_eval_runtime import (
    GRAPH_RETRIEVAL_EVAL_UUID_NAMESPACE,
    GraphRetrievalPerformanceResult,
    SeededGraphRetrievalPerformance,
    _performance_requests,
    graph_retrieval_calibration_thresholds,
    graph_retrieval_eval_uuid,
    validate_graph_retrieval_eval_database_id,
)
from app.services.graph_retrieval_observability import (
    CanonicalLogGraphRetrievalObservationSink,
    GraphRetrievalObservation,
    assert_sanitized_graph_retrieval_observation,
    emit_graph_retrieval_observation,
    graph_retrieval_metric_samples,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/graph_retrieval/manifests/release_v1.json"


def test_m5_release_dataset_is_strict_complete_and_canonical():
    loaded = load_graph_retrieval_eval_dataset(
        repository_root=ROOT,
        manifest_path=MANIFEST,
    )

    assert loaded.gold.schema_version == "graph-retrieval-gold-v1"
    assert loaded.manifest.schema_version == "graph-retrieval-eval-manifest-v1"
    assert len(loaded.gold.entities) >= 40
    assert len(loaded.gold.relations) >= 40
    assert len(loaded.gold.evidence) >= 20
    assert len(loaded.cases) >= 48
    assert tuple(row.case_id for row in loaded.cases) == tuple(
        sorted(row.case_id for row in loaded.cases)
    )
    assert len(loaded.dataset_manifest_sha256) == 64
    assert len(loaded.dataset_content_sha256) == 64
    assert len(loaded.evaluation_config_sha256) == 64
    assert loaded.manifest.evaluation_config_sha256 == loaded.evaluation_config_sha256
    assert all(
        loaded.category_counts[name] >= minimum
        for name, minimum in loaded.manifest.category_minimums.items()
    )


def test_m5_dataset_models_reject_unknown_fields_and_bad_references():
    loaded = load_graph_retrieval_eval_dataset(
        repository_root=ROOT,
        manifest_path=MANIFEST,
    )
    gold = loaded.gold.model_dump(mode="json")
    gold["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        GraphRetrievalEvalGold.model_validate(gold)

    case = loaded.cases[0].model_dump(mode="json")
    case["request"]["seeds"] = [{"entity_key": "missing-entity"}]
    parsed = GraphRetrievalEvalCase.model_validate(case)
    with pytest.raises(ValueError, match="unknown Entity"):
        loaded.validate_case_references(parsed)


def test_m5_semantic_hashes_ignore_mapping_order_but_files_are_canonical(tmp_path):
    left = {"b": 2, "a": ["中文", True]}
    right = {"a": ["中文", True], "b": 2}
    assert canonical_graph_json_v1(left) == canonical_graph_json_v1(right)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    noncanonical = tmp_path / "manifest.json"
    noncanonical.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical JSON bytes"):
        load_graph_retrieval_eval_dataset(
            repository_root=ROOT,
            manifest_path=noncanonical,
        )


def test_m5_canonical_response_maps_all_runtime_ids_and_normalizes_activation_time():
    publication_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    entity_type_id = uuid.uuid4()
    body = {
        "contract_version": "v1",
        "publication": {
            "id": str(publication_id),
            "ontology_version_id": str(ontology_id),
            "manifest_version": "v1",
            "manifest_hash": "a" * 64,
            "activated_at": "2026-07-16T00:00:00Z",
        },
        "seed_matches": [{"input_index": 0, "entity_id": str(entity_id)}],
        "nodes": [
            {
                "id": str(entity_id),
                "item_hash": "b" * 64,
                "entity_type": {
                    "id": str(entity_type_id),
                    "key": "person",
                    "label": "Person",
                },
                "canonical_name": "Synthetic Alice",
                "normalized_name": "synthetic alice",
                "source_type": "manual",
                "confidence": None,
                "depth": 0,
                "evidence": [],
            }
        ],
        "relations": [],
        "counts": {"seeds": 1, "nodes": 1, "relations": 0, "evidence_locators": 0},
        "truncated": {"nodes": False, "relations": False, "evidence": False},
    }
    id_map = {
        str(publication_id): "publication-primary",
        str(ontology_id): "ontology-primary",
        str(entity_id): "entity-alice",
        str(entity_type_id): "entity-type-person",
    }

    canonical = canonical_graph_retrieval_response(
        status_code=200,
        body=body,
        logical_id_by_uuid=id_map,
    )
    assert canonical["body"]["publication"]["id"] == "publication-primary"
    assert canonical["body"]["publication"]["activated_at"] == "dataset-activation-v1"
    assert canonical_graph_retrieval_response_hash(
        status_code=200,
        body=body,
        logical_id_by_uuid=id_map,
    ) == hashlib.sha256(canonical_graph_json_v1(canonical).encode()).hexdigest()

    body["nodes"][0]["id"] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="unknown runtime UUID"):
        canonical_graph_retrieval_response(
            status_code=200,
            body=body,
            logical_id_by_uuid=id_map,
        )


def test_m5_metric_denominators_are_exact_and_zero_never_passes():
    report = build_graph_retrieval_metric_report(
        (
            GraphRetrievalCaseScore(
                seed_correct=2,
                seed_total=2,
                expected_nodes=3,
                returned_expected_nodes=3,
                expected_relations=2,
                returned_expected_relations=2,
                returned_relations=2,
                expected_evidence_pairs=2,
                returned_expected_evidence_pairs=2,
                deterministic_equal=2,
                deterministic_total=2,
                scope_safe=1,
                scope_total=1,
                privacy_clean=36,
                privacy_total=36,
                property_leak_count=0,
                publication_membership_failures=0,
            ),
        )
    )
    assert report.seed_exact_accuracy.value == 1.0
    assert report.node_recall.value == 1.0
    assert report.relation_recall.value == 1.0
    assert report.relation_precision.value == 1.0
    assert report.evidence_coverage.value == 1.0
    assert report.determinism.value == 1.0
    assert report.scope_safety.value == 1.0
    assert report.property_privacy.value == 1.0
    assert report.passes_hard_gates()
    assert rate(0, 0).value is None
    assert not build_graph_retrieval_metric_report(()).passes_hard_gates()


def test_m5_nearest_rank_latency_is_integer_and_non_interpolated():
    stats = nearest_rank_latency([index * 1000 for index in range(1, 31)])
    assert stats.sample_count == 30
    assert stats.p50_us == 15
    assert stats.p95_us == 29
    assert stats.max_us == 30


@pytest.mark.parametrize(
    "payload",
    [
        {"properties": {"nested": "privacy-canary"}},
        {"canonical_name": "Synthetic Alice"},
        {"safe": "Bearer not-safe"},
        {"safe": "https://internal.example"},
        {"safe": str(uuid.uuid4())},
        {"safe": float("nan")},
    ],
)
def test_m5_artifact_privacy_scanner_rejects_private_or_unbounded_values(payload):
    with pytest.raises(ValueError):
        assert_sanitized_graph_retrieval_artifact(payload)


def test_m5_artifact_privacy_scanner_accepts_hashes_counts_and_policy_flag():
    assert_sanitized_graph_retrieval_artifact(
        {
            "run_id": "calibration-v1-01",
            "canonical_response_set_sha256": "a" * 64,
            "case_counts": {"passed": 48, "failed": 0},
            "properties_exposed": False,
            "property_privacy": {
                "numerator": 36,
                "denominator": 36,
                "value": 1.0,
            },
        }
    )


def test_m5_all_committed_json_is_canonical_lf():
    loaded = load_graph_retrieval_eval_dataset(
        repository_root=ROOT,
        manifest_path=MANIFEST,
    )
    for path in (loaded.gold_path, loaded.case_path, MANIFEST):
        payload = path.read_bytes()
        assert payload.endswith(b"\n")
        assert b"\r\n" not in payload


def test_m5_runtime_uuid_namespace_and_database_scope_are_frozen():
    assert str(GRAPH_RETRIEVAL_EVAL_UUID_NAMESPACE) == "cb2f1c85-28c2-5797-b4fe-350d6549dd33"
    assert str(graph_retrieval_eval_uuid("entity", "ent-010")) == (
        "152ce80b-5179-584a-8697-9e76e00b7862"
    )
    assert validate_graph_retrieval_eval_database_id("vkt_v06_m5_eval_release_01")
    with pytest.raises(ValueError):
        validate_graph_retrieval_eval_database_id("production")


def test_m5_runtime_freezes_six_performance_scenarios_and_candidate_formula():
    value = uuid.UUID(int=1)
    seeded = SeededGraphRetrievalPerformance(
        library_id=value,
        library_slug="performance",
        ontology_id=value,
        publication_id=value,
        entity_type_id=value,
        relation_type_id=value,
        evidence_id=value,
        seed_entity_id=value,
        high_degree_seed_id=value,
        first_relation_id=value,
        first_relation_item_hash="a" * 64,
        logical_by_uuid={},
    )
    assert tuple(sorted(_performance_requests(seeded))) == (
        "high-degree-node-truncation",
        "high-degree-relation-truncation",
        "one-hop-evidence-off",
        "one-hop-evidence-on",
        "two-hop-evidence-off",
        "two-hop-evidence-on",
    )
    performance = GraphRetrievalPerformanceResult(
        latency={
            "one-hop-evidence-on": GraphRetrievalLatencyStats(
                sample_count=30,
                p50_us=50_000,
                p95_us=80_000,
                max_us=90_000,
            )
        },
        explain={
            "snapshot-items": GraphRetrievalExplainStats(
                scan_rows_observed=10_000,
                plan_rows_observed=20_000,
                shared_hit_blocks=100,
                shared_read_blocks=0,
                temp_read_blocks=0,
                temp_written_blocks=0,
                plan_shape_sha256="b" * 64,
            )
        },
        environment_fingerprint_sha256="c" * 64,
    )
    thresholds = graph_retrieval_calibration_thresholds(performance)
    assert thresholds.max_p95_us == {"one-hop-evidence-on": 100_000}
    assert thresholds.max_scan_rows == {"snapshot-items": 11_000}


def test_m5_observation_and_metric_fields_are_allowlisted_and_low_cardinality(caplog):
    observation = GraphRetrievalObservation(
        request_id="a" * 32,
        result_code="success",
        status_code=200,
        library_id=uuid.UUID(int=1),
        ontology_version_id=uuid.UUID(int=2),
        publication_id=uuid.UUID(int=3),
        max_hops=2,
        include_evidence_locators=True,
        node_count=3,
        relation_count=2,
        evidence_locator_count=1,
        truncated_relations=True,
        duration_us=12_345,
    )
    samples = graph_retrieval_metric_samples(observation)
    assert {sample.name for sample in samples} == {
        "graph_retrieval_requests_total",
        "graph_retrieval_duration_seconds",
        "graph_retrieval_nodes_returned",
        "graph_retrieval_relations_returned",
        "graph_retrieval_truncated_total",
    }
    assert all(
        not ({"library_id", "ontology_version_id", "publication_id"} & set(sample.labels))
        for sample in samples
    )
    assert_sanitized_graph_retrieval_observation(
        observation,
        samples,
        forbidden_values=("private-seed", "private-properties"),
    )

    caplog.set_level("INFO", logger="m5-observation-test")
    sink = CanonicalLogGraphRetrievalObservationSink(
        logging.getLogger("m5-observation-test")
    )
    sink.emit(observation, samples)
    assert "private-seed" not in caplog.text
    assert "graph_retrieval.completed" in caplog.text


def test_m5_observation_sink_failure_is_suppressed_without_fallback_state():
    observation = GraphRetrievalObservation(
        request_id="b" * 32,
        result_code="graph_retrieval_internal_error",
        status_code=500,
        duration_us=1,
    )

    class FailingSink:
        def emit(self, _observation, _samples):
            raise RuntimeError("private-exception-marker")

    emit_graph_retrieval_observation(observation, sink=FailingSink())
