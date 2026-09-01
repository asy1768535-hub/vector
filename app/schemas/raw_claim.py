"""Domain-neutral, DB-free RawClaimV1 contract.

This module deliberately has no parser, provider, worker, ORM, or publication
dependency.  It validates an evidence-backed claim and computes the two
fingerprints needed to separate stable claim content from an extraction run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas._strict_datetime import canonical_datetime_string, strict_datetime
from app.schemas.evidence_locator import EvidenceLocatorV1, SourceLocatorV1, TextSpanV1, UnitKind


DirectionV1 = Literal[
    "source_to_target",
    "target_to_source",
    "undirected",
    "unknown",
]

MAX_MENTION_ID_LENGTH = 128
MAX_SURFACE_LENGTH = 512
MAX_PREDICATE_LENGTH = 256
MAX_DYNAMIC_STRING_LENGTH = 256
MAX_QUALIFIERS = 32
MAX_EVIDENCE_REFS = 16
MAX_JSON_DEPTH = 5
MAX_JSON_NODES = 256
MAX_JSON_BYTES = 8192
MAX_CANONICAL_CLAIM_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEYS = frozenset(
    {
        "bucket",
        "etag",
        "objectkey",
        "quote",
        "rawfilesha256",
        "normalizedcontenthash",
        "storagepath",
        "text",
    }
)


class _RawClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _bounded_string(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"{field} must be non-empty and bounded")
    return normalized


def _sha256(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _normalized_sensitive_key(value: str) -> str:
    """Normalize case, separators, and camelCase before sensitive-key checks."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _validate_json_value(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    reject_sensitive_keys: bool = False,
) -> Any:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise ValueError("dynamic JSON value exceeds depth or node limits")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > MAX_DYNAMIC_STRING_LENGTH:
            raise ValueError("dynamic JSON string exceeds the size limit")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("dynamic JSON numbers must be finite")
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_NODES:
            raise ValueError("dynamic JSON array exceeds the node limit")
        for item in value:
            _validate_json_value(
                item,
                depth=depth + 1,
                nodes=nodes,
                reject_sensitive_keys=reject_sensitive_keys,
            )
        return value
    if isinstance(value, dict):
        if len(value) > MAX_JSON_NODES:
            raise ValueError("dynamic JSON object exceeds the size limit")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_DYNAMIC_STRING_LENGTH:
                raise ValueError("dynamic JSON object keys must be bounded strings")
            if reject_sensitive_keys and _normalized_sensitive_key(key) in _SENSITIVE_KEYS:
                raise ValueError(f"sensitive evidence field is not allowed: {key}")
            _validate_json_value(
                item,
                depth=depth + 1,
                nodes=nodes,
                reject_sensitive_keys=reject_sensitive_keys,
            )
        return value
    raise ValueError("dynamic value must be JSON-safe")


def _validate_dynamic_payload(value: Any, *, reject_sensitive_keys: bool = False) -> Any:
    validated = _validate_json_value(value, reject_sensitive_keys=reject_sensitive_keys)
    encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("dynamic JSON value exceeds the byte limit")
    return validated


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_CANONICAL_CLAIM_BYTES:
        raise ValueError("canonical claim JSON exceeds the size limit")
    return encoded


def canonical_raw_claim_json(value: RawClaimV1 | Mapping[str, Any]) -> str:
    """Return deterministic JSON for the complete non-sensitive contract."""
    claim = value if isinstance(value, RawClaimV1) else RawClaimV1.model_validate(value)
    return _canonical_json(claim)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ClaimMentionV1(_RawClaimModel):
    local_id: str = Field(min_length=1, max_length=MAX_MENTION_ID_LENGTH)
    surface: str = Field(min_length=1, max_length=MAX_SURFACE_LENGTH)
    entity_type_hint: str | None = Field(default=None, max_length=128)
    evidence_ref: str = Field(min_length=1, max_length=64)

    @field_validator("local_id", "surface", "entity_type_hint", "evidence_ref", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any, info) -> str | None:
        if value is None and info.field_name == "entity_type_hint":
            return None
        return _bounded_string(
            value,
            field=info.field_name,
            limit=128 if info.field_name in {"local_id", "entity_type_hint", "evidence_ref"} else MAX_SURFACE_LENGTH,
        )


class NegationV1(_RawClaimModel):
    value: bool
    evidence_ref: str | None = Field(default=None, max_length=64)

    @field_validator("evidence_ref", mode="before")
    @classmethod
    def normalize_evidence_ref(cls, value: Any) -> str | None:
        return _bounded_string(value, field="evidence_ref", limit=64) if value is not None else None


class ModalityV1(_RawClaimModel):
    value: str | None = Field(default=None, max_length=MAX_DYNAMIC_STRING_LENGTH)
    evidence_ref: str | None = Field(default=None, max_length=64)

    @field_validator("value", "evidence_ref", mode="before")
    @classmethod
    def normalize_values(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return _bounded_string(
            value,
            field=info.field_name,
            limit=64 if info.field_name == "evidence_ref" else MAX_DYNAMIC_STRING_LENGTH,
        )


class QualifierV1(_RawClaimModel):
    key: str = Field(min_length=1, max_length=MAX_DYNAMIC_STRING_LENGTH)
    value: Any
    evidence_ref: str | None = Field(default=None, max_length=64)

    @field_validator("key", "evidence_ref", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any, info) -> str | None:
        if value is None and info.field_name == "evidence_ref":
            return None
        normalized = _bounded_string(
            value,
            field=info.field_name,
            limit=64 if info.field_name == "evidence_ref" else MAX_DYNAMIC_STRING_LENGTH,
        )
        if info.field_name == "key" and _normalized_sensitive_key(normalized) in _SENSITIVE_KEYS:
            raise ValueError("qualifier key is reserved for sensitive evidence fields")
        return normalized

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Any) -> Any:
        return _validate_dynamic_payload(value, reject_sensitive_keys=True)


def _parse_iso8601(value: str) -> datetime:
    return strict_datetime(value, field="time")


class TimeIntervalV1(_RawClaimModel):
    start: str | None = None
    end: str | None = None
    evidence_ref: str | None = Field(default=None, max_length=64)

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_time(cls, value: Any) -> str | None:
        if value is None:
            return None
        return canonical_datetime_string(value, field="time")

    @field_validator("evidence_ref", mode="before")
    @classmethod
    def normalize_evidence_ref(cls, value: Any) -> str | None:
        return _bounded_string(value, field="evidence_ref", limit=64) if value is not None else None

    @model_validator(mode="after")
    def validate_order(self) -> TimeIntervalV1:
        if self.start is not None and self.end is not None and _parse_iso8601(self.start) > _parse_iso8601(self.end):
            raise ValueError("time interval start must not be after end")
        return self


class EvidenceLocatorClaimRefV1(_RawClaimModel):
    """Safe locator projection used by a claim; no raw file identity or text."""

    locator_version: Literal["v1"]
    document_id: UUID
    document_revision_id: UUID
    revision_no: int = Field(ge=1)
    unit_id: UUID
    unit_kind: UnitKind
    ordinal: int = Field(ge=0)
    quote_sha256: str
    unit_text_sha256: str
    source: SourceLocatorV1 | None = None

    @field_validator("quote_sha256", "unit_text_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @classmethod
    def from_locator(cls, locator: EvidenceLocatorV1) -> EvidenceLocatorClaimRefV1:
        if locator.provenance_status != "verified":
            raise ValueError("raw claim locator reference requires a verified locator")
        if locator.quote_sha256 is None or locator.unit_text_sha256 is None:
            raise ValueError("raw claim locator reference requires quote and unit hashes")
        return cls(
            locator_version=locator.locator_version,
            document_id=locator.document_id,
            document_revision_id=locator.document_revision_id,
            revision_no=locator.revision_no,
            unit_id=locator.unit_id,
            unit_kind=locator.unit_kind,
            ordinal=locator.ordinal,
            quote_sha256=locator.quote_sha256,
            unit_text_sha256=locator.unit_text_sha256,
            source=locator.source,
        )


class EvidenceReferenceV1(_RawClaimModel):
    ref_id: str = Field(min_length=1, max_length=64)
    evidence_id: UUID
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: int = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    unit_id: UUID
    chunk_id: UUID | None = None
    block_id: UUID | None = None
    quote_sha256: str
    unit_text_sha256: str
    source_span: TextSpanV1 | None = None
    locator: EvidenceLocatorClaimRefV1 | None = None

    @field_validator("ref_id", mode="before")
    @classmethod
    def normalize_ref_id(cls, value: Any) -> str:
        return _bounded_string(value, field="ref_id", limit=64)

    @field_validator("quote_sha256", "unit_text_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator("locator", mode="before")
    @classmethod
    def project_locator(cls, value: Any) -> Any:
        if value is None or isinstance(value, EvidenceLocatorClaimRefV1):
            return value
        if isinstance(value, EvidenceLocatorV1):
            return EvidenceLocatorClaimRefV1.from_locator(value)
        if isinstance(value, Mapping) and {"parser", "quality", "source"}.issubset(value):
            return EvidenceLocatorClaimRefV1.from_locator(EvidenceLocatorV1.model_validate(value))
        return value

    @model_validator(mode="after")
    def validate_locator_identity(self) -> EvidenceReferenceV1:
        if self.locator is None and self.source_span is None:
            raise ValueError("evidence reference requires a locator or source span")
        if self.locator is None:
            return self
        if (
            self.locator.document_id != self.document_id
            or self.locator.document_revision_id != self.document_revision_id
            or self.locator.revision_no != self.revision_no
            or self.locator.unit_id != self.unit_id
            or self.locator.quote_sha256 != self.quote_sha256
            or self.locator.unit_text_sha256 != self.unit_text_sha256
        ):
            raise ValueError("EvidenceLocatorV1 identity or required hashes do not match the evidence reference")
        locator_span = self.locator.source.text
        if locator_span is not None:
            if (self.source_span is not None) and (
                self.source_span.start != locator_span.start or self.source_span.end != locator_span.end
            ):
                raise ValueError("evidence source span conflicts with locator source span")
            if self.source_span is not None and self.source_span.ranges and locator_span.ranges:
                source_ranges = [item.model_dump(mode="json") for item in self.source_span.ranges]
                locator_ranges = [item.model_dump(mode="json") for item in locator_span.ranges]
                if source_ranges != locator_ranges:
                    raise ValueError("evidence source ranges conflict with locator source ranges")
        return self


def stable_evidence_identity(reference: EvidenceReferenceV1 | Mapping[str, Any]) -> str:
    """Return the persistence-stable identity for one evidence reference.

    Local ``ref_id``, job, and extraction-unit aliases are intentionally
    excluded.  This is the shared byte-level contract for core fingerprints
    and replay export; changing it would change persisted claim identity.
    """
    validated = reference if isinstance(reference, EvidenceReferenceV1) else EvidenceReferenceV1.model_validate(reference)
    payload = validated.model_dump(mode="json")
    payload.pop("ref_id", None)
    payload.pop("job_id", None)
    payload.pop("extraction_unit_id", None)
    return _sha256_json(payload)


def _claim_content_payload(claim: RawClaimV1) -> dict[str, Any]:
    payload = claim.model_dump(mode="json")
    for field in (
        "claim_id",
        "content_scoped_claim_fingerprint",
        "extraction_occurrence_id",
        "extraction_occurrence_fingerprint",
        "job_id",
        "extraction_unit_id",
        "extractor_version",
        "prompt_version",
        "model_provider",
        "model_name",
        "model_config_hash",
        "prompt_content_hash",
        "parser_version",
        "normalization_rule_version",
        "ontology_snapshot_hash",
    ):
        payload.pop(field, None)
    evidence_identities = {
        reference.ref_id: stable_evidence_identity(reference) for reference in claim.evidence_refs
    }
    for field in ("source_mention", "target_mention", "negation", "modality", "valid_time", "effective_time"):
        value = payload.get(field)
        if isinstance(value, dict) and value.get("evidence_ref") is not None:
            value["evidence_ref"] = evidence_identities[value["evidence_ref"]]
    for qualifier in payload.get("qualifiers", []):
        if isinstance(qualifier, dict) and qualifier.get("evidence_ref") is not None:
            qualifier["evidence_ref"] = evidence_identities[qualifier["evidence_ref"]]
    stable_references = []
    for reference in payload.get("evidence_refs", []):
        stable_reference = dict(reference)
        ref_id = stable_reference.pop("ref_id")
        stable_reference.pop("job_id", None)
        stable_reference.pop("extraction_unit_id", None)
        stable_reference["evidence_identity"] = evidence_identities[ref_id]
        stable_references.append(stable_reference)
    payload["evidence_refs"] = sorted(stable_references, key=lambda item: item["evidence_identity"])
    # Qualifier order is not semantic for this raw contract; canonical ordering
    # makes equivalent qualifier sets fingerprint-identical.
    payload["qualifiers"] = sorted(payload.get("qualifiers", []), key=_canonical_json)
    return payload


def content_scoped_claim_fingerprint(claim: RawClaimV1) -> str:
    return _sha256_json(_claim_content_payload(claim))


def _claim_occurrence_payload(claim: RawClaimV1) -> dict[str, Any]:
    payload = _claim_content_payload(claim)
    payload.update(
        {
            "content_scoped_claim_fingerprint": content_scoped_claim_fingerprint(claim),
            "job_id": str(claim.job_id),
            "extraction_unit_id": str(claim.extraction_unit_id),
            "extractor_version": claim.extractor_version,
            "prompt_version": claim.prompt_version,
            "model_provider": claim.model_provider,
            "model_name": claim.model_name,
            "model_config_hash": claim.model_config_hash,
            "prompt_content_hash": claim.prompt_content_hash,
            "parser_version": claim.parser_version,
            "normalization_rule_version": claim.normalization_rule_version,
            "ontology_snapshot_hash": claim.ontology_snapshot_hash,
        }
    )
    return payload


def extraction_occurrence_fingerprint(claim: RawClaimV1) -> str:
    return _sha256_json(_claim_occurrence_payload(claim))


class RawClaimV1(_RawClaimModel):
    claim_schema_version: Literal["raw_claim_v1"] = "raw_claim_v1"
    claim_id: UUID
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: int = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    source_mention: ClaimMentionV1
    raw_predicate: str = Field(min_length=1, max_length=MAX_PREDICATE_LENGTH)
    target_mention: ClaimMentionV1
    surface_direction: DirectionV1
    negation: NegationV1
    modality: ModalityV1
    qualifiers: tuple[QualifierV1, ...] = Field(default_factory=tuple, max_length=MAX_QUALIFIERS)
    valid_time: TimeIntervalV1 | None = None
    effective_time: TimeIntervalV1 | None = None
    evidence_refs: tuple[EvidenceReferenceV1, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_REFS)
    extractor_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    model_provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    model_config_hash: str
    prompt_content_hash: str
    parser_version: str = Field(min_length=1, max_length=64)
    normalization_rule_version: str = Field(min_length=1, max_length=64)
    ontology_snapshot_hash: str | None = None
    content_scoped_claim_fingerprint: str | None = None
    extraction_occurrence_id: UUID
    extraction_occurrence_fingerprint: str | None = None

    @field_validator(
        "raw_predicate",
        "extractor_version",
        "prompt_version",
        "model_provider",
        "model_name",
        "parser_version",
        "normalization_rule_version",
        mode="before",
    )
    @classmethod
    def normalize_provenance_strings(cls, value: Any, info) -> str:
        limits = {"raw_predicate": MAX_PREDICATE_LENGTH, "model_name": 128}
        return _bounded_string(value, field=info.field_name, limit=limits.get(info.field_name, 64))

    @field_validator("model_config_hash", "prompt_content_hash", "ontology_snapshot_hash", mode="before")
    @classmethod
    def validate_provenance_hashes(cls, value: Any, info) -> str | None:
        if value is None and info.field_name == "ontology_snapshot_hash":
            return None
        return _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_scope_and_fingerprints(self) -> RawClaimV1:
        reference_ids = [reference.ref_id for reference in self.evidence_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("evidence reference ref_id values must be unique within a claim")
        declared_refs = set(reference_ids)
        referenced_values = {
            self.source_mention.evidence_ref,
            self.target_mention.evidence_ref,
            self.negation.evidence_ref,
            self.modality.evidence_ref,
            *(qualifier.evidence_ref for qualifier in self.qualifiers),
            *(interval.evidence_ref for interval in (self.valid_time, self.effective_time) if interval is not None),
        }
        dangling_refs = {value for value in referenced_values if value is not None} - declared_refs
        if dangling_refs:
            raise ValueError("claim contains dangling evidence references")
        for interval in (self.valid_time, self.effective_time):
            if interval is not None and interval.evidence_ref is None:
                raise ValueError("time interval evidence_ref is required when the interval is present")
        for reference in self.evidence_refs:
            if (
                reference.library_id != self.library_id
                or reference.document_id != self.document_id
                or reference.document_revision_id != self.document_revision_id
                or reference.revision_no != self.revision_no
                or reference.job_id != self.job_id
                or reference.extraction_unit_id != self.extraction_unit_id
            ):
                raise ValueError("evidence reference crosses the RawClaim scope")

        content_fingerprint = content_scoped_claim_fingerprint(self)
        occurrence_fingerprint = extraction_occurrence_fingerprint(self)
        if self.content_scoped_claim_fingerprint not in (None, content_fingerprint):
            raise ValueError("content scoped claim fingerprint does not match the claim")
        if self.extraction_occurrence_fingerprint not in (None, occurrence_fingerprint):
            raise ValueError("extraction occurrence fingerprint does not match the claim")
        object.__setattr__(self, "content_scoped_claim_fingerprint", content_fingerprint)
        object.__setattr__(self, "extraction_occurrence_fingerprint", occurrence_fingerprint)
        return self


def revalidate_raw_claim(value: RawClaimV1) -> RawClaimV1:
    """Rebuild a RawClaimV1 from canonical fields at trusted boundaries."""
    if not isinstance(value, RawClaimV1):
        raise TypeError("value must be a RawClaimV1")
    return RawClaimV1.model_validate(json.loads(canonical_raw_claim_json(value)))
