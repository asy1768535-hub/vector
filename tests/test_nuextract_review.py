from __future__ import annotations

import json

import pytest

from app.services.graph_extraction_review import (
    build_draft_pool_qwen_review_messages,
    build_minstral_qwen_review_messages,
    build_nuextract_graph_template,
    build_qwen_review_messages,
    parse_nuextract_draft,
    run_nuextract_qwen_review,
)
from app.services.graph_extraction_provider import MockGraphExtractor


def test_qwen_review_keeps_draft_untrusted_and_preserves_source_context():
    messages = build_qwen_review_messages(
        context_text='{"c0":{"text":"Acme owns Project Vector."}}',
        ontology_snapshot={"entity_types": [{"key": "organization"}]},
        nuextract_draft={"entities": [{"name": "Acme"}]},
    )

    assert [item["role"] for item in messages] == ["system", "user"]
    assert "untrusted" in messages[0]["content"].lower()
    assert "not evidence" in messages[0]["content"].lower()
    assert '"confidence":0.9' in messages[0]["content"]
    assert '"external_mapping_hints":[]' in messages[0]["content"]
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["untrusted_context"] == '{"c0":{"text":"Acme owns Project Vector."}}'
    assert payload["untrusted_nuextract_draft"] == {"entities": [{"name": "Acme"}]}


def test_minstral_review_keeps_draft_untrusted_and_preserves_source_context():
    messages = build_minstral_qwen_review_messages(
        context_text='{"c0":{"text":"Acme owns Project Vector."}}',
        ontology_snapshot={"entity_types": [{"key": "organization"}]},
        minstral_draft={"entities": [{"name": "Acme"}]},
    )

    assert "untrusted" in messages[0]["content"].lower()
    assert "not evidence" in messages[0]["content"].lower()
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["untrusted_context"] == '{"c0":{"text":"Acme owns Project Vector."}}'
    assert payload["untrusted_minstral_draft"] == {"entities": [{"name": "Acme"}]}


def test_qwen_review_is_auditing_only_and_cannot_add_missing_facts():
    messages = build_minstral_qwen_review_messages(
        context_text='{"c0":{"text":"李娜加入星河科技。"}}',
        ontology_snapshot={"entity_types": [], "relation_types": []},
        minstral_draft={"entities": [], "relations": []},
    )

    system_prompt = messages[0]["content"]
    assert "Do not add any entity or relation absent from the draft" in system_prompt
    assert system_prompt.rfind("Audit only") > system_prompt.rfind("Extract every")


def test_draft_pool_review_identifies_the_untrusted_draft_provider():
    messages = build_draft_pool_qwen_review_messages(
        context_text='{"c0":{"text":"李娜加入星河科技。"}}',
        ontology_snapshot={"entity_types": [], "relation_types": []},
        draft_provider="qwen3-draft-4b",
        draft={"entities": [], "relations": []},
    )

    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["draft_provider"] == "qwen3-draft-4b"
    assert payload["untrusted_small_model_draft"] == {"entities": [], "relations": []}


@pytest.mark.parametrize("field", ["context_text", "ontology_snapshot", "nuextract_draft"])
def test_qwen_review_rejects_invalid_contract_inputs(field):
    values = {
        "context_text": "context",
        "ontology_snapshot": {},
        "nuextract_draft": {},
    }
    values[field] = None
    with pytest.raises(ValueError):
        build_qwen_review_messages(**values)


def test_nuextract_template_uses_frozen_type_keys():
    template = build_nuextract_graph_template(
        {
            "entity_types": [{"key": "organization"}],
            "relation_types": [{"key": "owns"}],
        }
    )
    assert template["entities"][0]["entity_type_key"] == ["organization"]
    assert template["relations"][0]["relation_type_key"] == ["owns"]


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"entities":[]}',
        '{"entities":[],"relations":[],"untrusted_context":"leak"}',
    ],
)
def test_nuextract_draft_parser_fails_closed(content):
    with pytest.raises(ValueError):
        parse_nuextract_draft(content)


@pytest.mark.asyncio
async def test_review_pipeline_uses_qwen_output_as_only_canonical_payload():
    draft = MockGraphExtractor(
        {
            "entities": [
                {
                    "local_id": "bad",
                    "name": "Draft-only",
                    "entity_type_key": "organization",
                    "aliases": [],
                    "properties": {},
                    "external_mapping_hints": [],
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "c0", "quote": "Draft-only"}],
                }
            ],
            "relations": [],
        }
    )
    draft.extract_context = draft.extract
    review = MockGraphExtractor({"entities": [], "relations": []})

    result = await run_nuextract_qwen_review(
        draft_provider=draft,
        review_provider=review,
        context_text='{"c0":{"text":"No supported fact."}}',
        ontology_snapshot={"entity_types": [], "relation_types": []},
    )

    assert result.payload.entities == []
    assert result.audit["draft"]["parsed_response"]["entities"][0]["name"] == "Draft-only"
    assert result.audit["version"] == "nuextract-review-audit-v1"
