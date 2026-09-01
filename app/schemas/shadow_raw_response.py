"""Provider-neutral, DB-free shadow extraction response contract."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.raw_claim import (
    MAX_EVIDENCE_REFS,
    MAX_PREDICATE_LENGTH,
    ClaimMentionV1,
    DirectionV1,
    ModalityV1,
    NegationV1,
    QualifierV1,
    TimeIntervalV1,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_REF_KEY_LENGTH = 64


class _ShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalize_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"{field} must be non-empty and bounded")
    return normalized


def _normalize_ref_key(value: object) -> str:
    return _normalize_text(value, field="evidence_ref_key", limit=_MAX_REF_KEY_LENGTH)


def _validate_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


class ShadowRawResponseV1(_ShadowModel):
    """One raw relation as explicitly returned by a shadow extractor.

    `surface_raw_predicate` is deliberately distinct from any ontology or
    canonical relation key. No canonical field is accepted by this protocol.
    """

    source_mention: ClaimMentionV1
    surface_raw_predicate: str = Field(min_length=1, max_length=MAX_PREDICATE_LENGTH)
    target_mention: ClaimMentionV1
    surface_direction: DirectionV1
    negation: NegationV1
    modality: ModalityV1
    qualifiers: tuple[QualifierV1, ...] = Field(default_factory=tuple, max_length=32)
    valid_time: TimeIntervalV1 | None = None
    effective_time: TimeIntervalV1 | None = None
    evidence_ref_keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_REFS)

    @field_validator("surface_raw_predicate", mode="before")
    @classmethod
    def normalize_surface_predicate(cls, value: object) -> str:
        return _normalize_text(value, field="surface_raw_predicate", limit=MAX_PREDICATE_LENGTH)

    @field_validator("evidence_ref_keys", mode="before")
    @classmethod
    def normalize_evidence_ref_keys(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("evidence_ref_keys must be a list or tuple")
        keys = tuple(_normalize_ref_key(item) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("evidence_ref_keys must be unique")
        return keys

    @model_validator(mode="after")
    def validate_evidence_bindings(self) -> ShadowRawResponseV1:
        if self.source_mention.local_id == self.target_mention.local_id:
            raise ValueError("source and target local IDs must be distinct")
        referenced = {
            self.source_mention.evidence_ref,
            self.target_mention.evidence_ref,
            self.negation.evidence_ref,
            self.modality.evidence_ref,
            *(qualifier.evidence_ref for qualifier in self.qualifiers),
            *(interval.evidence_ref for interval in (self.valid_time, self.effective_time) if interval),
        }
        referenced.discard(None)
        declared = set(self.evidence_ref_keys)
        if referenced != declared:
            raise ValueError("evidence_ref_keys must exactly bind all evidence-bearing fields")
        for interval in (self.valid_time, self.effective_time):
            if interval is not None and interval.evidence_ref is None:
                raise ValueError("time interval evidence_ref is required")
        return self


class ShadowExtractionProvenanceV1(_ShadowModel):
    """Explicit provenance required to turn a shadow response into RawClaimV1."""

    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: int = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    extractor_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    model_provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    model_config_hash: str
    prompt_content_hash: str
    parser_version: str = Field(min_length=1, max_length=64)
    normalization_rule_version: str = Field(min_length=1, max_length=64)
    ontology_snapshot_hash: str | None = None

    @field_validator("model_config_hash", "prompt_content_hash", "ontology_snapshot_hash", mode="before")
    @classmethod
    def validate_hashes(cls, value: object, info) -> str | None:
        if value is None and info.field_name == "ontology_snapshot_hash":
            return None
        return _validate_sha256(value, field=info.field_name)


ShadowLibraryPolicyV1 = Literal["inherit", "enabled", "disabled"]


class ShadowExtractionConfigV1(_ShadowModel):
    """Pure rollout decision contract; it has no Settings/ORM integration."""

    global_enabled: bool = False
    library_policy: ShadowLibraryPolicyV1 = "inherit"


def resolve_shadow_extraction(config: ShadowExtractionConfigV1) -> bool:
    if config.library_policy == "enabled":
        return True
    if config.library_policy == "disabled":
        return False
    return config.global_enabled
