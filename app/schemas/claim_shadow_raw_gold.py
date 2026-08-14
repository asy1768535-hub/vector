"""Independent, hash-only raw gold annotation contract for M4B2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.schemas.claim_shadow_replay_artifact import (
    RedactedRawClaimV2,
    claim_shadow_v2_hash_json,
)


class _RawGoldModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawGoldAnnotationV1(_RawGoldModel):
    gold_claim_id_sha256: str
    claim: RedactedRawClaimV2
    unknown_predicate: StrictBool
    unknown_source_endpoint: StrictBool
    unknown_target_endpoint: StrictBool

    @field_validator("gold_claim_id_sha256")
    @classmethod
    def validate_gold_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("gold_claim_id_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def validate_unknown_endpoint_annotations(self) -> RawGoldAnnotationV1:
        if self.unknown_source_endpoint == self.claim.source_type_hint_present:
            raise ValueError("unknown_source_endpoint conflicts with source type-hint presence")
        if self.unknown_target_endpoint == self.claim.target_type_hint_present:
            raise ValueError("unknown_target_endpoint conflicts with target type-hint presence")
        return self


class RawGoldFixtureV1(_RawGoldModel):
    schema_version: Literal["claim_shadow_raw_gold_v1"] = "claim_shadow_raw_gold_v1"
    fixture_id_sha256: str
    annotations: tuple[RawGoldAnnotationV1, ...] = Field(max_length=4096)

    @field_validator("fixture_id_sha256")
    @classmethod
    def validate_fixture_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("fixture_id_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def reject_ambiguous_anchors(self) -> RawGoldFixtureV1:
        anchors = {
            raw_gold_anchor_v1(annotation.claim)
            for annotation in self.annotations
        }
        if len(anchors) != len(self.annotations):
            raise ValueError("raw gold fixture contains duplicate or ambiguous anchors")
        return self


def raw_gold_anchor_v1(claim: RedactedRawClaimV2) -> str:
    """Anchor only on scope and evidence identity bindings, never scored fields."""

    evidence_ids = sorted(item.evidence_ref_sha256 for item in claim.evidence_refs)
    return claim_shadow_v2_hash_json(
        "raw_gold_anchor_v1",
        {"scope_id": claim.scope_id, "evidence_ref_sha256": evidence_ids},
    )


def validate_raw_gold_fixture(value: RawGoldFixtureV1 | dict[str, Any]) -> RawGoldFixtureV1:
    return value if isinstance(value, RawGoldFixtureV1) else RawGoldFixtureV1.model_validate(value)

