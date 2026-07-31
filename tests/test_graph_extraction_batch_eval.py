from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.graph_extraction_batch_eval import (
    BatchChunkBoundary,
    BatchExtractionParseError,
    GraphExtractionBatchInput,
    _safe_batch_prefix,
    build_batched_graph_extraction_messages,
    parse_batched_graph_extraction_output,
    process_eval_graph_extraction_batch,
    route_ontology_for_extraction,
)
from app.services.graph_extraction_eval_runtime import eval_batch_size
from app.services.graph_extraction_prompt import build_graph_extraction_messages
from app.services.graph_extraction_provider import ProviderResponse
from app.services.graph_extraction_worker import PreparedGraphExtractionUnit


def _ontology() -> dict:
    return {
        "ontology_version_id": "00000000-0000-0000-0000-000000000001",
        "entity_types": [
            {
                "id": "10000000-0000-0000-0000-000000000001",
                "key": "person",
                "properties_schema": {"type": "object"},
                "active_attribute_definitions": [{"key": "employee_id"}],
            },
            {
                "id": "10000000-0000-0000-0000-000000000002",
                "key": "department",
                "properties_schema": {},
                "active_attribute_definitions": [],
            },
        ],
        "relation_types": [
            {
                "id": "20000000-0000-0000-0000-000000000001",
                "key": "belongs_to",
                "direction": "directed",
                "requires_evidence": True,
                "default_review_policy": "auto_active",
                "properties_schema": {},
                "active_attribute_definitions": [],
            }
        ],
        "relation_constraints": [
            {
                "relation_type_id": "20000000-0000-0000-0000-000000000001",
                "source_entity_type_id": "10000000-0000-0000-0000-000000000001",
                "target_entity_type_id": "10000000-0000-0000-0000-000000000002",
                "cardinality": "many_to_one",
                "requires_review": False,
            }
        ],
    }


def test_schema_router_keeps_only_model_facing_contract():
    routed = route_ontology_for_extraction(_ontology())

    assert routed == {
        "entity_types": [
            {
                "key": "person",
                "properties_schema": {"type": "object"},
                "active_attribute_definitions": [{"key": "employee_id"}],
            },
            {
                "key": "department",
                "properties_schema": {},
                "active_attribute_definitions": [],
            },
        ],
        "relation_types": [
            {
                "key": "belongs_to",
                "direction": "directed",
                "properties_schema": {},
                "active_attribute_definitions": [],
            }
        ],
    }
    encoded = json.dumps(routed, sort_keys=True)
    assert "00000000" not in encoded
    assert "relation_constraints" not in encoded
    assert "default_review_policy" not in encoded


def test_schema_router_selects_related_types_and_falls_back_when_uncertain():
    snapshot = _ontology()
    snapshot["entity_types"][0].update(label="人员", description="员工和人员信息")
    snapshot["entity_types"][1].update(label="部门", description="组织部门信息")
    snapshot["entity_types"].append(
        {
            "id": "10000000-0000-0000-0000-000000000003",
            "key": "product",
            "label": "产品",
            "description": "产品目录",
            "properties_schema": {},
            "active_attribute_definitions": [],
        }
    )
    routed = route_ontology_for_extraction(snapshot, context_text="人力资源部门负责入职", enabled=True)
    assert {row["key"] for row in routed["entity_types"]} == {
        "person",
        "department",
    }
    assert [row["key"] for row in routed["relation_types"]] == ["belongs_to"]

    fallback = route_ontology_for_extraction(snapshot, context_text="完全无法分类的内容", enabled=True)
    assert len(fallback["entity_types"]) == 3


def test_batch_parser_rejects_types_outside_routed_subset():
    raw = json.dumps(
        {
            "batches": [
                {
                    "batch_key": "u0",
                    "entities": [
                        {
                            "local_id": "e1",
                            "name": "Widget",
                            "entity_type_key": "product",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": "c0", "quote": "Widget"}],
                        }
                    ],
                    "relations": [],
                }
            ]
        }
    )
    with pytest.raises(BatchExtractionParseError, match="payload schema"):
        parse_batched_graph_extraction_output(
            raw,
            expected_keys=("u0",),
            allowed_entity_type_keys={"person"},
            allowed_relation_type_keys={"belongs_to"},
        )


def test_batch_prompt_shares_one_compact_schema_and_scopes_local_ids():
    messages = build_batched_graph_extraction_messages(
        ontology_snapshot=_ontology(),
        batches=(
            GraphExtractionBatchInput("u0", '{"c0":{"text":"Alice"}}'),
            GraphExtractionBatchInput("u1", '{"c0":{"text":"Bob"}}'),
        ),
    )

    assert len(messages) == 2
    assert "batch_key" in messages[0]["content"]
    assert "local_id values are scoped to one batch" in messages[0]["content"]
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert [row["batch_key"] for row in payload["batches"]] == ["u0", "u1"]
    assert payload["frozen_ontology"] == route_ontology_for_extraction(_ontology())
    assert "ontology_version_id" not in payload["frozen_ontology"]


def test_batch_parser_requires_exact_unique_requested_keys():
    raw = json.dumps(
        {
            "batches": [
                {"batch_key": "u0", "entities": [], "relations": []},
                {"batch_key": "u1", "entities": [], "relations": []},
            ]
        }
    )
    parsed = parse_batched_graph_extraction_output(raw, expected_keys=("u0", "u1"))
    assert list(parsed) == ["u0", "u1"]

    with pytest.raises(BatchExtractionParseError, match="exactly"):
        parse_batched_graph_extraction_output(raw, expected_keys=("u0", "u2"))
    duplicate = json.dumps(
        {
            "batches": [
                {"batch_key": "u0", "entities": [], "relations": []},
                {"batch_key": "u0", "entities": [], "relations": []},
            ]
        }
    )
    with pytest.raises(BatchExtractionParseError, match="unique"):
        parse_batched_graph_extraction_output(duplicate, expected_keys=("u0",))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("1", 1), ("2", 2), ("4", 4), ("8", 8)],
)
def test_eval_batch_size_defaults_to_single_unit(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("GRAPH_EXTRACTION_EVAL_BATCH_SIZE", raising=False)
    else:
        monkeypatch.setenv("GRAPH_EXTRACTION_EVAL_BATCH_SIZE", raw)
    assert eval_batch_size() == expected


@pytest.mark.parametrize("raw", ["0", "9", "true", "1.5"])
def test_eval_batch_size_rejects_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("GRAPH_EXTRACTION_EVAL_BATCH_SIZE", raw)
    with pytest.raises(ValueError, match="batch size"):
        eval_batch_size()


def _boundary(
    index: int,
    *,
    seq: int | None = None,
    title_path: tuple[str, ...] = ("section",),
    structured_kind: str | None = None,
    block_id: uuid.UUID | None = None,
) -> BatchChunkBoundary:
    return BatchChunkBoundary(
        unit_id=uuid.UUID(f"50000000-0000-0000-0000-{index + 1:012d}"),
        seq=index if seq is None else seq,
        title_path=title_path,
        structured_kind=structured_kind,
        block_id=block_id,
    )


def test_safe_batch_prefix_groups_only_contiguous_same_section_chunks():
    rows = (_boundary(0), _boundary(1), _boundary(2, title_path=("next",)))

    assert _safe_batch_prefix(rows) == (rows[0].unit_id, rows[1].unit_id)
    assert _safe_batch_prefix((_boundary(0), _boundary(1, seq=3))) == (rows[0].unit_id,)


def test_safe_batch_prefix_preserves_table_and_list_block_boundaries():
    block = uuid.UUID("60000000-0000-0000-0000-000000000001")
    other = uuid.UUID("60000000-0000-0000-0000-000000000002")
    table_rows = (
        _boundary(0, structured_kind="table", block_id=block),
        _boundary(1, structured_kind="table", block_id=block),
        _boundary(2, structured_kind="table", block_id=other),
    )
    list_to_text = (
        _boundary(0, structured_kind="list", block_id=block),
        _boundary(1),
    )

    assert _safe_batch_prefix(table_rows) == (table_rows[0].unit_id, table_rows[1].unit_id)
    assert _safe_batch_prefix(list_to_text) == (list_to_text[0].unit_id,)
    assert _safe_batch_prefix(()) == ()


def _prepared(index: int) -> PreparedGraphExtractionUnit:
    return PreparedGraphExtractionUnit(
        unit_id=uuid.UUID(f"10000000-0000-0000-0000-{index + 1:012d}"),
        job_id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        context_snapshot_id=uuid.UUID(f"30000000-0000-0000-0000-{index + 1:012d}"),
        claim_token=uuid.UUID(f"40000000-0000-0000-0000-{index + 1:012d}"),
        messages=build_graph_extraction_messages(
            context_text=json.dumps({"c0": {"text": f"center {index}"}}),
            ontology_snapshot=_ontology(),
            center_only=True,
        ),
        model_config_snapshot={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            "timeout_seconds": 120.0,
            "max_output_tokens": 8000,
        },
        model_config_hash="a" * 64,
    )


def _response(content: str, *, finish_reason: str = "stop") -> ProviderResponse:
    return ProviderResponse(
        content=content,
        provider_request_id="batch-request-1",
        raw_response=content,
        request_payload_hash="b" * 64,
        input_token_count=100,
        output_token_count=20,
        latency_ms=50,
        finish_reason=finish_reason,
    )


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):  # noqa: ARG002
        return False


def _sessions() -> _SessionContext:
    return _SessionContext()


def test_batch_orchestration_skips_provider_when_every_unit_is_cached():
    prepared = tuple(_prepared(index) for index in range(4))
    units = tuple(SimpleNamespace(id=row.unit_id, claim_token=row.claim_token) for row in prepared)
    cached = GraphExtractionPayload(entities=[], relations=[])
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._load_prepared_cache",
            new=AsyncMock(return_value=cached),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(return_value=False),
        ) as persist,
        patch(
            "app.services.graph_extraction_batch_eval._call_provider_with_batch_renewal",
            new=AsyncMock(),
        ) as provider_call,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.provider_call_count == 0
    assert result.outcomes == Counter({"succeeded": 4})
    provider_call.assert_not_awaited()
    assert persist.await_count == 4
    assert all(call.kwargs["prepared"].cache_hit for call in persist.await_args_list)


def test_batch_orchestration_calls_provider_once_and_persists_every_unit():
    prepared = tuple(_prepared(index) for index in range(4))
    units = tuple(SimpleNamespace(id=row.unit_id, claim_token=row.claim_token) for row in prepared)
    content = json.dumps(
        {"batches": [{"batch_key": f"u{index}", "entities": [], "relations": []} for index in range(4)]}
    )
    attempt = SimpleNamespace(id=uuid.uuid4())
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ) as create_attempt,
        patch(
            "app.services.graph_extraction_batch_eval._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._configured_provider",
            return_value=object(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._call_provider_with_batch_renewal",
            new=AsyncMock(return_value=(_response(content), False)),
        ) as provider_call,
        patch(
            "app.services.graph_extraction_batch_eval.finalize_attempt",
            new=AsyncMock(return_value=True),
        ) as finalize,
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(return_value=False),
        ) as persist,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.provider_call_count == 1
    assert result.outcomes == Counter({"succeeded": 4})
    assert provider_call.await_count == 1
    assert persist.await_count == 4
    assert create_attempt.await_count == 4
    memberships = [call.kwargs for call in create_attempt.await_args_list]
    assert len({row["batch_request_id"] for row in memberships}) == 1
    assert [row["batch_key"] for row in memberships] == ["u0", "u1", "u2", "u3"]
    assert [row["batch_ordinal"] for row in memberships] == [0, 1, 2, 3]
    assert finalize.await_count == 4
    completions = [call.kwargs["completion"] for call in finalize.await_args_list]
    assert completions[0].raw_response is not None
    assert completions[0].input_token_count is not None
    assert all(row.raw_response is None for row in completions[1:])
    assert all(row.input_token_count is None for row in completions[1:])
    assert all(row.parsed_response is not None for row in completions)


def test_batch_orchestration_splits_parseable_truncated_response():
    prepared = tuple(_prepared(index) for index in range(2))
    units = tuple(SimpleNamespace(id=row.unit_id, claim_token=row.claim_token) for row in prepared)
    content = json.dumps(
        {"batches": [{"batch_key": f"u{index}", "entities": [], "relations": []} for index in range(2)]}
    )
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.create_pending_attempt",
            new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._configured_provider",
            return_value=object(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._call_provider_with_batch_renewal",
            new=AsyncMock(return_value=(_response(content, finish_reason="length"), False)),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.finalize_attempt",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.process_graph_extraction_unit",
            new=AsyncMock(return_value=SimpleNamespace(outcome="succeeded", ready_for_materialization=False)),
        ) as process_single,
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(),
        ) as persist,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.outcomes == Counter({"succeeded": 2})
    assert process_single.await_count == 2
    persist.assert_not_awaited()


def test_batch_orchestration_splits_invalid_batch_response_into_single_units():
    prepared = tuple(_prepared(index) for index in range(2))
    units = tuple(SimpleNamespace(id=row.unit_id, claim_token=row.claim_token) for row in prepared)
    content = json.dumps({"batches": [{"batch_key": "u0", "entities": [], "relations": []}]})
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.create_pending_attempt",
            new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._configured_provider",
            return_value=object(),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._call_provider_with_batch_renewal",
            new=AsyncMock(return_value=(_response(content), False)),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.finalize_attempt",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.process_graph_extraction_unit",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    outcome="succeeded",
                    ready_for_materialization=False,
                )
            ),
        ) as process_single,
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(),
        ) as persist,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.outcomes == Counter({"succeeded": 2})
    assert result.provider_call_count == 3
    assert process_single.await_count == 2
    persist.assert_not_awaited()
