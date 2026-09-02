from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.evidence_locator import SourceLocatorV1, TextSpanV1, sha256_text
from app.schemas.raw_claim import EvidenceLocatorClaimRefV1
from app.schemas.shadow_extraction import (
    ShadowEvidenceContextV1,
    ShadowExtractionLimitsV1,
    ShadowProviderResponseV1,
)
from app.schemas.shadow_raw_response import ShadowExtractionConfigV1, ShadowExtractionProvenanceV1
from app.services.shadow_extraction_provider import (
    ShadowProviderCallError,
    ShadowProviderRunError,
    ShadowResponseParseError,
    build_shadow_extraction_request,
    parse_shadow_response,
    run_shadow_extraction,
)


DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def _provenance() -> ShadowExtractionProvenanceV1:
    return ShadowExtractionProvenanceV1(
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        revision_no=1,
        job_id=JOB_ID,
        extraction_unit_id=UNIT_ID,
        extractor_version="shadow-v1",
        prompt_version="shadow-prompt-v1",
        model_provider="fixture-provider",
        model_name="fixture-model",
        model_config_hash=HASH,
        prompt_content_hash=HASH,
        parser_version="shadow-parser-v1",
        normalization_rule_version="shadow-normalization-v1",
        ontology_snapshot_hash=HASH,
    )


def _context(ref_key: str = "e1") -> ShadowEvidenceContextV1:
    quote = "source evidence"
    locator = EvidenceLocatorClaimRefV1(
        locator_version="v1",
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        revision_no=1,
        unit_id=UNIT_ID,
        unit_kind="chunk",
        ordinal=0,
        quote_sha256=sha256_text(quote),
        unit_text_sha256=sha256_text(quote),
        source=SourceLocatorV1(
            kind="text",
            file_name="C:/secret/storage/source.txt",
            text=TextSpanV1(start=0, end=len(quote)),
        ),
    )
    return ShadowEvidenceContextV1(ref_key=ref_key, locator=locator)


def _claim(*, predicate: str = "supports", direction: str = "source_to_target", evidence: str = "e1") -> dict:
    return {
        "source_mention": {
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": "source_type",
            "evidence_ref": evidence,
        },
        "surface_raw_predicate": predicate,
        "target_mention": {
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": evidence,
        },
        "surface_direction": direction,
        "negation": {"value": False, "evidence_ref": evidence},
        "modality": {"value": "asserted", "evidence_ref": evidence},
        "qualifiers": [],
        "valid_time": None,
        "effective_time": None,
        "evidence_ref_keys": [evidence],
    }


def _anchored(claim: dict, *, projection_ref: str | None = None) -> dict:
    return {"projection_ref": projection_ref, "claim": claim}


def _response(*claims: dict, finish_reason: str = "stop") -> ShadowProviderResponseV1:
    return ShadowProviderResponseV1(
        content=json.dumps(
            {"claims": [_anchored(claim) for claim in claims]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        finish_reason=finish_reason,
        input_token_count=101,
        output_token_count=23,
        latency_ms=4,
    )


def _request(*, limits: ShadowExtractionLimitsV1 | None = None, text: str = "Source supports target."):
    return build_shadow_extraction_request(
        unit_text=text,
        evidence_contexts=[_context()],
        provenance=_provenance(),
        limits=limits,
    )


@pytest.mark.asyncio
async def test_flag_off_skips_provider_without_calling_it():
    calls = 0

    async def provider(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be called")

    result = await run_shadow_extraction(
        _request(), provider, config=ShadowExtractionConfigV1(global_enabled=False)
    )

    assert result.status == "skipped"
    assert result.claims == ()
    assert result.telemetry.finish_reason == "skipped"
    assert calls == 0


@pytest.mark.asyncio
async def test_valid_multiple_claims_preserve_surface_fields_and_telemetry_is_hash_only():
    async def provider(_request):
        return _response(
            _claim(predicate="is alleged to support", direction="target_to_source"),
            _claim(predicate="unclassified surface predicate", direction="unknown"),
        )

    result = await run_shadow_extraction(
        _request(), provider, config=ShadowExtractionConfigV1(global_enabled=True)
    )

    assert result.status == "success"
    assert [claim.claim.surface_raw_predicate for claim in result.claims] == [
        "is alleged to support",
        "unclassified surface predicate",
    ]
    assert result.claims[1].claim.surface_direction == "unknown"
    telemetry = result.telemetry.model_dump(mode="json")
    assert len(telemetry["request_hash"]) == 64
    assert len(telemetry["response_hash"]) == 64
    assert "Source supports target." not in json.dumps(telemetry)
    assert "source.txt" not in json.dumps(telemetry)
    assert HASH not in json.dumps(telemetry)


def test_request_is_deterministic_and_prompt_contains_no_canonical_key_or_sensitive_locator():
    first = _request()
    second = _request()

    assert first.request_hash == second.request_hash
    prompt = json.dumps(first.messages)
    assert "relation_type_key" not in prompt
    assert "C:/secret/storage/source.txt" not in prompt
    assert HASH not in prompt
    assert "e1" in prompt
    assert '"source_mention":{"local_id"' in first.messages[0]["content"]
    assert '"negation":{"value":false' in first.messages[0]["content"]


def test_request_bounds_and_duplicate_context_keys_are_enforced():
    with pytest.raises(ValueError, match="unique"):
        build_shadow_extraction_request(
            unit_text="text",
            evidence_contexts=[_context(), _context()],
            provenance=_provenance(),
        )
    with pytest.raises(ValueError, match="token budget"):
        _request(limits=ShadowExtractionLimitsV1(max_request_tokens=256))
    with pytest.raises(ValueError, match="byte bound|token budget"):
        _request(text="x" * 48_000, limits=ShadowExtractionLimitsV1(max_request_bytes=4096))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "```json\n{\"claims\": []}\n```",
        '{"claims": []} trailing prose',
        '{"claims": [], "relation_type_key": "supports"}',
    ],
)
def test_parser_rejects_non_json_fence_trailing_prose_and_extra_canonical_fields(content):
    with pytest.raises(ShadowResponseParseError, match="JSON|fences|exact|extra"):
        parse_shadow_response(content, allowed_evidence_ref_keys=["e1"])


def test_parser_rejects_canonical_key_inside_claim_and_never_uses_it_as_predicate():
    claim = _claim(predicate="surface wording")
    claim["relation_type_key"] = "supports"
    with pytest.raises(ShadowResponseParseError, match="validation"):
        parse_shadow_response(
            json.dumps({"claims": [_anchored(claim)]}), allowed_evidence_ref_keys=["e1"]
        )


def test_parser_rejects_unknown_evidence_key_and_preserves_unknown_predicate():
    with pytest.raises(ShadowResponseParseError, match="undeclared"):
        parse_shadow_response(
            json.dumps({"claims": [_anchored(_claim(evidence="e2", predicate="surface only"))]}),
            allowed_evidence_ref_keys=["e1"],
        )
    parsed = parse_shadow_response(
        json.dumps(
            {"claims": [_anchored(_claim(predicate="never canonicalized", direction="unknown"))]}
        ),
        allowed_evidence_ref_keys=["e1"],
    )
    assert parsed[0].claim.surface_raw_predicate == "never canonicalized"
    assert parsed[0].claim.surface_direction == "unknown"


@pytest.mark.parametrize("content", ["{\"claims\": [", json.dumps({"claims": []}) + " prose"])
def test_parser_rejects_malformed_or_trailing_response(content):
    with pytest.raises(ShadowResponseParseError):
        parse_shadow_response(content, allowed_evidence_ref_keys=["e1"])


@pytest.mark.parametrize(
    ("case", "category"),
    [
        ("not_json", "json_syntax"),
        ("markdown", "markdown_or_reasoning_wrapper"),
        ("missing", "schema_missing"),
        ("extra", "schema_extra"),
        ("type", "schema_type"),
        ("evidence", "evidence_reference"),
        ("oversize", "bounds"),
    ],
)
def test_parser_exposes_only_stable_parse_category(case, category):
    content = {
        "not_json": "not json",
        "markdown": "```json\n{\"claims\": []}\n```",
        "missing": '{"payload": []}',
        "extra": '{"claims": [], "extra": true}',
        "type": json.dumps(
            {"claims": [_anchored({**_claim(), "surface_direction": 1})]}
        ),
        "evidence": json.dumps({"claims": [_anchored(_claim(evidence="e2"))]}),
        "oversize": "x" * (128 * 1024 + 1),
    }[case]
    with pytest.raises(ShadowResponseParseError) as error:
        parse_shadow_response(content, allowed_evidence_ref_keys=["e1"])

    assert error.value.parse_category == category


def test_parser_exposes_only_bounded_pydantic_paths_types_and_counts():
    claim = _claim()
    claim.update(
        source_mention="Sensitive Asset Name",
        target_mention="Sensitive Legal Party",
        negation="Sensitive Medical Negation",
        modality="Sensitive Medical Modality",
    )

    with pytest.raises(ShadowResponseParseError) as error:
        parse_shadow_response(
            json.dumps({"claims": [_anchored(claim)]}),
            allowed_evidence_ref_keys=["e1"],
        )

    assert error.value.parse_category == "schema_type"
    assert [(issue.path, issue.error_type, issue.count) for issue in error.value.validation_issues] == [
        ("claims.0.claim.modality", "model_type", 1),
        ("claims.0.claim.negation", "model_type", 1),
        ("claims.0.claim.source_mention", "model_type", 1),
        ("claims.0.claim.target_mention", "model_type", 1),
    ]
    serialized = json.dumps(
        [issue.model_dump(mode="json") for issue in error.value.validation_issues]
    )
    assert "Sensitive" not in serialized


def test_parser_rejects_truncation_size_depth_nodes_and_claim_count():
    with pytest.raises(ShadowResponseParseError, match="truncated"):
        parse_shadow_response('{"claims": []}', allowed_evidence_ref_keys=["e1"], finish_reason="length")
    with pytest.raises(ShadowResponseParseError, match="byte"):
        parse_shadow_response("x" * (128 * 1024 + 1), allowed_evidence_ref_keys=["e1"])
    deep: object = "leaf"
    for _ in range(10):
        deep = [deep]
    with pytest.raises(ShadowResponseParseError, match="depth"):
        parse_shadow_response(json.dumps(deep), allowed_evidence_ref_keys=["e1"])
    with pytest.raises(ShadowResponseParseError, match="node"):
        parse_shadow_response(
            json.dumps({"claims": [None] * 2050}), allowed_evidence_ref_keys=["e1"]
        )
    with pytest.raises(ShadowResponseParseError, match="claim count"):
        parse_shadow_response(
            json.dumps({"claims": [_anchored(_claim()) for _ in range(33)]}),
            allowed_evidence_ref_keys=["e1"],
            max_claims=32,
        )


@pytest.mark.asyncio
async def test_runner_maps_truncation_timeout_and_provider_error_without_success():
    async def truncated(_request):
        return _response(_claim(), finish_reason="length")

    with pytest.raises(ShadowProviderRunError) as truncated_error:
        await run_shadow_extraction(
            _request(), truncated, config=ShadowExtractionConfigV1(global_enabled=True)
        )
    assert truncated_error.value.code == "truncated_response"
    assert truncated_error.value.telemetry.error_code == "truncated_response"

    async def timeout(_request):
        raise TimeoutError("provider timed out")

    with pytest.raises(ShadowProviderRunError) as timeout_error:
        await run_shadow_extraction(
            _request(), timeout, config=ShadowExtractionConfigV1(global_enabled=True)
        )
    assert timeout_error.value.code == "timeout"

    async def failure(_request):
        raise RuntimeError("secret provider details")

    with pytest.raises(ShadowProviderRunError) as provider_error:
        await run_shadow_extraction(
            _request(), failure, config=ShadowExtractionConfigV1(global_enabled=True)
        )
    assert provider_error.value.code == "provider_error"
    assert "secret provider details" not in str(provider_error.value)


@pytest.mark.asyncio
async def test_runner_classifies_empty_length_envelope_as_truncated_with_metadata():
    async def provider(_request):
        return ShadowProviderResponseV1(
            content="",
            finish_reason="length",
            input_token_count=404,
            output_token_count=2048,
            latency_ms=17_155,
        )

    with pytest.raises(ShadowProviderRunError) as error:
        await run_shadow_extraction(
            _request(),
            provider,
            config=ShadowExtractionConfigV1(global_enabled=True),
        )

    telemetry = error.value.telemetry
    assert error.value.code == "truncated_response"
    assert telemetry.finish_reason == "length"
    assert telemetry.response_hash == sha256_text("")
    assert telemetry.input_token_count == 404
    assert telemetry.output_token_count == 2048
    assert telemetry.parse_category == "bounds"


@pytest.mark.asyncio
async def test_runner_keeps_schema_validation_diagnostics_bounded_and_redacted():
    invalid = _claim()
    invalid["source_mention"] = "Sensitive Asset Name"

    async def provider(_request):
        return ShadowProviderResponseV1(
            content=json.dumps({"claims": [_anchored(invalid)]}),
            finish_reason="stop",
            input_token_count=404,
            output_token_count=971,
            latency_ms=9_170,
        )

    with pytest.raises(ShadowProviderRunError) as error:
        await run_shadow_extraction(
            _request(),
            provider,
            config=ShadowExtractionConfigV1(global_enabled=True),
        )

    telemetry = error.value.telemetry
    assert telemetry.parse_category == "schema_type"
    assert telemetry.validation_error_count == 1
    assert telemetry.validation_issues[0].path == "claims.0.claim.source_mention"
    assert telemetry.validation_issues[0].error_type == "model_type"
    assert "Sensitive Asset Name" not in json.dumps(telemetry.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "status"),
    [
        ("network", None),
        ("http", 503),
        ("auth", 401),
        ("rate_limit", 429),
        ("budget", 413),
        ("config", None),
        ("invalid_envelope", 200),
        ("unknown", None),
    ],
)
async def test_runner_persists_stable_provider_failure_projection(category, status):
    async def failure(_request):
        raise ShadowProviderCallError(category, http_status=status, retry_count=3)

    with pytest.raises(ShadowProviderRunError) as error:
        await run_shadow_extraction(
            _request(), failure, config=ShadowExtractionConfigV1(global_enabled=True)
        )

    telemetry = error.value.telemetry
    assert error.value.code == "provider_error"
    assert telemetry.provider_error_category == category
    assert telemetry.http_status == status
    assert telemetry.retry_count == 3
    assert telemetry.final_outcome == "failed"
    assert "secret" not in json.dumps(telemetry.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_runner_preserves_response_hash_finish_reason_and_retry_count_without_content():
    async def provider(_request):
        return _response(_claim()).model_copy(update={"retry_count": 2})

    result = await run_shadow_extraction(
        _request(), provider, config=ShadowExtractionConfigV1(global_enabled=True)
    )

    telemetry = result.telemetry
    assert telemetry.response_hash == sha256_text(
        _response(_claim()).content
    )
    assert telemetry.finish_reason == "stop"
    assert telemetry.retry_count == 2
    assert telemetry.input_token_count == 101
    assert telemetry.output_token_count == 23
    assert "Source entity" not in json.dumps(telemetry.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_runner_keeps_timeout_separate_and_reports_exhausted_retry_count():
    async def timeout(_request):
        raise TimeoutError("provider timeout")

    with pytest.raises(ShadowProviderRunError) as timeout_error:
        await run_shadow_extraction(
            _request(), timeout, config=ShadowExtractionConfigV1(global_enabled=True)
        )
    assert timeout_error.value.code == "timeout"
    assert timeout_error.value.telemetry.final_outcome == "timeout"
    assert timeout_error.value.telemetry.provider_error_category is None

    async def exhausted(_request):
        raise ShadowProviderCallError("rate_limit", http_status=429, retry_count=4)

    with pytest.raises(ShadowProviderRunError) as exhausted_error:
        await run_shadow_extraction(
            _request(), exhausted, config=ShadowExtractionConfigV1(global_enabled=True)
        )
    assert exhausted_error.value.telemetry.retry_count == 4
    assert exhausted_error.value.telemetry.http_status == 429


def test_response_contract_rejects_invalid_provider_counts():
    with pytest.raises(ValidationError):
        ShadowProviderResponseV1(content="{}", input_token_count=-1)
