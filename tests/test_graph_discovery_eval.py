from __future__ import annotations

from app.schemas.graph_extraction import GraphExtractionPayload
from scripts.graph_discovery_eval import (
    _aggregate_extraction_outputs,
    _RecordingProvider,
    _exchange_stage,
    _validate_export,
    score_predictions,
)

import pytest


def test_exchange_stage_does_not_misclassify_synthesis_prompt_wording():
    assert _exchange_stage(
        "discovery",
        [
            {
                "role": "system",
                "content": "Synthesize a Schema; semantic refinement is a later stage.",
            },
            {"role": "user", "content": '{"concept_inventory": []}'},
        ],
    ) == "schema_synthesis"


def _extraction_payload(*, name_suffix: str, context_ref: str) -> GraphExtractionPayload:
    return GraphExtractionPayload.model_validate(
        {
            "entities": [
                {
                    "local_id": "line",
                    "name": f"集电线路{name_suffix}L-01",
                    "entity_type_key": "collector_line",
                    "confidence": 0.9,
                    "evidence": [{"context_ref": context_ref, "quote": "line evidence"}],
                },
                {
                    "local_id": "array",
                    "name": f"组件阵列{name_suffix}C-03",
                    "entity_type_key": "pv_array",
                    "confidence": 0.9,
                    "evidence": [{"context_ref": context_ref, "quote": "array evidence"}],
                },
            ],
            "relations": [
                {
                    "source_local_id": "line",
                    "relation_type_key": "connects",
                    "target_local_id": "array",
                    "confidence": 0.9,
                    "evidence": [{"context_ref": context_ref, "quote": "connects evidence"}],
                }
            ],
        }
    )


def test_real_provider_recording_keeps_attempted_request_when_provider_fails():
    class FailingProvider:
        async def extract(self, _messages):
            raise TimeoutError("provider timeout must not be persisted")

    trace = []
    provider = _RecordingProvider(
        FailingProvider(),
        stage="concept_inventory",
        trace=trace,
    )

    with pytest.raises(TimeoutError):
        import asyncio

        asyncio.run(
            provider.extract(
                [
                    {"role": "system", "content": "inventory"},
                    {"role": "user", "content": '{"text":"secret source"}'},
                ]
            )
        )

    assert len(trace) == 1
    assert trace[0]["stage"] == "concept_inventory"
    assert len(trace[0]["request"]["payload_hash"]) == 64
    assert trace[0]["response"] == {
        "kind": "desensitized_real_response_error",
        "error_type": "TimeoutError",
    }
    assert "provider timeout" not in str(trace)
    assert "secret source" not in str(trace)


def test_extraction_aggregation_uses_production_name_identity_and_deduplicates_relations():
    result = _aggregate_extraction_outputs(
        [
            ("first", _extraction_payload(name_suffix=" ", context_ref="c0")),
            ("second", _extraction_payload(name_suffix="", context_ref="c1")),
        ],
        snapshot={"relation_types": [{"key": "connects", "direction": "undirected"}]},
    )

    assert len(result["entities"]) == 2
    assert len(result["relations"]) == 1
    assert len(result["relations"][0]["evidence"]) == 2
    assert "集电线路L-01" in result["entities"][0]["aliases"]
    assert "组件阵列C-03" in result["entities"][1]["aliases"]


def test_extraction_aggregation_derives_evidence_backed_identifier_aliases():
    payload = GraphExtractionPayload.model_validate(
        {
            "entities": [
                {
                    "local_id": "full",
                    "name": "监控网关G-01",
                    "entity_type_key": "equipment",
                    "aliases": [],
                    "properties": {},
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "监控网关G-01"}],
                },
                {
                    "local_id": "short",
                    "name": "G-01",
                    "entity_type_key": "equipment",
                    "aliases": [],
                    "properties": {},
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "监控网关G-01（G-01）"}],
                },
            ],
            "relations": [],
        }
    )

    result = _aggregate_extraction_outputs(
        [("unit", payload)],
        snapshot={"relation_types": []},
    )

    assert len(result["entities"]) == 1
    assert "G-01" in result["entities"][0]["aliases"]


def test_extraction_aggregation_rejects_cross_endpoint_identifier_pollution():
    payload = GraphExtractionPayload.model_validate(
        {
            "entities": [
                {
                    "local_id": "inverter",
                    "name": "inverter A-01",
                    "entity_type_key": "inverter",
                    "aliases": ["A-01", "WO-77"],
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "inverter A-01 from supplier; work order WO-77"}],
                },
                {
                    "local_id": "supplier",
                    "name": "supplier",
                    "entity_type_key": "supplier",
                    "aliases": ["A-01"],
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "inverter A-01 from supplier"}],
                },
                {
                    "local_id": "work-order",
                    "name": "work order WO-77",
                    "entity_type_key": "work_order",
                    "aliases": ["WO-77", "A-01"],
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "work order WO-77 repairs inverter A-01"}],
                },
            ],
            "relations": [],
        }
    )

    result = _aggregate_extraction_outputs([("unit", payload)], snapshot={"relation_types": []})
    aliases = {row["canonical_name"]: row["aliases"] for row in result["entities"]}
    assert aliases["inverter A-01"] == ["A-01"]
    assert aliases["supplier"] == []
    assert aliases["work order WO-77"] == ["WO-77"]


def test_gold_standard_reports_precision_recall_direction_and_alias_metrics():
    gold = {
        "gold_entities": [
            {"gold_id": "site", "canonical_name": "青岩光伏电站", "entity_type_key": "photovoltaic_site", "aliases": ["青岩光伏电站项目"]},
            {"gold_id": "inv", "canonical_name": "逆变器A-01", "entity_type_key": "inverter", "aliases": ["A-01"]},
        ],
        "gold_relations": [{"source_gold_id": "site", "relation_type_key": "contains_inverter", "target_gold_id": "inv", "directed": True}],
    }
    predictions = {
        "entities": [
            {"canonical_name": "青岩光伏电站", "entity_type_key": "photovoltaic_site", "aliases": ["青岩光伏电站项目"]},
            {"canonical_name": "逆变器A-01", "entity_type_key": "inverter", "aliases": ["A-01"]},
        ],
        "relations": [{"source_name": "青岩光伏电站项目", "relation_type_key": "contains_inverter", "target_name": "A-01", "directed": False}],
    }

    assert score_predictions(gold, predictions) == {
        "entity_precision": 1.0,
        "entity_recall": 1.0,
        "entity_f1": 1.0,
        "relation_precision": 1.0,
        "relation_recall": 1.0,
        "relation_f1": 1.0,
        "type_accuracy": 1.0,
        "relation_direction_accuracy": 0.0,
        "alias_merge_rate": 1.0,
        "duplicate_entities": 0,
        "extra_entities": 0,
        "extra_relations": 0,
        "gold_entity_count": 2,
        "gold_relation_count": 1,
        "predicted_entity_count": 2,
        "predicted_relation_count": 1,
    }


def test_export_validation_keeps_id_backed_constraint_relation():
    snapshot = {
        "entity_types": [
            {"id": "site-id", "key": "site"},
            {"id": "person-id", "key": "person"},
        ],
        "relation_types": [{"id": "reports-id", "key": "reports_to"}],
        "relation_constraints": [
            {
                "relation_type_id": "reports-id",
                "source_entity_type_id": "person-id",
                "target_entity_type_id": "person-id",
            }
        ],
    }
    predictions = {
        "entities": [
            {"canonical_name": "Alice", "entity_type_key": "person", "aliases": []},
            {"canonical_name": "Bob", "entity_type_key": "person", "aliases": []},
        ],
        "relations": [
            {
                "source_name": "Alice",
                "relation_type_key": "reports_to",
                "target_name": "Bob",
                "directed": True,
            }
        ],
    }

    assert _validate_export(predictions, snapshot)["relations"] == predictions["relations"]


def test_semantic_alignment_keeps_strict_keys_and_accepts_explicit_inverse():
    gold = {
        "gold_entities": [
            {"gold_id": "site", "canonical_name": "North Site", "entity_type_key": "photovoltaic_site", "aliases": []},
            {"gold_id": "firm", "canonical_name": "Design Firm", "entity_type_key": "design_firm", "aliases": []},
        ],
        "gold_relations": [{"source_gold_id": "firm", "relation_type_key": "designed", "target_gold_id": "site", "directed": True}],
    }
    predictions = {
        "entities": [
            {"canonical_name": "North Site", "entity_type_key": "project", "aliases": []},
            {"canonical_name": "Design Firm", "entity_type_key": "organization", "aliases": []},
        ],
        "relations": [{"source_name": "North Site", "relation_type_key": "designed_by", "target_name": "Design Firm", "directed": True}],
    }
    alignment = {
        "accepted_entity_type_aliases": {"project": ["photovoltaic_site"]},
        "relation_equivalences": [{"predicted_relation_key": "designed_by", "gold_relation_key": "designed", "reverse_endpoints": True}],
    }

    assert score_predictions(gold, predictions)["relation_recall"] == 0.0
    semantic = score_predictions(gold, predictions, semantic_alignment=alignment)
    assert semantic["relation_recall"] == 1.0
    assert semantic["type_accuracy"] == 0.5
