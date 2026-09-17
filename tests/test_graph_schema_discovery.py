from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.ontology_version import OntologyVersion
from app.services.graph_extraction_batch_eval import (
    GraphExtractionBatchInput,
    build_batched_graph_extraction_messages,
    estimate_graph_extraction_request_tokens,
    plan_graph_extraction_batches,
)
from app.services.graph_extraction_provider import ProviderResponse
from app.services.graph_schema_discovery import (
    BusinessSchemaDraft,
    ConceptInventoryItem,
    ConceptInventoryRelationHint,
    DiscoveryText,
    SchemaDiscoveryEntityType,
    SchemaDiscoveryPayload,
    _merge_concept_inventory,
    build_schema_discovery_messages,
    compare_and_merge_active_schema,
    discover_business_schema,
    merge_schema_discovery_payloads,
    plan_schema_discovery_batches,
    schema_draft_to_snapshot,
)
from app.services.schema_discovery_runs import (
    _ready_batch_revisions,
    _sample_discovery_texts,
)
from app.services.token_budget import estimate_text_tokens

DISCOVERY_RESPONSE = {
    "entity_types": [
        {
            "key": "solar_farm",
            "label": "Solar farm",
            "description": "A photovoltaic generation site.",
            "attributes": [{"key": "capacity_mw", "description": "Rated capacity", "value_type": "number"}],
        },
        {
            "key": "inverter",
            "label": "Inverter",
            "description": "Power conversion equipment.",
            "attributes": [],
        },
    ],
    "relation_types": [
        {
            "key": "contains_inverter",
            "label": "Contains inverter",
            "description": "A site contains an inverter.",
            "direction": "directed",
            "attributes": [],
        }
    ],
    "constraints": [
        {
            "source_type_key": "solar_farm",
            "relation_type_key": "contains_inverter",
            "target_type_key": "inverter",
            "cardinality": "one_to_many",
        }
    ],
}


def test_interrupted_schema_discovery_runs_are_requeued():
    from app.services.schema_discovery_runs import (
        recover_interrupted_schema_discovery_runs,
    )

    result = SimpleNamespace(rowcount=2)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        execute = AsyncMock(return_value=result)

    session = Session()

    recovered = asyncio.run(
        recover_interrupted_schema_discovery_runs(session_factory=lambda: session)
    )

    assert recovered == 2
    assert session.execute.await_count == 1


def test_schema_discovery_timeout_requeues_interrupted_run(monkeypatch):
    from app.services import schema_discovery_runs as service

    processor = AsyncMock()
    recover = AsyncMock(return_value=1)

    async def timeout(_awaitable, *, timeout):
        _awaitable.close()
        assert timeout == 123
        raise TimeoutError

    monkeypatch.setattr(service, "process_next_schema_discovery_run", processor)
    monkeypatch.setattr(service, "recover_interrupted_schema_discovery_runs", recover)
    monkeypatch.setattr(service.asyncio, "wait_for", timeout)

    result = asyncio.run(
        service.process_next_schema_discovery_run_with_recovery(
            session_factory=object(), timeout_seconds=123
        )
    )

    assert result is None
    assert recover.await_count == 2


def test_batch_revision_query_excludes_failed_and_cancelled_imports():
    class EmptyScalars:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, statement):
            params = statement.compile().params
            status_filters = {
                item
                for value in params.values()
                for item in (value if isinstance(value, (list, tuple)) else [value])
                if isinstance(item, str)
            }
            assert {"cancelled", "failed"}.issubset(status_filters)
            return EmptyScalars()

    assert asyncio.run(
        _ready_batch_revisions(
            Session(),
            library_id=uuid.uuid4(),
            batch_id=uuid.uuid4(),
        )
    ) == []


def test_batch_schema_comparison_retains_omitted_active_types_and_reports_delta():
    active_version_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    legacy_id = uuid.uuid4()
    relation_id = uuid.uuid4()
    active = SimpleNamespace(
        version=SimpleNamespace(id=active_version_id),
        entity_types=(
            SimpleNamespace(id=customer_id, key="customer", label="客户", description="旧描述"),
            SimpleNamespace(id=legacy_id, key="legacy_asset", label="旧资产", description="旧类型"),
        ),
        relation_types=(
            SimpleNamespace(id=relation_id, key="owns", label="拥有", description="拥有关系", direction="directed"),
        ),
        attributes=(),
        constraints=(),
    )
    proposal = merge_schema_discovery_payloads(
        [SchemaDiscoveryPayload.model_validate({
            "entity_types": [
                {"key": "customer", "label": "客户主体", "description": "新描述"},
                {"key": "invoice", "label": "发票", "description": "发票类型"},
            ],
            "relation_types": [{
                "key": "owns", "label": "持有", "description": "更新后的拥有关系",
            }],
            "constraints": [],
        })],
        source_hash="d" * 64,
    )

    comparison = compare_and_merge_active_schema(proposal, active_bundle=active)

    assert {row.key for row in comparison.draft.entity_types} == {
        "customer", "invoice", "legacy_asset",
    }
    assert comparison.draft.entity_types[0].key == "customer"
    assert comparison.diff["added"]["entity_types"] == ["invoice"]
    assert comparison.diff["updated"]["entity_types"] == ["customer"]
    assert comparison.diff["retained"]["entity_types"] == ["legacy_asset"]
    assert comparison.diff["removed_candidates"]["entity_types"] == ["legacy_asset"]


def test_persist_schema_draft_writes_the_merged_active_schema():
    from app.services.graph_schema_discovery import persist_business_schema_draft

    active = SimpleNamespace(
        version=SimpleNamespace(id=uuid.uuid4()),
        entity_types=(
            SimpleNamespace(id=uuid.uuid4(), key="customer", label="客户", description="旧描述"),
            SimpleNamespace(id=uuid.uuid4(), key="legacy_asset", label="旧资产", description="旧类型"),
        ),
        relation_types=(),
        attributes=(),
        constraints=(),
    )
    draft = BusinessSchemaDraft(
        entity_types=(
            SchemaDiscoveryEntityType(key="customer", label="客户主体", description="新描述"),
            SchemaDiscoveryEntityType(key="invoice", label="发票", description="发票类型"),
        ),
        relation_types=(),
        constraints=(),
        source_hash="e" * 64,
    )
    entity_create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    relation_create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    attribute_create = AsyncMock()
    constraint_create = AsyncMock()
    db = SimpleNamespace(flush=AsyncMock())
    ontology_version = SimpleNamespace(id=uuid.uuid4(), status="draft")
    library = SimpleNamespace(id=uuid.uuid4())

    with (
        patch("app.services.graph_schema_discovery.ontology.create_entity_type", new=entity_create),
        patch("app.services.graph_schema_discovery.ontology.create_relation_type", new=relation_create),
        patch("app.services.graph_schema_discovery.ontology.create_attribute_definition", new=attribute_create),
        patch(
            "app.services.graph_schema_discovery.ontology.create_relation_type_constraint",
            new=constraint_create,
        ),
    ):
        snapshot = asyncio.run(
            persist_business_schema_draft(
                db,
                library=library,
                ontology_version=ontology_version,
                draft=draft,
                active_bundle=active,
            )
        )

    written_keys = [call.kwargs["key"] for call in entity_create.await_args_list]
    assert written_keys == ["customer", "invoice", "legacy_asset"]
    assert {row["key"] for row in snapshot["entity_types"]} == set(written_keys)
    db.flush.assert_awaited_once()


class DeterministicDiscoveryProvider:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []

    async def extract(self, messages: list[dict[str, str]]) -> ProviderResponse:
        self.messages.append(messages)
        return ProviderResponse(
            content=json.dumps(DISCOVERY_RESPONSE, ensure_ascii=False),
            provider_request_id=None,
            raw_response="{}",
            request_payload_hash="0" * 64,
            input_token_count=None,
            output_token_count=None,
            latency_ms=0,
            finish_reason="stop",
        )


def test_discovery_protocol_is_dynamic_and_not_an_exploration_allowlist():
    provider = DeterministicDiscoveryProvider()
    texts = [DiscoveryText(f"chunk-{index}", "光伏电站包含逆变器，额定容量为 50 MW。" * 100) for index in range(4)]

    draft = asyncio.run(
        discover_business_schema(
            texts,
            provider=provider,
            source_hash="a" * 64,
            context_window_tokens=1024,
            max_output_tokens=128,
        )
    )

    assert len(provider.messages) > 1
    assert {row.key for row in draft.entity_types} == {"solar_farm", "inverter"}
    assert {row.key for row in draft.relation_types} == {"contains_inverter"}
    assert draft.confirmed is False
    assert draft.status == "ai_draft"


def test_production_capability_runs_concept_inventory_before_schema_synthesis():
    class InventoryProvider(DeterministicDiscoveryProvider):
        supports_concept_inventory = True

        async def extract(self, messages):
            self.messages.append(messages)
            if "concept inventory protocol" in messages[0]["content"]:
                content = {
                    "concepts": [
                        {
                            "label": "Site",
                            "proposed_entity_type": "generation_site",
                            "aliases": [],
                            "identifiers": ["S-01"],
                            "proposed_relations": [],
                            "evidence_refs": ["chunk-0"],
                            "confidence": 0.9,
                            "ambiguity": [],
                        }
                    ]
                }
            else:
                content = DISCOVERY_RESPONSE
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="1" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = InventoryProvider()
    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("chunk-0", "A site contains an inverter.")],
            provider=provider,
            source_hash="e" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
        )
    )

    assert len(provider.messages) == 2
    assert draft.concept_inventory[0].proposed_entity_type == "generation_site"
    synthesis_payload = json.loads(provider.messages[1][1]["content"])
    assert synthesis_payload["concept_inventory"][0]["evidence_refs"] == ["chunk-0"]
    assert draft.trace["concept_inventory"][0]["request_payload_hash"] == "1" * 64


def test_large_discovery_sample_spans_the_ordered_corpus():
    texts = [DiscoveryText(f"chunk-{index}", str(index)) for index in range(100)]

    sampled = _sample_discovery_texts(texts, max_texts=8)

    assert [item.key for item in sampled] == [
        "chunk-0",
        "chunk-12",
        "chunk-25",
        "chunk-37",
        "chunk-50",
        "chunk-62",
        "chunk-75",
        "chunk-87",
    ]


def test_discovery_can_skip_optional_concept_inventory():
    class InventoryProvider(DeterministicDiscoveryProvider):
        supports_concept_inventory = True

    provider = InventoryProvider()

    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("chunk-0", "A site contains an inverter.")],
            provider=provider,
            source_hash="s" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
            concept_inventory_enabled=False,
        )
    )

    assert len(provider.messages) == 1
    assert "concept inventory protocol" not in provider.messages[0][0]["content"]
    assert draft.concept_inventory == ()


def test_unbounded_discovery_uses_wide_inventory_output_budget():
    class UnboundedInventoryProvider(DeterministicDiscoveryProvider):
        supports_concept_inventory = True

        def __init__(self) -> None:
            super().__init__()
            self.budgets: list[int] = []

        def with_output_budget(self, max_output_tokens: int):
            self.budgets.append(max_output_tokens)
            return self

        async def extract(self, messages):
            self.messages.append(messages)
            content = (
                {
                    "concepts": [
                        {
                            "label": "Site",
                            "proposed_entity_type": "generation_site",
                            "aliases": [],
                            "identifiers": [],
                            "proposed_relations": [],
                            "evidence_refs": ["chunk-0"],
                            "confidence": 0.9,
                            "ambiguity": [],
                        }
                    ]
                }
                if "concept inventory protocol" in messages[0]["content"]
                else DISCOVERY_RESPONSE
            )
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="3" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = UnboundedInventoryProvider()
    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("chunk-0", "A site contains an inverter.")],
            provider=provider,
            source_hash="u" * 64,
            context_window_tokens=4096,
            unbounded_output=True,
        )
    )

    assert draft.concept_inventory[0].proposed_entity_type == "generation_site"
    assert len(provider.messages) >= 2
    assert provider.budgets[0] >= 2_048


def test_inventory_protocol_error_retries_once_with_complete_payload():
    class RepairingInventoryProvider(DeterministicDiscoveryProvider):
        supports_concept_inventory = True

        async def extract(self, messages):
            self.messages.append(messages)
            if "concept inventory protocol" in messages[0]["content"]:
                if len(self.messages) == 1:
                    content = {"concepts": [{"label": "missing protocol fields"}]}
                else:
                    content = {
                        "concepts": [
                            {
                                "label": "Site",
                                "proposed_entity_type": "generation_site",
                                "aliases": [],
                                "identifiers": [],
                                "proposed_relations": [],
                                "evidence_refs": ["chunk-0"],
                                "confidence": 0.9,
                                "ambiguity": [],
                            }
                        ]
                    }
            else:
                content = DISCOVERY_RESPONSE
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash=str(len(self.messages)).zfill(64),
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = RepairingInventoryProvider()
    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("chunk-0", "A site contains an inverter.")],
            provider=provider,
            source_hash="r" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
        )
    )

    assert draft.concept_inventory[0].label == "Site"
    assert len(provider.messages) == 3
    repair_payload = json.loads(provider.messages[1][1]["content"])
    assert repair_payload["repair_instruction"]
    assert "previous_schema" not in repair_payload
    assert len(draft.trace["concept_inventory"]) == 2


def test_inventory_protocol_error_fails_after_one_repair_attempt():
    class AlwaysInvalidInventoryProvider(DeterministicDiscoveryProvider):
        supports_concept_inventory = True

        async def extract(self, messages):
            self.messages.append(messages)
            content = (
                {"concepts": [{"label": "missing protocol fields"}]}
                if "concept inventory protocol" in messages[0]["content"]
                else DISCOVERY_RESPONSE
            )
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="4" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = AlwaysInvalidInventoryProvider()
    with pytest.raises(ValueError, match="concept inventory response is not valid"):
        asyncio.run(
            discover_business_schema(
                [DiscoveryText("chunk-0", "A site contains an inverter.")],
                provider=provider,
                source_hash="i" * 64,
                context_window_tokens=4096,
                max_output_tokens=1024,
            )
        )
    assert len(provider.messages) == 2


def test_concept_inventory_merges_evidence_without_merging_distinct_types():
    first = ConceptInventoryItem(
        label="G-01",
        proposed_entity_type="monitoring_gateway",
        aliases=["Gateway G-01"],
        identifiers=["G-01"],
        proposed_relations=[
            ConceptInventoryRelationHint(
                label="connects",
                source_hint="monitoring_gateway",
                target_hint="platform",
                direction_hint="gateway to platform",
                evidence_refs=["doc-a"],
            )
        ],
        evidence_refs=["doc-a"],
        confidence=0.8,
        ambiguity=[],
    )
    second = first.model_copy(
        update={
            "aliases": ["监控网关G-01"],
            "evidence_refs": ["doc-b"],
            "confidence": 0.9,
            "proposed_relations": [
                first.proposed_relations[0].model_copy(update={"evidence_refs": ["doc-b"]})
            ],
        }
    )
    distinct = first.model_copy(update={"proposed_entity_type": "asset_identifier"})

    merged = _merge_concept_inventory([first, second, distinct])

    assert len(merged) == 2
    gateway = next(item for item in merged if item.proposed_entity_type == "monitoring_gateway")
    assert gateway.aliases == ["Gateway G-01", "监控网关G-01"]
    assert gateway.evidence_refs == ["doc-a", "doc-b"]
    assert gateway.confidence == 0.9
    assert gateway.proposed_relations[0].evidence_refs == ["doc-a", "doc-b"]


@pytest.mark.parametrize(
    ("fixture_text", "entity_key", "relation_key"),
    [
        ("asset disposal case names a seized facility and a valuation report.", "seized_facility", "valued_in"),
        ("medical record names a patient and a treatment plan.", "patient", "receives_plan"),
        ("ordinary project notes name a workshop and a delivery milestone.", "workshop", "has_milestone"),
    ],
)
def test_independent_domain_fixtures_drive_distinct_schema_keys(
    fixture_text, entity_key, relation_key
):
    responses = {
        "seized_facility": {
            "entity_types": [
                {"key": "seized_facility", "label": "Seized facility", "description": "A facility named in the case."},
                {"key": "valuation_report", "label": "Valuation report", "description": "A report describing a valuation."},
            ],
            "relation_types": [
                {"key": "valued_in", "label": "Valued in", "description": "A facility is described by a valuation report.", "direction": "directed"}
            ],
            "constraints": [
                {"source_type_key": "seized_facility", "relation_type_key": "valued_in", "target_type_key": "valuation_report", "cardinality": "many_to_one"}
            ],
        },
        "patient": {
            "entity_types": [
                {"key": "patient", "label": "Patient", "description": "A person named in the record."},
                {"key": "treatment_plan", "label": "Treatment plan", "description": "A plan named in the record."},
            ],
            "relation_types": [
                {"key": "receives_plan", "label": "Receives plan", "description": "A patient receives a treatment plan.", "direction": "directed"}
            ],
            "constraints": [
                {"source_type_key": "patient", "relation_type_key": "receives_plan", "target_type_key": "treatment_plan", "cardinality": "one_to_many"}
            ],
        },
        "workshop": {
            "entity_types": [
                {"key": "workshop", "label": "Workshop", "description": "A workshop named in the notes."},
                {"key": "delivery_milestone", "label": "Delivery milestone", "description": "A milestone named in the notes."},
            ],
            "relation_types": [
                {"key": "has_milestone", "label": "Has milestone", "description": "A workshop has a delivery milestone.", "direction": "directed"}
            ],
            "constraints": [
                {"source_type_key": "workshop", "relation_type_key": "has_milestone", "target_type_key": "delivery_milestone", "cardinality": "one_to_many"}
            ],
        },
    }

    class FixtureProvider:
        async def extract(self, messages):
            payload = json.loads(messages[1]["content"])
            text = payload["excerpts"][0]["text"]
            selected = next(
                key for key in responses if key.split("_", 1)[0] in text
            )
            return ProviderResponse(
                content=json.dumps(responses[selected]),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="2" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("fixture", fixture_text)],
            provider=FixtureProvider(),
            source_hash="f" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
        )
    )

    assert {row.key for row in draft.entity_types} == {entity_key, next(
        row.key for row in draft.entity_types if row.key != entity_key
    )}
    assert {row.key for row in draft.relation_types} == {relation_key}
    assert draft.confirmed is False


def test_discovery_batches_include_prompt_schema_and_output_in_budget():
    batches = plan_schema_discovery_batches(
        [DiscoveryText("a", "x" * 3000), DiscoveryText("b", "y" * 3000)],
        context_window_tokens=1024,
        max_output_tokens=128,
    )

    assert len(batches) > 1
    assert all(batch.estimated_total_tokens <= 1024 for batch in batches)

    messages = build_schema_discovery_messages(batches[0])
    assert "protocol_schema" in messages[1]["content"]
    for rule in (
        "labels and descriptions in Simplified Chinese",
        "source_type_key and target_type_key must reference",
        "relation_type_key must reference",
        "string_literal, text, or",
        "direction must be directed or undirected",
        "cardinality must be one_to_one",
        "most specific stable concept",
        "contextual qualifier",
        "Different relation mentions alone",
        "create an inverse relation",
    ):
        assert rule in messages[0]["content"]


def test_discovery_repairs_unknown_references_once_with_complete_schema_request():
    invalid = {
        **DISCOVERY_RESPONSE,
        "constraints": [
            {
                **DISCOVERY_RESPONSE["constraints"][0],
                "target_type_key": "undeclared_scalar",
            }
        ],
    }

    class RepairProvider:
        def __init__(self) -> None:
            self.messages = []

        async def extract(self, messages):
            self.messages.append(messages)
            content = invalid if len(self.messages) == 1 else DISCOVERY_RESPONSE
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = RepairProvider()
    draft = asyncio.run(
        discover_business_schema(
            [DiscoveryText("d1", "A site contains an inverter.")],
            provider=provider,
            source_hash="c" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
        )
    )

    assert len(provider.messages) == 2
    repair_payload = json.loads(provider.messages[1][1]["content"])
    assert repair_payload["previous_schema"] == invalid
    assert repair_payload["validation_errors"] == [
        "AI Schema discovery returned a constraint with an unknown reference"
    ]
    assert "complete corrected Schema" in repair_payload["repair_instruction"]
    assert "do not return a patch" in repair_payload["repair_instruction"]
    assert len(draft.constraints) == 1


def test_discovery_repair_reuses_source_excerpts_after_empty_schema():
    texts = [
        DiscoveryText("d1", "A site contains an inverter."),
        DiscoveryText("d2", "The inverter reports to the monitoring platform."),
    ]

    class RepairProvider:
        def __init__(self) -> None:
            self.messages = []

        async def extract(self, messages):
            self.messages.append(messages)
            payload = json.loads(messages[1]["content"])
            content = (
                {}
                if len(self.messages) == 1
                else (
                    DISCOVERY_RESPONSE
                    if payload.get("excerpts")
                    == [{"key": item.key, "text": item.text} for item in texts]
                    else {}
                )
            )
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = RepairProvider()
    draft = asyncio.run(
        discover_business_schema(
            texts,
            provider=provider,
            source_hash="e" * 64,
            context_window_tokens=4096,
            max_output_tokens=1024,
        )
    )

    assert len(provider.messages) == 2
    repair_payload = json.loads(provider.messages[1][1]["content"])
    assert repair_payload["excerpts"] == [
        {"key": item.key, "text": item.text} for item in texts
    ]
    assert len(draft.constraints) == 1


def test_discovery_repair_error_keeps_empty_schema_reason():
    class EmptySchemaProvider:
        async def extract(self, _messages):
            return ProviderResponse(
                content="{}",
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    with pytest.raises(
        ValueError,
        match="schema discovery repair response failed validation: AI Schema discovery returned an empty Schema",
    ):
        asyncio.run(
            discover_business_schema(
                [DiscoveryText("d1", "A site contains an inverter.")],
                provider=EmptySchemaProvider(),
                source_hash="e" * 64,
                context_window_tokens=4096,
                max_output_tokens=1024,
            )
        )


def test_repair_budget_accounts_for_complete_previous_schema():
    invalid = {
        **DISCOVERY_RESPONSE,
        "constraints": [
            {
                **DISCOVERY_RESPONSE["constraints"][0],
                "target_type_key": "undeclared_scalar",
            }
        ],
    }
    valid = DISCOVERY_RESPONSE

    class BudgetProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.budgets: list[int] = []

        def with_output_budget(self, max_output_tokens: int):
            self.budgets.append(max_output_tokens)
            return self

        async def extract(self, _messages):
            self.calls += 1
            content = invalid if self.calls == 1 else valid
            return ProviderResponse(
                content=json.dumps(content),
                provider_request_id=None,
                raw_response="{}",
                request_payload_hash="0" * 64,
                input_token_count=None,
                output_token_count=None,
                latency_ms=0,
                finish_reason="stop",
            )

    provider = BudgetProvider()
    asyncio.run(
        discover_business_schema(
            [DiscoveryText("d1", "A site contains an inverter.")],
            provider=provider,
            source_hash="c" * 64,
            context_window_tokens=8192,
            max_output_tokens=8000,
        )
    )

    assert provider.calls == 3
    assert provider.budgets[1] > 128
    assert provider.budgets[1] >= len(json.dumps(valid)) // 4
    assert provider.budgets[2] > 128


def test_extraction_batch_planner_uses_serialized_request_budget():
    snapshot = {
        "entity_types": [{"id": "e1", "key": "solar_farm", "properties_schema": {}}],
        "relation_types": [],
        "relation_constraints": [],
    }
    inputs = tuple(GraphExtractionBatchInput(str(index), "光伏电站" * 100) for index in range(4))
    batches = plan_graph_extraction_batches(
        ontology_snapshot=snapshot,
        inputs=inputs,
        schema_routing_enabled=False,
        context_window_tokens=1024,
        max_output_tokens=128,
    )

    assert len(batches) > 1
    for batch in batches:
        messages = build_batched_graph_extraction_messages(
            ontology_snapshot=snapshot,
            batches=batch,
            schema_routing_enabled=False,
        )
        assert estimate_graph_extraction_request_tokens(messages, max_output_tokens=128) <= 1024


def test_merge_deduplicates_semantically_equal_schema_and_snapshot_freezes_draft():
    from app.services.graph_schema_discovery import SchemaDiscoveryPayload

    first = SchemaDiscoveryPayload.model_validate(
        {
            **DISCOVERY_RESPONSE,
            "entity_types": [
                {
                    **DISCOVERY_RESPONSE["entity_types"][0],
                    "attributes": [
                        {"key": "zeta", "description": "Z", "value_type": "string"},
                        {"key": "alpha", "description": "A", "value_type": "string"},
                    ],
                },
                DISCOVERY_RESPONSE["entity_types"][1],
            ],
        }
    )
    second = SchemaDiscoveryPayload.model_validate(
        {**DISCOVERY_RESPONSE, "entity_types": [DISCOVERY_RESPONSE["entity_types"][0]]}
    )
    draft = merge_schema_discovery_payloads([first, second], source_hash="b" * 64)
    snapshot = schema_draft_to_snapshot(
        draft,
        ontology_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    )

    assert len(draft.entity_types) == 2
    assert snapshot["schema_state"] == "ai_draft"
    assert snapshot["confirmed"] is False
    solar_farm = next(row for row in snapshot["entity_types"] if row["key"] == "solar_farm")
    assert [row["key"] for row in solar_farm["active_attribute_definitions"]] == [
        "alpha",
        "capacity_mw",
        "zeta",
    ]
    assert len(snapshot["relation_constraints"]) == 1
    assert snapshot["relation_constraints"][0]["cardinality"] == "one_to_many"


def test_schema_snapshot_sorts_relation_constraints_by_ids():
    from app.services.graph_schema_discovery import (
        BusinessSchemaDraft,
        SchemaDiscoveryConstraint,
        SchemaDiscoveryEntityType,
        SchemaDiscoveryRelationType,
    )

    draft = BusinessSchemaDraft(
        source_hash="b" * 64,
        entity_types=(
            SchemaDiscoveryEntityType(key="alpha", label="Alpha", description="A"),
            SchemaDiscoveryEntityType(key="beta", label="Beta", description="B"),
        ),
        relation_types=(
            SchemaDiscoveryRelationType(key="owns", label="Owns", description="Owns"),
            SchemaDiscoveryRelationType(key="uses", label="Uses", description="Uses"),
        ),
        constraints=(
            SchemaDiscoveryConstraint(
                source_type_key="beta",
                relation_type_key="uses",
                target_type_key="alpha",
            ),
            SchemaDiscoveryConstraint(
                source_type_key="alpha",
                relation_type_key="owns",
                target_type_key="beta",
            ),
        ),
    )

    constraints = schema_draft_to_snapshot(
        draft,
        ontology_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    )["relation_constraints"]

    assert constraints == sorted(
        constraints,
        key=lambda row: (
            row["relation_type_id"],
            row["source_entity_type_id"],
            row["target_entity_type_id"],
        ),
    )


def test_one_discovery_run_binds_six_waiting_jobs_to_one_snapshot():
    from app.models.library import Library
    from app.models.ontology_version import OntologyVersion
    from app.models.schema_discovery_run import SchemaDiscoveryRun
    from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
    from app.services.schema_discovery_runs import process_next_schema_discovery_run

    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=ontology_id,
        source_hash="a" * 64,
        source_revision_ids=[str(uuid.uuid4()) for _ in range(6)],
        status="queued",
        ontology_snapshot=None,
        ontology_snapshot_hash=None,
    )
    jobs = [
        SimpleNamespace(
            schema_discovery_run_id=run.id,
            ontology_version_id=ontology_id,
            ontology_snapshot=None,
            ontology_snapshot_hash=None,
            model_config_snapshot={"schema_discovery": "pending"},
            model_config_hash=canonical_graph_value_hash_v1({"schema_discovery": "pending"}),
            status="waiting_schema",
            current_stage="waiting_schema",
        )
        for _ in range(6)
    ]
    library = SimpleNamespace(id=library_id)
    ontology = SimpleNamespace(id=ontology_id, status="draft")
    snapshot = {
        "ontology_version_id": str(ontology_id),
        "schema_state": "ai_draft",
        "confirmed": False,
        "entity_types": [{"id": "entity-1", "key": "sensor"}],
        "relation_types": [{"id": "relation-1", "key": "reports_to", "direction": "directed"}],
        "relation_constraints": [],
    }

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def all(self):
            return list(self.rows)

    class Session:
        def __init__(self, first=False):
            self.first = first

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        async def execute(self, _statement):
            return Result([run] if self.first else jobs)

        async def get(self, model, _id, **_kwargs):
            if model is SchemaDiscoveryRun:
                return run
            if model is OntologyVersion:
                return ontology
            if model is Library:
                return library
            return None

    sessions = iter((Session(first=True), Session(first=False)))

    # async_sessionmaker is callable, not awaitable; mirror that contract.
    def make_session():
        return next(sessions)

    provider = object()
    fake_draft = SimpleNamespace(entity_types=[object()])
    with (
        patch("app.services.schema_discovery_runs._current_run_texts", new=AsyncMock(return_value=(
            [DiscoveryText("c0", "王芳向周强汇报")],
            run.source_hash,
        ))),
        patch("app.services.schema_discovery_runs.discover_business_schema", new=AsyncMock(return_value=fake_draft)) as discover,
        patch("app.services.schema_discovery_runs.persist_business_schema_draft", new=AsyncMock(return_value=snapshot)),
        patch(
            "app.services.graph_extraction_provider.OpenAICompatibleGraphExtractor",
            return_value=provider,
        ) as provider_cls,
    ):
        result = asyncio.run(
            process_next_schema_discovery_run(
                session_factory=make_session,
            )
        )

    assert result is run
    discover.assert_awaited_once()
    assert provider_cls.call_args.kwargs["timeout_seconds"] == 300.0
    assert provider_cls.call_args.kwargs["max_output_tokens"] == 8000
    assert discover.await_args.kwargs["context_window_tokens"] == 16384
    assert discover.await_args.kwargs["max_output_tokens"] == 8000
    assert run.status == "waiting_confirmation"
    assert len(jobs) == 6
    assert {job.ontology_version_id for job in jobs} == {ontology_id}
    assert {job.ontology_snapshot_hash for job in jobs} == {run.ontology_snapshot_hash}
    assert all(job.ontology_snapshot == snapshot for job in jobs)
    assert all(
        job.model_config_hash == canonical_graph_value_hash_v1(job.model_config_snapshot)
        for job in jobs
    )
    assert all(
        job.status == "waiting_schema" and job.current_stage == "waiting_schema"
        for job in jobs
    )


def test_repair_discovery_rejects_a_non_failed_run():
    from app.models.schema_discovery_run import SchemaDiscoveryRun
    from app.services.schema_discovery_runs import repair_schema_discovery_run

    run = SimpleNamespace(library_id=uuid.uuid4(), status="queued")
    library = SimpleNamespace(id=run.library_id)
    db = SimpleNamespace(get=AsyncMock(return_value=run))

    with pytest.raises(ValueError, match="schema_discovery_run_not_retryable"):
        asyncio.run(
            repair_schema_discovery_run(
                db,
                library=library,
                run_id=uuid.uuid4(),
            )
        )

    db.get.assert_awaited_once_with(SchemaDiscoveryRun, db.get.call_args.args[1], with_for_update=True)


def test_confirmation_resumes_all_jobs_with_the_activated_snapshot():
    from app.services.schema_discovery_runs import (
        resume_schema_discovery_run_after_confirmation,
    )

    ontology_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ontology_version_id=ontology_id,
        status="waiting_confirmation",
        ontology_snapshot=None,
        ontology_snapshot_hash=None,
        finished_at=None,
    )
    jobs = [
        SimpleNamespace(
            status="waiting_schema",
            current_stage="waiting_schema",
            ontology_version_id=uuid.uuid4(),
            ontology_snapshot=None,
            ontology_snapshot_hash=None,
            model_config_snapshot={"schema_discovery": "pending"},
            model_config_hash=None,
        )
        for _ in range(3)
    ]
    snapshot = {
        "ontology_version_id": str(ontology_id),
        "entity_types": [{"key": "patient"}],
        "relation_types": [],
        "relation_constraints": [],
    }

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self.rows)

    class Session:
        def __init__(self):
            self.results = iter((Result([run]), Result(jobs)))

        async def execute(self, _statement):
            return next(self.results)

    library = SimpleNamespace(id=uuid.uuid4())
    with patch(
        "app.services.graph_extraction_jobs.build_ontology_rule_snapshot",
        new=AsyncMock(return_value=(snapshot, "unused")),
    ) as build_snapshot:
        resumed = asyncio.run(
            resume_schema_discovery_run_after_confirmation(
                Session(),
                library=library,
                ontology_version_id=ontology_id,
            )
        )

    assert resumed == 3
    assert run.status == "succeeded"
    assert run.ontology_snapshot["confirmed"] is True
    assert run.ontology_snapshot_hash
    assert {job.ontology_version_id for job in jobs} == {ontology_id}
    assert {job.ontology_snapshot_hash for job in jobs} == {
        run.ontology_snapshot_hash
    }
    assert all(job.status == "queued" for job in jobs)
    assert all(job.current_stage == "preparing" for job in jobs)
    build_snapshot.assert_awaited_once()


def test_automatic_discovery_activates_the_exact_draft_and_queues_jobs():
    from app.models.library import Library
    from app.models.ontology_version import OntologyVersion
    from app.models.schema_discovery_run import SchemaDiscoveryRun
    from app.services.schema_discovery_runs import process_next_schema_discovery_run

    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=ontology_id,
        source_hash="a" * 64,
        source_revision_ids=[str(uuid.uuid4())],
        status="queued",
        confirmation_policy="automatic",
        ontology_snapshot=None,
        ontology_snapshot_hash=None,
        concept_inventory=[],
        discovery_trace={},
        finished_at=None,
    )
    ontology = SimpleNamespace(id=ontology_id, status="draft")
    library = SimpleNamespace(
        id=library_id,
        schema_confirmation_policy="automatic",
        created_by=None,
    )
    job = SimpleNamespace(
        status="waiting_schema",
        current_stage="waiting_schema",
        ontology_version_id=ontology_id,
        ontology_snapshot=None,
        ontology_snapshot_hash=None,
        model_config_snapshot={"schema_discovery": "pending"},
        model_config_hash=None,
    )
    draft = SimpleNamespace(entity_types=[object()], concept_inventory=(), trace={})
    snapshot = {
        "ontology_version_id": str(ontology_id),
        "schema_state": "ai_draft",
        "confirmed": False,
        "entity_types": [{"id": "entity-1", "key": "patient"}],
        "relation_types": [],
        "relation_constraints": [],
    }
    confirmed_snapshot = dict(snapshot, schema_state="confirmed", confirmed=True)

    class Result:
        def __init__(self, rows=(), scalar=None):
            self.rows = list(rows)
            self.scalar = scalar

        def scalars(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def all(self):
            return list(self.rows)

        def scalar_one_or_none(self):
            return self.scalar

    class Session:
        def __init__(self, first=False):
            self.first = first
            self.results = iter(
                (Result([run]),)
                if first
                else (Result([job]), Result(scalar=None))
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        async def execute(self, _statement):
            return next(self.results)

        async def get(self, model, _id, **_kwargs):
            if model is SchemaDiscoveryRun:
                return run
            if model is OntologyVersion:
                return ontology
            if model is Library:
                return library
            return None

    sessions = iter((Session(first=True), Session(first=False)))

    def make_session():
        return next(sessions)

    with (
        patch(
            "app.services.schema_discovery_runs._current_run_texts",
            new=AsyncMock(
                return_value=([DiscoveryText("c0", "A patient receives care.")], run.source_hash)
            ),
        ),
        patch(
            "app.services.schema_discovery_runs.discover_business_schema",
            new=AsyncMock(return_value=draft),
        ),
        patch(
            "app.services.schema_discovery_runs.persist_business_schema_draft",
            new=AsyncMock(return_value=snapshot),
        ),
        patch(
            "app.services.schema_lifecycle_actions.activate_schema_version",
            new=AsyncMock(),
        ) as activate,
        patch(
            "app.services.graph_extraction_jobs.build_ontology_rule_snapshot",
            new=AsyncMock(return_value=(confirmed_snapshot, "unused")),
        ),
        patch(
            "app.services.schema_lifecycle_read.load_schema_version_bundle",
            new=AsyncMock(return_value=SimpleNamespace(version=ontology)),
        ),
        patch(
            "app.services.schema_lifecycle_read.schema_version_state_hash",
            return_value="a" * 64,
        ),
    ):
        result = asyncio.run(
            process_next_schema_discovery_run(
                session_factory=make_session,
                provider=object(),
            )
        )

    assert result is run
    assert run.status == "succeeded"
    assert run.ontology_snapshot["confirmed"] is True
    assert job.status == "queued"
    assert job.current_stage == "preparing"
    activate.assert_awaited_once()
    command = activate.await_args.args[2]
    assert command.target_id == ontology_id


def test_failed_discovery_cancels_only_queued_graph_units():
    from app.models.schema_discovery_run import SchemaDiscoveryRun
    from app.services.schema_discovery_runs import _fail_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="discovering",
        error_code=None,
        error_message=None,
        finished_at=None,
    )
    jobs = [
        SimpleNamespace(
            status="waiting_schema",
            current_stage="waiting_schema",
            error_code=None,
            error_message=None,
            finished_at=None,
        )
    ]

    class Result:
        def scalars(self):
            return self

        def all(self):
            return list(jobs)

    class Session:
        def __init__(self):
            self.statements = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        async def get(self, model, run_id, **_kwargs):
            assert model is SchemaDiscoveryRun
            assert run_id == run.id
            return run

        async def execute(self, statement):
            self.statements.append(statement)
            return Result() if len(self.statements) == 1 else SimpleNamespace(rowcount=2)

    session = Session()
    result = asyncio.run(
        _fail_run(
            lambda: session,
            run.id,
            "schema_discovery_failed",
            "provider unavailable",
        )
    )

    assert result is run
    assert run.status == "failed"
    assert jobs[0].status == "failed"
    assert len(session.statements) == 2
    unit_update = session.statements[1]
    statement_params = unit_update.compile().params.values()
    assert "update graph_extraction_units" in str(unit_update).lower()
    assert "queued" in statement_params
    assert "cancelled" in statement_params
    assert "processing" not in statement_params


def test_chinese_token_estimate_does_not_use_three_char_underestimate():
    text = "中文" * 2048

    assert estimate_text_tokens(text, model_name="unavailable-qwen") >= len(text)
