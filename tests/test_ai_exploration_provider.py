from __future__ import annotations

import json

import httpx
import pytest

from app.services.graph_extraction_provider import (
    GraphExtractionProviderError,
    OpenAICompatibleGraphExtractor,
    ProviderResponse,
    request_preview,
)
from app.services.graph_extraction_batch_eval import (
    GraphExtractionBatchInput,
    build_batched_graph_extraction_messages,
    plan_graph_extraction_batches,
)
from app.services.graph_schema_discovery import DiscoveryText, discover_business_schema


def _messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "discover Schema"},
        {"role": "user", "content": '{"excerpts":[{"key":"d1","text":"王芳向周强汇报"}]}'},
    ]


def _schema_response() -> str:
    return json.dumps(
        {
            "entity_types": [
                {"key": "person", "label": "人员", "description": "人员", "attributes": []}
            ],
            "relation_types": [
                {
                    "key": "reports_to",
                    "label": "汇报给",
                    "description": "汇报关系",
                    "direction": "directed",
                    "attributes": [],
                }
            ],
            "constraints": [
                {
                    "source_type_key": "person",
                    "relation_type_key": "reports_to",
                    "target_type_key": "person",
                    "cardinality": "many_to_one",
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_response_format_unsupported_retries_once_without_response_format():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(400, text='{"error":"response_format is unsupported"}')
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    provider = OpenAICompatibleGraphExtractor(
        base_url="http://local.test/v1",
        model="qwen3.5-9b",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    response = await provider.extract(_messages())

    assert response.content == "{}"
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]


def test_request_preview_is_desensitized():
    preview = request_preview(_messages(), max_output_tokens=2048)

    assert "王芳" not in json.dumps(preview, ensure_ascii=False)
    assert preview["messages"][1]["content_chars"] > 0
    assert preview["estimated_request_tokens"] > 0


@pytest.mark.asyncio
async def test_discovery_retries_transient_provider_failure_and_rejects_truncation():
    class RetryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, _messages):
            self.calls += 1
            if self.calls == 1:
                raise GraphExtractionProviderError(
                    "timeout", "timed out", latency_ms=1
                )
            return ProviderResponse(
                content=_schema_response(),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=10,
                output_token_count=20,
                latency_ms=1,
                finish_reason="stop",
            )

    provider = RetryProvider()
    draft = await discover_business_schema(
        [DiscoveryText("d1", "王芳向周强汇报")],
        provider=provider,
        source_hash="a" * 64,
        context_window_tokens=4096,
        max_output_tokens=2048,
    )
    assert provider.calls == 2
    assert draft.entity_types[0].key == "person"

    class TruncatedProvider:
        async def extract(self, _messages):
            return ProviderResponse(
                content='{"entity_types":',
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=10,
                output_token_count=2048,
                latency_ms=1,
                finish_reason="length",
            )

    with pytest.raises(ValueError, match="truncated"):
        await discover_business_schema(
            [DiscoveryText("d1", "王芳向周强汇报")],
            provider=TruncatedProvider(),
            source_hash="a" * 64,
            context_window_tokens=4096,
            max_output_tokens=2048,
        )


@pytest.mark.asyncio
async def test_discovery_protocol_repair_is_attempted_only_once():
    invalid = json.loads(_schema_response())
    invalid["relation_types"][0]["direction"] = "outgoing"
    invalid["constraints"][0]["cardinality"] = "sometimes"

    class InvalidProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = []

        async def extract(self, messages):
            self.calls += 1
            self.messages.append(messages)
            return ProviderResponse(
                content=json.dumps(invalid),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=10,
                output_token_count=20,
                latency_ms=1,
                finish_reason="stop",
            )

    provider = InvalidProvider()
    with pytest.raises(ValueError, match="repair response failed validation"):
        await discover_business_schema(
            [DiscoveryText("d1", "A reports to B.")],
            provider=provider,
            source_hash="d" * 64,
            context_window_tokens=4096,
            max_output_tokens=2048,
        )

    assert provider.calls == 2
    repair_payload = json.loads(provider.messages[1][1]["content"])
    assert all(
        "input_value" not in error
        for error in repair_payload["validation_errors"]
    )
    assert any(
        "direction" in error
        for error in repair_payload["validation_errors"]
    )
    assert any(
        "cardinality" in error
        for error in repair_payload["validation_errors"]
    )


def test_extraction_planner_sends_split_body_keys_and_text():
    snapshot = {
        "entity_types": [
            {
                "id": "e1",
                "key": "asset",
                "label": "Asset",
                "properties_schema": {},
                "active_attribute_definitions": [],
            }
        ],
        "relation_types": [],
        "relation_constraints": [],
    }
    original = "正文" + ("甲" * 5000)
    planned = plan_graph_extraction_batches(
        ontology_snapshot=snapshot,
        inputs=(GraphExtractionBatchInput("u0", original),),
        schema_routing_enabled=False,
        context_window_tokens=4096,
        max_output_tokens=512,
    )
    fragments = [item for group in planned for item in group]
    assert len(fragments) > 1
    assert all(item.source_key == "u0" for item in fragments)
    messages = build_batched_graph_extraction_messages(
        ontology_snapshot=snapshot,
        batches=(fragments[0],),
        context_window_tokens=4096,
        max_output_tokens=512,
    )
    assert fragments[0].context_text in messages[1]["content"]
    assert original not in messages[1]["content"]
