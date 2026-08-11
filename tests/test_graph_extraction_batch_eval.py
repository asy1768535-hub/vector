from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.schemas.graph_extraction import GraphExtractionPayload
from app.services.graph_candidate_aggregation import CandidateReplayError
from app.services.graph_extraction_batch_eval import (
    BatchChunkBoundary,
    BatchExtractionParseError,
    GraphExtractionBatchInput,
    _call_provider_with_batch_renewal,
    _keep_batch_leases_live_during_persistence,
    _prepared_input,
    _safe_batch_prefix,
    build_batched_graph_extraction_messages,
    claim_eval_graph_extraction_batch,
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
        "relation_constraints": [
            {
                "relation_type_key": "belongs_to",
                "source_entity_type_key": "person",
                "target_entity_type_key": "department",
            }
        ],
    }
    encoded = json.dumps(routed, sort_keys=True)
    assert "00000000" not in encoded
    assert "default_review_policy" not in encoded


def test_schema_router_selects_related_types_and_falls_back_when_uncertain():
    snapshot = _ontology()
    snapshot["relation_types"][0].update(label="负责", description="负责、归属或隶属")
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
    assert "match a supplied relation_constraint" in messages[0]["content"]
    assert "most specific" in messages[0]["content"]
    assert "never substring guesses" in messages[0]["content"]
    assert "reverse endpoints" in messages[0]["content"]
    assert "top-level object must contain exactly one key named batches" in messages[0]["content"]
    assert '"confidence":0.9' in messages[0]["content"]
    assert '"confidence":0.0' not in messages[0]["content"]
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert [row["batch_key"] for row in payload["batches"]] == ["u0", "u1"]
    expected_ontology = route_ontology_for_extraction(_ontology())
    assert payload["frozen_ontology"] == {
        **expected_ontology,
        "constraints": expected_ontology["relation_constraints"],
    }
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


def test_batch_parser_accepts_batch_key_map_shape_from_real_model():
    raw = json.dumps(
        {
            "overview": {
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
        }
    )

    parsed = parse_batched_graph_extraction_output(
        raw,
        expected_keys=("overview",),
        allowed_entity_type_keys={"product"},
        allowed_relation_type_keys={"contains"},
    )

    assert parsed["overview"].entities[0].name == "Widget"
    with pytest.raises(BatchExtractionParseError, match="only batches"):
        parse_batched_graph_extraction_output(raw, expected_keys=("equipment",))


def test_batch_parser_omits_relations_with_undeclared_local_endpoints():
    raw = json.dumps(
        {
            "batches": [
                {
                    "batch_key": "equipment",
                    "entities": [
                        {
                            "local_id": "e1",
                            "name": "INV-A-01",
                            "entity_type_key": "equipment",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": "c0", "quote": "INV-A-01"}],
                        }
                    ],
                    "relations": [
                        {
                            "source_local_id": "e1",
                            "relation_type_key": "belongs_to",
                            "target_local_id": "missing_project",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": "c0", "quote": "INV-A-01 belongs"}],
                        }
                    ],
                }
            ]
        }
    )

    parsed = parse_batched_graph_extraction_output(
        raw,
        expected_keys=("equipment",),
        allowed_entity_type_keys={"equipment"},
        allowed_relation_type_keys={"belongs_to"},
    )

    assert len(parsed["equipment"].entities) == 1
    assert parsed["equipment"].relations == []


def test_batch_parser_coerces_non_negative_numeric_evidence_context_refs():
    raw = json.dumps(
        {
            "batches": [
                {
                    "batch_key": "equipment",
                    "entities": [
                        {
                            "local_id": "e1",
                            "name": "INV-A-01",
                            "entity_type_key": "equipment",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": 0, "quote": "INV-A-01"}],
                        },
                        {
                            "local_id": "e2",
                            "name": "North Site",
                            "entity_type_key": "site",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": 0, "quote": "North Site"}],
                        },
                    ],
                    "relations": [
                        {
                            "source_local_id": "e1",
                            "relation_type_key": "belongs_to",
                            "target_local_id": "e2",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": 0, "quote": "INV-A-01 belongs to North Site"}],
                        }
                    ],
                }
            ]
        }
    )

    parsed = parse_batched_graph_extraction_output(
        raw,
        expected_keys=("equipment",),
        allowed_entity_type_keys={"equipment", "site"},
        allowed_relation_type_keys={"belongs_to"},
    )

    assert parsed["equipment"].relations[0].evidence[0].context_ref == "0"


def test_batch_parser_rejects_boolean_evidence_context_refs():
    raw = json.dumps(
        {
            "batches": [
                {
                    "batch_key": "equipment",
                    "entities": [
                        {
                            "local_id": "e1",
                            "name": "INV-A-01",
                            "entity_type_key": "equipment",
                            "confidence": 0.9,
                            "evidence": [{"context_ref": True, "quote": "INV-A-01"}],
                        }
                    ],
                    "relations": [],
                }
            ]
        }
    )

    with pytest.raises(BatchExtractionParseError, match="payload schema"):
        parse_batched_graph_extraction_output(raw, expected_keys=("equipment",))


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


def test_prepared_batch_input_removes_neighbors_for_center_only_policy():
    context = {
        "document": {"title": "Asset file", "metadata": {"kind": "legal"}},
        "chunks": [
            {"context_ref": "c0", "role": "current", "text": "center"},
            {"context_ref": "p1", "role": "neighbor", "text": "previous"},
            {"context_ref": "n1", "role": "neighbor", "text": "next"},
        ],
    }
    prepared = _prepared(0)
    prepared = replace(
        prepared,
        center_only=True,
        messages=build_graph_extraction_messages(
            context_text=json.dumps(context),
            ontology_snapshot=_ontology(),
            center_only=True,
        ),
    )

    context_text, ontology = _prepared_input(prepared)
    projected = json.loads(context_text)

    assert ontology == _ontology()
    assert projected["document"] == context["document"]
    assert projected["chunks"] == [context["chunks"][0]]


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


def test_batch_claim_excludes_jobs_with_an_active_batch():
    class EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

    class CaptureDB:
        def __init__(self):
            self.statements = []

        def begin(self):
            return _SessionContext()

        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyResult()

    db = CaptureDB()
    assert not asyncio.run(
        claim_eval_graph_extraction_batch(
            db,
            worker_id="worker-1",
            batch_size=8,
            lease_seconds=180,
            max_attempts=3,
        )
    )

    compiled = db.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled).upper()
    assert "NOT (EXISTS" in sql
    assert "SKIP LOCKED" in sql
    assert "processing" in compiled.params.values()


@pytest.fixture(autouse=True)
def _live_batch_unit_leases(monkeypatch):
    monkeypatch.setattr(
        "app.services.graph_extraction_batch_eval.renew_graph_extraction_unit_lease",
        AsyncMock(return_value=True),
    )


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


def test_batch_retry_replays_unit_payloads_without_provider_or_semantic_cache():
    prepared = tuple(
        replace(_prepared(index), has_prior_attempts=True)
        for index in range(3)
    )
    units = tuple(
        SimpleNamespace(id=row.unit_id, claim_token=row.claim_token)
        for row in prepared
    )
    replay = GraphExtractionPayload(entities=[], relations=[])
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._load_prepared_replay",
            new=AsyncMock(return_value=replay),
        ) as load_replay,
        patch(
            "app.services.graph_extraction_batch_eval._load_prepared_cache",
            new=AsyncMock(),
        ) as load_cache,
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(return_value=False),
        ) as persist,
        patch(
            "app.services.graph_extraction_batch_eval._call_provider_with_batch_renewal",
            new=AsyncMock(),
        ) as provider_call,
    ):
        result = asyncio.run(
            process_eval_graph_extraction_batch(_sessions, units=units)
        )

    assert result.provider_call_count == 0
    assert result.outcomes == Counter({"succeeded": 3})
    assert load_replay.await_count == 3
    load_cache.assert_not_awaited()
    provider_call.assert_not_awaited()
    assert all(
        not call.kwargs["prepared"].cache_hit
        for call in persist.await_args_list
    )


def test_batch_replay_mismatch_fails_only_the_unit_and_keeps_worker_loop_alive():
    prepared = tuple(
        replace(_prepared(index), has_prior_attempts=True)
        for index in range(2)
    )
    units = tuple(
        SimpleNamespace(id=row.unit_id, claim_token=row.claim_token)
        for row in prepared
    )
    replay = GraphExtractionPayload(entities=[], relations=[])
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._load_prepared_replay",
            new=AsyncMock(return_value=replay),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._persist_candidate_result",
            new=AsyncMock(
                side_effect=[
                    CandidateReplayError(
                        "entity_occurrence_replay_mismatch",
                        "replayed payload changed",
                    ),
                    False,
                ]
            ),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._finish_claim_after_error",
            new=AsyncMock(return_value=True),
        ) as finish,
    ):
        result = asyncio.run(
            process_eval_graph_extraction_batch(_sessions, units=units)
        )

    assert result.outcomes == Counter({"failed": 1, "succeeded": 1})
    assert finish.await_args.kwargs["error_code"] == (
        "entity_occurrence_replay_mismatch"
    )
    assert finish.await_args.kwargs["error_message"] == "CandidateReplayError"


def test_batch_preparation_failure_falls_back_to_single_units():
    units = tuple(SimpleNamespace(id=uuid.uuid4(), claim_token=uuid.uuid4()) for _ in range(2))
    single_result = SimpleNamespace(
        outcome="succeeded",
        ready_for_materialization=False,
    )
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=ValueError("incompatible batch")),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.process_graph_extraction_unit",
            new=AsyncMock(return_value=single_result),
        ) as process_single,
        patch(
            "app.services.graph_extraction_batch_eval._finish_batch",
            new=AsyncMock(),
        ) as finish_batch,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.outcomes == Counter({"succeeded": 2})
    assert result.provider_call_count == 2
    assert process_single.await_count == 2
    finish_batch.assert_not_awaited()


def test_full_schema_batches_split_before_provider_call():
    units = tuple(SimpleNamespace(id=uuid.uuid4(), claim_token=uuid.uuid4()) for _ in range(4))
    prepared = tuple(
        SimpleNamespace(
            unit_id=unit.id,
            model_config_snapshot={"schema_routing_enabled": False},
        )
        for unit in units
    )
    child_result = SimpleNamespace(
        outcomes=Counter({"succeeded": 2}), provider_call_count=1, ready_job_ids=()
    )
    with (
        patch(
            "app.services.graph_extraction_batch_eval._prepare_graph_extraction_unit",
            new=AsyncMock(side_effect=prepared),
        ),
        patch(
            "app.services.graph_extraction_batch_eval._consume_cached_batch_rows",
            new=AsyncMock(return_value=(Counter(), set(), units)),
        ),
        patch(
            "app.services.graph_extraction_batch_eval.process_eval_graph_extraction_batch",
            new=AsyncMock(return_value=child_result),
        ) as process_child,
        patch(
            "app.services.graph_extraction_batch_eval.build_batched_graph_extraction_messages"
        ) as build_messages,
    ):
        result = asyncio.run(process_eval_graph_extraction_batch(_sessions, units=units))

    assert result.outcomes == Counter({"succeeded": 4})
    assert result.provider_call_count == 2
    assert process_child.await_count == 2
    assert [call.kwargs["units"] for call in process_child.await_args_list] == [
        units[:2],
        units[2:],
    ]
    build_messages.assert_not_called()


def test_batch_orchestration_calls_provider_once_and_persists_every_unit():
    prepared = tuple(_prepared(index) for index in range(4))
    units = tuple(SimpleNamespace(id=row.unit_id, claim_token=row.claim_token) for row in prepared)
    content = json.dumps(
        {"batches": [{"batch_key": f"u{index}", "entities": [], "relations": []} for index in range(4)]}
    )
    attempt = SimpleNamespace(id=uuid.uuid4())
    guards = []

    class TrackingGuard:
        def __init__(self, rows):
            self.pending = {row.unit_id for row in rows}

        def lease_lost(self, unit_id):  # noqa: ARG002
            return False

        def release(self, unit_id):
            self.pending.discard(unit_id)

    class TrackingContext:
        def __init__(self, guard):
            self.guard = guard

        async def __aenter__(self):
            return self.guard

        async def __aexit__(self, exc_type, exc, traceback):  # noqa: ARG002
            return False

    def tracked_guard(*args, **kwargs):  # noqa: ARG001
        guard = TrackingGuard(kwargs["prepared_rows"])
        guards.append(guard)
        return TrackingContext(guard)

    async def persist_after_release(*args, **kwargs):  # noqa: ARG001
        row = kwargs["prepared"]
        assert row.unit_id not in guards[-1].pending
        return False

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
            new=AsyncMock(side_effect=persist_after_release),
        ) as persist,
        patch(
            "app.services.graph_extraction_batch_eval._keep_batch_leases_live_during_persistence",
            side_effect=tracked_guard,
        ),
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


def test_persistence_keeps_all_pending_batch_leases_live():
    prepared = tuple(_prepared(index) for index in range(2))
    renewal = AsyncMock(return_value=True)

    async def exercise():
        async with _keep_batch_leases_live_during_persistence(
            _sessions,
            prepared_rows=prepared,
            lease_seconds=180,
            renew_seconds=0.001,
        ) as guard:
            await asyncio.sleep(0.01)
            guard.release(prepared[0].unit_id)
            await asyncio.sleep(0.005)
            guard.release(prepared[1].unit_id)

    with patch(
        "app.services.graph_extraction_batch_eval.renew_graph_extraction_unit_lease",
        renewal,
    ):
        asyncio.run(exercise())

    renewed_ids = [call.kwargs["unit_id"] for call in renewal.await_args_list]
    assert renewed_ids.count(prepared[0].unit_id) >= 2
    assert renewed_ids.count(prepared[1].unit_id) > renewed_ids.count(
        prepared[0].unit_id
    )


def test_persistence_guard_marks_only_the_unit_that_lost_its_lease():
    prepared = tuple(_prepared(index) for index in range(2))

    async def exercise():
        async with _keep_batch_leases_live_during_persistence(
            _sessions,
            prepared_rows=prepared,
            lease_seconds=180,
            renew_seconds=3600,
        ) as guard:
            assert not guard.lease_lost(prepared[0].unit_id)
            assert guard.lease_lost(prepared[1].unit_id)
            guard.release(prepared[0].unit_id)
            guard.release(prepared[1].unit_id)

    with patch(
        "app.services.graph_extraction_batch_eval.renew_graph_extraction_unit_lease",
        new=AsyncMock(side_effect=[True, False]),
    ):
        asyncio.run(exercise())


def test_provider_batch_renews_every_unit_before_dispatch():
    prepared = tuple(_prepared(index) for index in range(2))
    events = []

    async def renew(*args, **kwargs):  # noqa: ARG001
        events.append(("renew", kwargs["unit_id"]))
        return True

    class Provider:
        async def extract(self, messages):  # noqa: ARG002
            events.append(("provider", None))
            return _response('{"batches": []}')

    with patch(
        "app.services.graph_extraction_batch_eval.renew_graph_extraction_unit_lease",
        new=AsyncMock(side_effect=renew),
    ):
        response, lease_lost = asyncio.run(
            _call_provider_with_batch_renewal(
                _sessions,
                provider=Provider(),
                messages=[],
                prepared_rows=prepared,
                lease_seconds=180,
                renew_seconds=30,
            )
        )

    assert response is not None
    assert lease_lost is False
    assert events == [
        ("renew", prepared[0].unit_id),
        ("renew", prepared[1].unit_id),
        ("provider", None),
    ]


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
