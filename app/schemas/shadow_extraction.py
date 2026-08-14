"""Bounded, provider-neutral protocol for shadow extraction calls."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.evidence_locator import SourceLocatorV1
from app.schemas.raw_claim import EvidenceLocatorClaimRefV1
from app.schemas.shadow_raw_response import ShadowExtractionProvenanceV1, ShadowRawResponseV1


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_REF_KEY_LENGTH = 64


class _ShadowExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShadowEvidenceContextV1(_ShadowExtractionModel):
    """Safe evidence locator projection sent to the shadow provider."""

    ref_key: str = Field(min_length=1, max_length=_MAX_REF_KEY_LENGTH)
    locator: EvidenceLocatorClaimRefV1

    @field_validator("ref_key", mode="before")
    @classmethod
    def normalize_ref_key(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("ref_key must be a string")
        value = value.strip()
        if not value or len(value) > _MAX_REF_KEY_LENGTH or "\x00" in value:
            raise ValueError("ref_key must be non-empty and bounded")
        return value

    @classmethod
    def from_locator(cls, ref_key: str, locator: EvidenceLocatorClaimRefV1) -> ShadowEvidenceContextV1:
        return cls(ref_key=ref_key, locator=locator)


class ShadowExtractionLimitsV1(_ShadowExtractionModel):
    """Per-call bounds kept outside application Settings for M2C."""

    max_request_bytes: StrictInt = Field(default=64 * 1024, ge=4096, le=1024 * 1024)
    max_request_tokens: StrictInt = Field(default=8192, ge=256, le=32768)
    max_output_tokens: StrictInt = Field(default=2048, ge=128, le=32768)
    max_claims: StrictInt = Field(default=32, ge=1, le=128)


class ShadowExtractionRequestV1(_ShadowExtractionModel):
    """The complete call input; only ``messages`` are provider-facing."""

    unit_text: str = Field(min_length=1, max_length=48_000)
    evidence_contexts: tuple[ShadowEvidenceContextV1, ...] = Field(min_length=1, max_length=16)
    provenance: ShadowExtractionProvenanceV1
    limits: ShadowExtractionLimitsV1
    messages: tuple[dict[str, str], ...] = Field(min_length=2, max_length=2)
    request_hash: str
    estimated_request_tokens: StrictInt = Field(ge=1)

    @field_validator("unit_text", mode="before")
    @classmethod
    def validate_unit_text(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("unit_text must be non-empty text")
        if len(value) > 48_000:
            raise ValueError("unit_text exceeds the request bound")
        return value

    @field_validator("request_hash")
    @classmethod
    def validate_request_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("request_hash must be a lowercase SHA-256 hex digest")
        return value

    @property
    def evidence_ref_keys(self) -> frozenset[str]:
        return frozenset(context.ref_key for context in self.evidence_contexts)


class ShadowProviderResponseV1(_ShadowExtractionModel):
    """Minimal provider return contract; it deliberately has no raw envelope."""

    content: str
    finish_reason: str | None = Field(default=None, max_length=64)
    input_token_count: StrictInt | None = Field(default=None, ge=0)
    output_token_count: StrictInt | None = Field(default=None, ge=0)
    latency_ms: StrictInt | None = Field(default=None, ge=0)
    retry_count: StrictInt = Field(default=0, ge=0, le=128)


ProviderErrorCategoryV1 = Literal[
    "network",
    "http",
    "auth",
    "rate_limit",
    "budget",
    "config",
    "invalid_envelope",
    "unknown",
]

ParseCategoryV1 = Literal[
    "json_syntax",
    "markdown_or_reasoning_wrapper",
    "schema_missing",
    "schema_extra",
    "schema_type",
    "evidence_reference",
    "bounds",
    "unknown",
]

ShadowFinalOutcomeV1 = Literal["success", "failed", "timeout", "skipped"]


class ShadowValidationIssueV1(_ShadowExtractionModel):
    """Bounded Pydantic location/type metadata with no rejected values."""

    path: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.\[\]-]+$")
    error_type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    count: StrictInt = Field(ge=1, le=128)


class ShadowTelemetryV1(_ShadowExtractionModel):
    request_hash: str
    response_hash: str | None = None
    input_token_count: StrictInt | None = Field(default=None, ge=0)
    output_token_count: StrictInt | None = Field(default=None, ge=0)
    latency_ms: StrictInt = Field(ge=0)
    finish_reason: str | None = Field(default=None, max_length=64)
    claim_count: StrictInt = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    provider_error_category: ProviderErrorCategoryV1 | None = None
    http_status: StrictInt | None = Field(default=None, ge=100, le=599)
    retry_count: StrictInt = Field(default=0, ge=0, le=128)
    final_outcome: ShadowFinalOutcomeV1 = "success"
    parse_category: ParseCategoryV1 | None = None
    validation_error_count: StrictInt = Field(default=0, ge=0, le=4096)
    validation_issues: tuple[ShadowValidationIssueV1, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )

    @field_validator("request_hash", "response_hash")
    @classmethod
    def validate_hashes(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("telemetry hashes must be lowercase SHA-256 hex digests")
        return value

    @model_validator(mode="after")
    def validate_issue_count(self) -> ShadowTelemetryV1:
        if self.validation_error_count != sum(issue.count for issue in self.validation_issues):
            raise ValueError("validation error count must match bounded issues")
        return self


ShadowRunStatusV1 = Literal["success", "skipped"]


class ShadowExtractionResultV1(_ShadowExtractionModel):
    status: ShadowRunStatusV1
    claims: tuple[ShadowRawResponseV1, ...] = Field(default_factory=tuple)
    telemetry: ShadowTelemetryV1


def canonical_shadow_request_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def safe_locator_summary(locator: EvidenceLocatorClaimRefV1) -> dict[str, object]:
    """Return locator coordinates without IDs, hashes, file paths, or text."""

    source = locator.source.model_dump(mode="json") if isinstance(locator.source, SourceLocatorV1) else None
    if source is not None:
        source.pop("file_name", None)
    return {
        "locator_version": locator.locator_version,
        "unit_kind": locator.unit_kind,
        "ordinal": locator.ordinal,
        "source": source,
    }
