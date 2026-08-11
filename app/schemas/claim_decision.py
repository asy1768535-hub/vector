"""DB-free, append-only decision projection for raw claim follow-up."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Mapping
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas._strict_datetime import canonical_datetime_string, strict_datetime
from app.schemas.raw_claim import ClaimMentionV1, DirectionV1


DecisionSchemaVersion = Literal["claim_decision_projection_v1"]
DecisionKind = Literal["mapping_candidate", "schema_extension_candidate"]
DecisionStatus = Literal["pending"]
DecisionReasonCode = Literal[
    "unknown_predicate",
    "unknown_source_type",
    "unknown_target_type",
    "unknown_direction",
    "ambiguous_mapping",
]
DecisionProducerKind = Literal["system", "human", "external"]
MAPPING_DECISION_REASONS = frozenset({"unknown_predicate", "unknown_direction", "ambiguous_mapping"})
SCHEMA_EXTENSION_DECISION_REASONS = frozenset({"unknown_source_type", "unknown_target_type"})

MAX_DECISION_STRING_LENGTH = 128
MAX_DECISION_REF_IDS = 16
MAX_DECISION_PROPOSAL_BYTES = 8192
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
CLAIM_DECISION_ID_NAMESPACE = UUID("6d26de8e-6a6f-5f5a-9f8c-1cb0a3a8e8e1")
_SENSITIVE_PRODUCER_KEY_PARTS = frozenset(
    {
        "bucket",
        "etag",
        "objectkey",
        "quote",
        "rawfile",
        "normalizedcontent",
        "storage",
        "secret",
        "password",
        "text",
    }
)


class _DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


def _bounded_string(value: Any, *, field: str, limit: int = MAX_DECISION_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} must be non-empty and bounded")
    return value


def _bounded_producer_key(value: Any) -> str:
    normalized = _bounded_string(value, field="producer_key")
    if "/" in normalized or "\\" in normalized or ".." in normalized:
        raise ValueError("producer_key must not contain a path")
    key = re.sub(r"[^a-z0-9]", "", normalized.casefold())
    if any(part in key for part in _SENSITIVE_PRODUCER_KEY_PARTS):
        raise ValueError("producer_key must not contain sensitive identity data")
    return normalized


def _normalize_ref_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("evidence_ref_ids must be a list or tuple")
    refs = tuple(sorted(_bounded_string(item, field="evidence_ref_id", limit=64) for item in value))
    if not refs or len(refs) > MAX_DECISION_REF_IDS or len(refs) != len(set(refs)):
        raise ValueError("evidence_ref_ids must be non-empty, unique, and bounded")
    return refs


class DecisionEndpointV1(_DecisionModel):
    """A role-specific unknown endpoint proposal; never an ontology row."""

    local_id: str = Field(min_length=1, max_length=128)
    surface: str = Field(min_length=1, max_length=512)
    entity_type_hint: str | None = Field(default=None, max_length=128)
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_DECISION_REF_IDS)

    @field_validator("local_id", "surface", "entity_type_hint", mode="before")
    @classmethod
    def normalize_text(cls, value: Any, info) -> str | None:
        if value is None and info.field_name == "entity_type_hint":
            return None
        return _bounded_string(
            value,
            field=info.field_name,
            limit=128 if info.field_name in {"local_id", "entity_type_hint"} else 512,
        )

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalize_ref_ids(value)


class MappingCandidateProposalV1(_DecisionModel):
    """An explicit proposal, with no inferred canonical relation key."""

    raw_predicate: str = Field(min_length=1, max_length=256)
    source_mention: ClaimMentionV1
    target_mention: ClaimMentionV1
    surface_direction: DirectionV1
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_DECISION_REF_IDS)
    suggested_canonical_key: str | None = Field(default=None, max_length=128)

    @field_validator("raw_predicate", "suggested_canonical_key", mode="before")
    @classmethod
    def normalize_text(cls, value: Any, info) -> str | None:
        if value is None and info.field_name == "suggested_canonical_key":
            return None
        return _bounded_string(value, field=info.field_name, limit=256 if info.field_name == "raw_predicate" else 128)

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalize_ref_ids(value)


class SchemaExtensionCandidateProposalV1(_DecisionModel):
    """Role-preserving endpoint extension proposal without ontology mutation."""

    raw_predicate: str = Field(min_length=1, max_length=256)
    source_endpoint: DecisionEndpointV1
    target_endpoint: DecisionEndpointV1
    surface_direction: DirectionV1
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_DECISION_REF_IDS)

    @field_validator("raw_predicate", mode="before")
    @classmethod
    def normalize_predicate(cls, value: Any) -> str:
        return _bounded_string(value, field="raw_predicate", limit=256)

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalize_ref_ids(value)


DecisionProposalV1 = MappingCandidateProposalV1 | SchemaExtensionCandidateProposalV1


def _decision_identity_payload(value: ClaimDecisionProjectionV1) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    for field in ("decision_id", "decision_fingerprint", "created_at"):
        payload.pop(field, None)
    return payload


def claim_decision_fingerprint(value: ClaimDecisionProjectionV1 | Mapping[str, Any]) -> str:
    projection = value if isinstance(value, ClaimDecisionProjectionV1) else ClaimDecisionProjectionV1.model_validate(value)
    encoded = json.dumps(
        _decision_identity_payload(projection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_decision_id(namespace: UUID, decision_fingerprint: str) -> UUID:
    if _SHA256.fullmatch(decision_fingerprint) is None:
        raise ValueError("decision_fingerprint must be a lowercase SHA-256 hex digest")
    return uuid5(namespace, f"claim_decision_projection_v1:{decision_fingerprint}")


class ClaimDecisionProjectionV1(_DecisionModel):
    decision_id: UUID
    decision_fingerprint: str | None = None
    decision_schema_version: DecisionSchemaVersion = "claim_decision_projection_v1"
    decision_version: StrictInt = Field(ge=1)
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    claim_id: UUID
    extraction_occurrence_id: UUID | None = None
    decision_kind: DecisionKind
    status: DecisionStatus = "pending"
    reason_code: DecisionReasonCode
    created_by_kind: DecisionProducerKind
    producer_key: str = Field(min_length=1, max_length=MAX_DECISION_STRING_LENGTH)
    producer_version: str = Field(min_length=1, max_length=MAX_DECISION_STRING_LENGTH)
    created_at: datetime
    proposal: DecisionProposalV1

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return strict_datetime(value, field="created_at")

    @field_validator("decision_fingerprint", mode="before")
    @classmethod
    def validate_fingerprint(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError("decision_fingerprint must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("producer_key", mode="before")
    @classmethod
    def normalize_producer_key(cls, value: Any) -> str:
        return _bounded_producer_key(value)

    @field_validator("producer_version", mode="before")
    @classmethod
    def normalize_producer_version(cls, value: Any) -> str:
        return _bounded_string(value, field="producer_version")

    @model_validator(mode="after")
    def validate_projection(self) -> ClaimDecisionProjectionV1:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include an explicit timezone")
        if self.decision_kind == "mapping_candidate" and not isinstance(self.proposal, MappingCandidateProposalV1):
            raise ValueError("mapping_candidate requires a mapping proposal")
        if self.decision_kind == "schema_extension_candidate" and not isinstance(
            self.proposal, SchemaExtensionCandidateProposalV1
        ):
            raise ValueError("schema_extension_candidate requires a schema extension proposal")
        allowed_reasons = (
            MAPPING_DECISION_REASONS
            if self.decision_kind == "mapping_candidate"
            else SCHEMA_EXTENSION_DECISION_REASONS
        )
        if self.reason_code not in allowed_reasons:
            raise ValueError("decision reason_code is incompatible with decision_kind")
        if self.reason_code == "unknown_direction" and self.proposal.surface_direction != "unknown":
            raise ValueError("unknown_direction requires an unknown surface_direction")
        proposal_json = json.dumps(
            self.proposal.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(proposal_json.encode("utf-8")) > MAX_DECISION_PROPOSAL_BYTES:
            raise ValueError("decision proposal exceeds the bounded size limit")
        expected = claim_decision_fingerprint(self)
        if self.decision_fingerprint not in (None, expected):
            raise ValueError("decision_fingerprint does not match the immutable projection")
        object.__setattr__(self, "decision_fingerprint", expected)
        return self


def canonical_claim_decision_json(value: ClaimDecisionProjectionV1 | Mapping[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, ClaimDecisionProjectionV1) else value
    projection = ClaimDecisionProjectionV1.model_validate(payload)
    payload = projection.model_dump(mode="json")
    # PostgreSQL stores timestamptz as an instant and returns it in UTC.  Make
    # the typed boundary use the same representation for non-UTC inputs.
    payload["created_at"] = canonical_datetime_string(projection.created_at, field="created_at")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
