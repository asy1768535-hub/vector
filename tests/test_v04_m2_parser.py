from __future__ import annotations

import json

import pytest

from app.services.graph_extraction_parser import (
    GraphExtractionParseError,
    parse_graph_extraction_output,
)


def _payload() -> dict:
    return {
        "entities": [
            {
                "local_id": "e1",
                "name": "人力资源部",
                "entity_type_key": "department",
                "aliases": ["HR"],
                "properties": {"region": "CN"},
                "external_mapping_hints": [{"system": "oa", "key": "hr"}],
                "confidence": 0.95,
                "evidence": [{"context_ref": "c0", "quote": "人力资源部负责入职流程。"}],
            },
            {
                "local_id": "e2",
                "name": "员工入职流程",
                "entity_type_key": "process",
                "confidence": 0.9,
                "evidence": [{"context_ref": "c0", "quote": "人力资源部负责入职流程。"}],
            },
        ],
        "relations": [
            {
                "source_local_id": "e1",
                "relation_type_key": "responsible_for",
                "target_local_id": "e2",
                "properties": {},
                "confidence": 0.93,
                "evidence": [{"context_ref": "c0", "quote": "人力资源部负责入职流程。"}],
            }
        ],
    }


def test_parser_accepts_empty_and_full_payloads():
    empty = parse_graph_extraction_output('{"entities": [], "relations": []}')
    assert empty.entities == []
    assert empty.relations == []

    parsed = parse_graph_extraction_output(json.dumps(_payload(), ensure_ascii=False))
    assert [entity.local_id for entity in parsed.entities] == ["e1", "e2"]
    assert parsed.relations[0].target_local_id == "e2"


def test_parser_accepts_exactly_one_outer_json_fence():
    raw = "```json\n" + json.dumps(_payload(), ensure_ascii=False) + "\n```"
    assert len(parse_graph_extraction_output(raw).entities) == 2


@pytest.mark.parametrize(
    "raw",
    [
        "Here is JSON: {\"entities\": [], \"relations\": []}",
        "```json\n{\"entities\": [], \"relations\": []}\n``` trailing",
        "```json\n```json\n{\"entities\": [], \"relations\": []}\n```\n```",
    ],
)
def test_parser_rejects_prose_or_nested_fence_repair(raw: str):
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(raw)
    assert exc.value.parse_status == "invalid_json"


def test_parser_classifies_malformed_json_without_echoing_input():
    secret = "SECRET-CONTEXT-TEXT"
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output('{"entities": ["' + secret)
    assert exc.value.parse_status == "invalid_json"
    assert secret not in str(exc.value)
    assert len(str(exc.value)) <= 512


def test_parser_rejects_duplicate_local_ids():
    payload = _payload()
    payload["entities"][1]["local_id"] = "e1"
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(json.dumps(payload, ensure_ascii=False))
    assert exc.value.parse_status == "invalid_schema"
    assert "local_id" in str(exc.value)


@pytest.mark.parametrize("endpoint", ["source_local_id", "target_local_id"])
def test_parser_rejects_relation_endpoint_outside_unit(endpoint: str):
    payload = _payload()
    payload["relations"][0][endpoint] = "missing"
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(json.dumps(payload, ensure_ascii=False))
    assert exc.value.parse_status == "invalid_schema"
    assert "endpoint" in str(exc.value)


@pytest.mark.parametrize("extra_field", ["evidence_id", "source_span", "source_type", "database_uuid"])
def test_parser_rejects_model_provided_server_fields(extra_field: str):
    payload = _payload()
    payload["relations"][0][extra_field] = "untrusted"
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(json.dumps(payload, ensure_ascii=False))
    assert exc.value.parse_status == "invalid_schema"


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_parser_rejects_confidence_outside_unit_interval(confidence: float):
    payload = _payload()
    payload["entities"][0]["confidence"] = confidence
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(json.dumps(payload, ensure_ascii=False))
    assert exc.value.parse_status == "invalid_schema"


def test_parser_rejects_extra_top_level_fields():
    payload = _payload()
    payload["reasoning"] = "hidden chain of thought"
    with pytest.raises(GraphExtractionParseError) as exc:
        parse_graph_extraction_output(json.dumps(payload, ensure_ascii=False))
    assert exc.value.parse_status == "invalid_schema"
