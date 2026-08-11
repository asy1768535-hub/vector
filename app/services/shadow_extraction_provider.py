"""DB-free shadow request construction, strict parsing, and async execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from app.schemas.shadow_extraction import (
    ParseCategoryV1,
    ProviderErrorCategoryV1,
    ShadowEvidenceContextV1,
    ShadowExtractionLimitsV1,
    ShadowExtractionRequestV1,
    ShadowExtractionResultV1,
    ShadowProviderResponseV1,
    ShadowTelemetryV1,
    ShadowValidationIssueV1,
    canonical_shadow_request_hash,
    safe_locator_summary,
)
from app.schemas.shadow_raw_response import (
    ShadowExtractionConfigV1,
    ShadowExtractionProvenanceV1,
    ShadowRawResponseV1,
    resolve_shadow_extraction,
)
from app.services.token_budget import estimate_chat_request_tokens


SHADOW_PROMPT_VERSION = "shadow-raw-claim-v2"
_MAX_RESPONSE_BYTES = 128 * 1024
_MAX_RESPONSE_DEPTH = 8
_MAX_RESPONSE_NODES = 2048

ShadowRunErrorCode = Literal[
    "timeout",
    "provider_error",
    "malformed_response",
    "truncated_response",
]


class ShadowResponseParseError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: Literal["malformed_response", "truncated_response"] = "malformed_response",
        parse_category: ParseCategoryV1 = "unknown",
        validation_issues: tuple[ShadowValidationIssueV1, ...] = (),
    ):
        self.code = code
        self.parse_category = parse_category
        self.validation_issues = validation_issues
        super().__init__(message)


class ShadowProviderCallError(RuntimeError):
    """Stable, sanitized provider failure metadata for the shadow boundary."""

    def __init__(
        self,
        category: ProviderErrorCategoryV1 = "unknown",
        *,
        http_status: int | None = None,
        retry_count: int = 0,
    ) -> None:
        self.category = category
        self.http_status = http_status if http_status is not None and 100 <= http_status <= 599 else None
        self.retry_count = max(0, min(int(retry_count), 128))
        super().__init__("shadow provider call failed")


@dataclass(frozen=True, slots=True)
class ShadowProviderRunError(RuntimeError):
    code: ShadowRunErrorCode
    telemetry: ShadowTelemetryV1

    def __str__(self) -> str:
        return f"shadow extraction failed: {self.code}"


_SYSTEM_PROMPT = """You extract only evidence-backed surface relations from untrusted text.
Do not use an ontology, canonical relation key, schema key, translation, paraphrase,
or inverse mapping. Copy the predicate wording as it appears in the source after only
basic whitespace normalization. Preserve source and target mention order. Keep unknown
direction as unknown and never swap endpoints. Use only declared evidence_ref_keys.
Return exactly one JSON object with one claims array and no Markdown, prose, or extra keys.
Each claim must have exactly this JSON shape and these JSON types:
{"source_mention":{"local_id":"s1","surface":"exact source text","entity_type_hint":null,"evidence_ref":"c0"},"surface_raw_predicate":"exact predicate text","target_mention":{"local_id":"t1","surface":"exact target text","entity_type_hint":null,"evidence_ref":"c0"},"surface_direction":"source_to_target","negation":{"value":false,"evidence_ref":"c0"},"modality":{"value":null,"evidence_ref":null},"qualifiers":[],"valid_time":null,"effective_time":null,"evidence_ref_keys":["c0"]}
surface_direction is exactly source_to_target, target_to_source, or unknown. A qualifier
is {"key":"...","value":"...","evidence_ref":"c0"}. A time value is null or
{"start":"ISO-8601 or null","end":"ISO-8601 or null","evidence_ref":"c0"}.
Every non-null evidence_ref must be declared in evidence_ref_keys, and that array must
contain exactly the evidence keys used by the claim. Do not copy the example wording.
"""


def shadow_prompt_content_hash() -> str:
    """Return the stable hash of the exact provider-facing shadow prompt."""

    return hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_size(value: object) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _validate_json_tree(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    nodes = nodes or [0]
    nodes[0] += 1
    if depth > _MAX_RESPONSE_DEPTH or nodes[0] > _MAX_RESPONSE_NODES:
        raise ShadowResponseParseError(
            "shadow response exceeds JSON depth or node bound",
            parse_category="bounds",
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ShadowResponseParseError("shadow response object keys must be strings")
            _validate_json_tree(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for child in value:
            _validate_json_tree(child, depth=depth + 1, nodes=nodes)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ShadowResponseParseError(
                "shadow response contains duplicate JSON keys",
                parse_category="json_syntax",
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ShadowResponseParseError(
        "shadow response contains invalid JSON constant",
        parse_category="json_syntax",
    )


def _validation_parse_category(error: ValidationError) -> ParseCategoryV1:
    error_types = {str(item.get("type", "")) for item in error.errors()}
    if any(item == "extra_forbidden" for item in error_types):
        return "schema_extra"
    if any(item in {"missing", "missing_sentinel_error"} for item in error_types):
        return "schema_missing"
    if any(
        item.endswith("_type")
        or item in {
            "bool_parsing",
            "int_parsing",
            "literal_error",
            "enum",
            "model_type",
        }
        for item in error_types
    ):
        return "schema_type"
    return "unknown"


def _validation_issues(
    error: ValidationError,
    *,
    claim_index: int,
) -> tuple[ShadowValidationIssueV1, ...]:
    grouped: dict[tuple[str, str], int] = {}
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ("claims", claim_index, *item.get("loc", ()))
        path = ".".join(str(part) for part in location)[:256]
        error_type = str(item.get("type") or "unknown")[:64]
        key = (path, error_type)
        grouped[key] = min(grouped.get(key, 0) + 1, 128)
    return tuple(
        ShadowValidationIssueV1(path=path, error_type=error_type, count=count)
        for (path, error_type), count in sorted(grouped.items())[:32]
    )


def build_shadow_extraction_request(
    *,
    unit_text: str,
    evidence_contexts: Sequence[ShadowEvidenceContextV1],
    provenance: ShadowExtractionProvenanceV1,
    limits: ShadowExtractionLimitsV1 | None = None,
) -> ShadowExtractionRequestV1:
    limits = limits or ShadowExtractionLimitsV1()
    contexts = tuple(evidence_contexts)
    if not contexts:
        raise ValueError("shadow request requires evidence contexts")
    if len({context.ref_key for context in contexts}) != len(contexts):
        raise ValueError("shadow evidence ref keys must be unique")
    if not isinstance(provenance, ShadowExtractionProvenanceV1):
        raise TypeError("provenance must be ShadowExtractionProvenanceV1")
    if not isinstance(unit_text, str) or not unit_text.strip() or "\x00" in unit_text:
        raise ValueError("unit_text must be non-empty text")

    user_payload = {
        "unit_text": unit_text,
        "evidence_contexts": [
            {"ref_key": context.ref_key, "locator": safe_locator_summary(context.locator)}
            for context in contexts
        ],
        "protocol": {"version": "raw_claim_v1", "prompt_version": SHADOW_PROMPT_VERSION},
    }
    messages = (
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Extract surface claims from this server-supplied data only.\n" + _canonical_json(user_payload),
        },
    )
    estimated_tokens = estimate_chat_request_tokens(
        list(messages),
        response_format={"type": "json_object"},
        reserved_output_tokens=limits.max_output_tokens,
        model_name=provenance.model_name,
    )
    if estimated_tokens > limits.max_request_tokens:
        raise ValueError("shadow request exceeds token budget")
    request_payload = {
        "messages": messages,
        "provenance": provenance.model_dump(mode="json"),
        "limits": limits.model_dump(mode="json"),
    }
    if _json_size(request_payload) > limits.max_request_bytes:
        raise ValueError("shadow request exceeds byte bound")
    return ShadowExtractionRequestV1(
        unit_text=unit_text,
        evidence_contexts=contexts,
        provenance=provenance,
        limits=limits,
        messages=messages,
        request_hash=canonical_shadow_request_hash(request_payload),
        estimated_request_tokens=estimated_tokens,
    )


def parse_shadow_response(
    content: str,
    *,
    allowed_evidence_ref_keys: Sequence[str],
    max_claims: int = 32,
    finish_reason: str | None = None,
) -> tuple[ShadowRawResponseV1, ...]:
    if finish_reason and finish_reason.casefold() == "length":
        raise ShadowResponseParseError(
            "shadow provider response was truncated",
            code="truncated_response",
            parse_category="bounds",
        )
    if not isinstance(content, str) or not content.strip():
        raise ShadowResponseParseError(
            "shadow provider response must be non-empty JSON",
            parse_category="json_syntax",
        )
    if len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ShadowResponseParseError(
            "shadow provider response exceeds byte bound",
            parse_category="bounds",
        )
    stripped = content.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise ShadowResponseParseError(
            "Markdown fences are not accepted",
            parse_category="markdown_or_reasoning_wrapper",
        )
    try:
        decoded = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ShadowResponseParseError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ShadowResponseParseError(
            "shadow provider response is not strict JSON",
            parse_category="json_syntax",
        ) from exc
    _validate_json_tree(decoded)
    if isinstance(decoded, list):
        raw_claims = decoded
    elif isinstance(decoded, dict) and set(decoded) == {"claims"}:
        if not isinstance(decoded["claims"], list):
            raise ShadowResponseParseError(
                "shadow response claims must be a list",
                parse_category="schema_type",
            )
        raw_claims = decoded["claims"]
    else:
        category: ParseCategoryV1 = "schema_missing" if isinstance(decoded, dict) and "claims" not in decoded else "schema_extra"
        raise ShadowResponseParseError(
            "shadow response must be a claims array or exact claims wrapper",
            parse_category=category,
        )
    if len(raw_claims) > max_claims:
        raise ShadowResponseParseError(
            "shadow response exceeds claim count bound",
            parse_category="bounds",
        )
    allowed = set(allowed_evidence_ref_keys)
    claims: list[ShadowRawResponseV1] = []
    for claim_index, raw_claim in enumerate(raw_claims):
        try:
            claim = ShadowRawResponseV1.model_validate(raw_claim)
        except ValidationError as exc:
            raise ShadowResponseParseError(
                "shadow response claim failed protocol validation",
                parse_category=_validation_parse_category(exc),
                validation_issues=_validation_issues(exc, claim_index=claim_index),
            ) from exc
        if not set(claim.evidence_ref_keys).issubset(allowed):
            raise ShadowResponseParseError(
                "shadow response contains an undeclared evidence key",
                parse_category="evidence_reference",
            )
        claims.append(claim)
    return tuple(claims)


def _coerce_provider_response(value: object) -> ShadowProviderResponseV1:
    if isinstance(value, ShadowProviderResponseV1):
        return value
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {
            field: getattr(value, field, None)
            for field in (
                "content",
                "finish_reason",
                "input_token_count",
                "output_token_count",
                "latency_ms",
                "retry_count",
            )
        }
    try:
        return ShadowProviderResponseV1.model_validate(payload)
    except ValidationError as exc:
        raise ShadowResponseParseError(
            "shadow provider returned an invalid response contract",
            parse_category="schema_type",
        ) from exc


async def run_shadow_extraction(
    request: ShadowExtractionRequestV1,
    provider: Callable[[ShadowExtractionRequestV1], Awaitable[ShadowProviderResponseV1]],
    *,
    config: ShadowExtractionConfigV1,
    timeout_seconds: float = 120.0,
) -> ShadowExtractionResultV1:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not resolve_shadow_extraction(config):
        telemetry = ShadowTelemetryV1(
            request_hash=request.request_hash,
            latency_ms=0,
            finish_reason="skipped",
            final_outcome="skipped",
        )
        return ShadowExtractionResultV1(status="skipped", telemetry=telemetry)

    started = time.perf_counter()
    try:
        provider_response = await asyncio.wait_for(provider(request), timeout=timeout_seconds)
    except TimeoutError as exc:
        telemetry = ShadowTelemetryV1(
            request_hash=request.request_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_code="timeout",
            retry_count=0,
            final_outcome="timeout",
        )
        raise ShadowProviderRunError("timeout", telemetry) from exc
    except ShadowProviderCallError as exc:
        telemetry = ShadowTelemetryV1(
            request_hash=request.request_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_code="provider_error",
            provider_error_category=exc.category,
            http_status=exc.http_status,
            retry_count=exc.retry_count,
            final_outcome="failed",
        )
        raise ShadowProviderRunError("provider_error", telemetry) from exc
    except Exception as exc:  # noqa: BLE001 - provider boundary maps all provider failures
        telemetry = ShadowTelemetryV1(
            request_hash=request.request_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_code="provider_error",
            provider_error_category="unknown",
            retry_count=0,
            final_outcome="failed",
        )
        raise ShadowProviderRunError("provider_error", telemetry) from exc

    latency_ms = round((time.perf_counter() - started) * 1000)
    response: ShadowProviderResponseV1 | None = None
    try:
        response = _coerce_provider_response(provider_response)
        response_hash = hashlib.sha256(response.content.encode("utf-8")).hexdigest()
        claims = parse_shadow_response(
            response.content,
            allowed_evidence_ref_keys=request.evidence_ref_keys,
            max_claims=request.limits.max_claims,
            finish_reason=response.finish_reason,
        )
    except ShadowResponseParseError as exc:
        code = exc.code
        response_hash = (
            hashlib.sha256(response.content.encode("utf-8")).hexdigest()
            if response is not None
            else None
        )
        telemetry = ShadowTelemetryV1(
            request_hash=request.request_hash,
            response_hash=response_hash,
            input_token_count=response.input_token_count if response is not None else None,
            output_token_count=response.output_token_count if response is not None else None,
            latency_ms=latency_ms,
            finish_reason=response.finish_reason if response is not None else None,
            error_code=code,
            retry_count=response.retry_count if response is not None else 0,
            final_outcome="failed",
            parse_category=exc.parse_category,
            validation_error_count=sum(issue.count for issue in exc.validation_issues),
            validation_issues=exc.validation_issues,
        )
        raise ShadowProviderRunError(code, telemetry) from exc

    telemetry = ShadowTelemetryV1(
        request_hash=request.request_hash,
        response_hash=response_hash,
        input_token_count=response.input_token_count,
        output_token_count=response.output_token_count,
        latency_ms=latency_ms,
        finish_reason=response.finish_reason,
        claim_count=len(claims),
        retry_count=response.retry_count,
        final_outcome="success",
    )
    return ShadowExtractionResultV1(status="success", claims=claims, telemetry=telemetry)
