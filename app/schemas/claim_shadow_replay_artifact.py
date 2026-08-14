"""DB-free, redacted Claim Shadow Replay Artifact v1 contract.

The artifact intentionally stores hashes, bounded enums, counts, and version
metadata only.  It is independent from the existing graph-discovery eval
artifacts and must not become a second raw-claim scorer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.schemas._strict_datetime import canonical_datetime_string, strict_datetime


ArtifactSchemaVersion = Literal["claim_shadow_replay_artifact_v1"]
ArtifactStatus = Literal["success", "failed"]
Direction = Literal["source_to_target", "target_to_source", "undirected", "unknown"]
DecisionKind = Literal["mapping_candidate", "schema_extension_candidate"]
DecisionStatus = Literal["pending"]

MAX_SCOPES = 32
MAX_RECORDS = 4096
MAX_COMPLETED_STAGES = 32
MAX_COUNT_ENTRIES = 128
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TOKEN = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _safe_token(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a bounded stable token")
    return value


def _safe_token_tuple(value: Any, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError(f"{field} exceeds its bound")
    result = tuple(_safe_token(item, field=field) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _sha_tuple(value: Any, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError(f"{field} exceeds its bound")
    result = tuple(_sha256(item, field=field) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def scope_id_for(
    *,
    library_id_sha256: str,
    document_id_sha256: str,
    revision_id_sha256: str,
    revision_no: int,
) -> str:
    """Compute the stable, non-reversible scope identity used by records."""

    material = {
        "document_id_sha256": _sha256(document_id_sha256, field="document_id_sha256"),
        "library_id_sha256": _sha256(library_id_sha256, field="library_id_sha256"),
        "revision_id_sha256": _sha256(revision_id_sha256, field="revision_id_sha256"),
        "revision_no": revision_no,
    }
    if isinstance(revision_no, bool) or not isinstance(revision_no, int) or revision_no < 1:
        raise ValueError("revision_no must be a positive integer")
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


class ReplayScopeV1(_ArtifactModel):
    scope_id: str
    library_id_sha256: str
    document_id_sha256: str
    revision_id_sha256: str
    revision_no: StrictInt = Field(ge=1)

    @field_validator("scope_id", "library_id_sha256", "document_id_sha256", "revision_id_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_scope(self) -> ReplayScopeV1:
        expected = scope_id_for(
            library_id_sha256=self.library_id_sha256,
            document_id_sha256=self.document_id_sha256,
            revision_id_sha256=self.revision_id_sha256,
            revision_no=self.revision_no,
        )
        if self.scope_id != expected:
            raise ValueError("scope_id does not match its revision scope")
        return self


class ReplayCountV1(_ArtifactModel):
    key: str = Field(min_length=1, max_length=MAX_TOKEN)
    count: StrictInt = Field(ge=0)

    @field_validator("key", mode="before")
    @classmethod
    def validate_key(cls, value: Any) -> str:
        return _safe_token(value, field="count key")


class ReplayMetricsV1(_ArtifactModel):
    input_token_count: StrictInt = Field(ge=0)
    output_token_count: StrictInt = Field(ge=0)
    latency_ms: StrictInt = Field(ge=0)
    provider_call_count: StrictInt = Field(ge=0)
    raw_claim_count: StrictInt = Field(ge=0)
    occurrence_count: StrictInt = Field(ge=0)
    decision_count: StrictInt = Field(ge=0)


def _validate_success_metric_counts(
    metrics: ReplayMetricsV1,
    *,
    unique_core_count: int,
    occurrence_count: int,
) -> None:
    """Validate observed raw counts separately from redacted unique cores."""
    if metrics.raw_claim_count < unique_core_count:
        raise ValueError("raw_claim_count cannot be smaller than unique raw claim cores")
    if metrics.occurrence_count != occurrence_count:
        raise ValueError("metrics occurrence_count does not match occurrence records")
    if occurrence_count > 0 and metrics.raw_claim_count != metrics.occurrence_count:
        raise ValueError("observed raw claim count must match occurrence count when occurrences are present")


class ReplayFailureV1(_ArtifactModel):
    stage: str = Field(min_length=1, max_length=MAX_TOKEN)
    error_code: str = Field(min_length=1, max_length=MAX_TOKEN)
    finish_reason: str | None = Field(default=None, max_length=MAX_TOKEN)
    response_sha256: str | None = None
    observed_claim_count: StrictInt = Field(default=0, ge=0)
    truncated: StrictBool = False

    @field_validator("stage", "error_code", "finish_reason", mode="before")
    @classmethod
    def validate_tokens(cls, value: Any, info) -> str | None:
        return _safe_token(value, field=info.field_name) if value is not None else None

    @field_validator("response_sha256")
    @classmethod
    def validate_response_hash(cls, value: str | None) -> str | None:
        return _sha256(value, field="response_sha256") if value is not None else None


class RedactedRawClaimV1(_ArtifactModel):
    scope_id: str
    claim_id_sha256: str
    content_fingerprint: str
    raw_predicate_sha256: str
    source_mention_id_sha256: str
    target_mention_id_sha256: str
    direction: Direction
    evidence_ref_count: StrictInt = Field(ge=1, le=16)
    evidence_ref_ids_sha256: tuple[str, ...] = Field(min_length=1, max_length=16)
    qualifier_count: StrictInt = Field(ge=0, le=32)
    has_effective_time: StrictBool = False

    @field_validator(
        "scope_id",
        "claim_id_sha256",
        "content_fingerprint",
        "raw_predicate_sha256",
        "source_mention_id_sha256",
        "target_mention_id_sha256",
    )
    @classmethod
    def validate_hash_fields(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator("evidence_ref_ids_sha256", mode="before")
    @classmethod
    def validate_refs(cls, value: Any) -> tuple[str, ...]:
        return _sha_tuple(value, field="evidence_ref_ids_sha256", maximum=16)

    @model_validator(mode="after")
    def validate_ref_count(self) -> RedactedRawClaimV1:
        if self.evidence_ref_count != len(self.evidence_ref_ids_sha256):
            raise ValueError("evidence_ref_count does not match evidence_ref_ids_sha256")
        return self


class RedactedOccurrenceV1(_ArtifactModel):
    scope_id: str
    occurrence_id_sha256: str
    claim_id_sha256: str
    occurrence_fingerprint: str
    extractor_version: str
    parser_version: str
    model_version: str
    prompt_version: str
    config_version: str

    @field_validator("scope_id", "occurrence_id_sha256", "claim_id_sha256", "occurrence_fingerprint")
    @classmethod
    def validate_hash_fields(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator(
        "extractor_version",
        "parser_version",
        "model_version",
        "prompt_version",
        "config_version",
        mode="before",
    )
    @classmethod
    def validate_versions(cls, value: Any, info) -> str:
        return _safe_token(value, field=info.field_name)


class RedactedDecisionV1(_ArtifactModel):
    scope_id: str
    decision_id_sha256: str
    claim_id_sha256: str
    occurrence_id_sha256: str | None = None
    decision_fingerprint: str
    decision_kind: DecisionKind
    status: DecisionStatus
    reason_code: str

    @field_validator(
        "scope_id",
        "decision_id_sha256",
        "claim_id_sha256",
        "occurrence_id_sha256",
        "decision_fingerprint",
        mode="before",
    )
    @classmethod
    def validate_hash_fields(cls, value: Any, info) -> str | None:
        return _sha256(value, field=info.field_name) if value is not None else None

    @field_validator("reason_code", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str:
        return _safe_token(value, field="reason_code")


class ClaimShadowReplayArtifactV1(_ArtifactModel):
    schema_version: ArtifactSchemaVersion = "claim_shadow_replay_artifact_v1"
    artifact_id_sha256: str
    run_id_sha256: str
    provider_key_sha256: str
    model_key_sha256: str
    config_sha256: str
    prompt_sha256: str
    artifact_producer_version: str
    schema_producer_version: str
    projection_producer_version: str
    created_at: datetime
    status: ArtifactStatus
    completed_stages: tuple[str, ...] = Field(max_length=MAX_COMPLETED_STAGES)
    scopes: tuple[ReplayScopeV1, ...] = Field(max_length=MAX_SCOPES)
    raw_claims: tuple[RedactedRawClaimV1, ...] = Field(max_length=MAX_RECORDS)
    occurrences: tuple[RedactedOccurrenceV1, ...] = Field(max_length=MAX_RECORDS)
    decisions: tuple[RedactedDecisionV1, ...] = Field(max_length=MAX_RECORDS)
    aggregate_counts: tuple[ReplayCountV1, ...] = Field(max_length=MAX_COUNT_ENTRIES)
    metrics: ReplayMetricsV1 | None = None
    failure: ReplayFailureV1 | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return strict_datetime(value, field="created_at")

    @field_validator(
        "artifact_id_sha256",
        "run_id_sha256",
        "provider_key_sha256",
        "model_key_sha256",
        "config_sha256",
        "prompt_sha256",
    )
    @classmethod
    def validate_artifact_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator(
        "artifact_producer_version",
        "schema_producer_version",
        "projection_producer_version",
        mode="before",
    )
    @classmethod
    def validate_artifact_versions(cls, value: Any, info) -> str:
        return _safe_token(value, field=info.field_name)

    @field_validator("completed_stages", mode="before")
    @classmethod
    def validate_stages(cls, value: Any) -> tuple[str, ...]:
        return _safe_token_tuple(value, field="completed_stages", maximum=MAX_COMPLETED_STAGES)

    @model_validator(mode="after")
    def validate_artifact(self) -> ClaimShadowReplayArtifactV1:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include an explicit timezone")
        if self.status == "success" and (self.metrics is None or self.failure is not None):
            raise ValueError("success artifacts require metrics and no failure")
        if self.status == "failed" and (self.metrics is not None or self.failure is None):
            raise ValueError("failed artifacts require metrics=null and failure metadata")

        scope_map = {scope.scope_id: scope for scope in self.scopes}
        if len(scope_map) != len(self.scopes):
            raise ValueError("scopes must be unique")
        claims = {claim.claim_id_sha256: claim for claim in self.raw_claims}
        if len(claims) != len(self.raw_claims):
            raise ValueError("claim records must be unique")
        occurrences = {row.occurrence_id_sha256: row for row in self.occurrences}
        if len(occurrences) != len(self.occurrences):
            raise ValueError("occurrence records must be unique")
        decisions = {row.decision_id_sha256: row for row in self.decisions}
        if len(decisions) != len(self.decisions):
            raise ValueError("decision records must be unique")
        for claim in self.raw_claims:
            if claim.scope_id not in scope_map:
                raise ValueError("claim record references an unknown scope")
        for occurrence in self.occurrences:
            claim = claims.get(occurrence.claim_id_sha256)
            if occurrence.scope_id not in scope_map or claim is None:
                raise ValueError("occurrence record references an unknown claim or scope")
            if claim.scope_id != occurrence.scope_id:
                raise ValueError("occurrence crosses claim revision scope")
        for decision in self.decisions:
            claim = claims.get(decision.claim_id_sha256)
            if decision.scope_id not in scope_map or claim is None:
                raise ValueError("decision record references an unknown claim or scope")
            if claim.scope_id != decision.scope_id:
                raise ValueError("decision crosses claim revision scope")
            if decision.occurrence_id_sha256 is not None:
                occurrence = occurrences.get(decision.occurrence_id_sha256)
                if occurrence is None or occurrence.scope_id != decision.scope_id:
                    raise ValueError("decision occurrence crosses claim revision scope")
        return self


ClaimShadowReplayArtifactSchemaVersionV2 = Literal["claim_shadow_replay_artifact_v2"]


def _v2_hash_text(kind: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{kind} must be text")
    material = f"claim-shadow-replay-v2:{kind}:utf8:{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _v2_hash_json(kind: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _v2_hash_text(kind, encoded)


def claim_shadow_v2_hash_text(kind: str, value: str) -> str:
    """Hash a redacted v2 text value using the versioned input specification."""

    return _v2_hash_text(kind, value)


def claim_shadow_v2_hash_json(kind: str, value: Any) -> str:
    """Hash a redacted v2 JSON value using the versioned input specification."""

    return _v2_hash_json(kind, value)


class RedactedQualifierV2(_ArtifactModel):
    key_sha256: str
    value_sha256: str
    evidence_ref_sha256: str | None = None

    @field_validator("key_sha256", "value_sha256", "evidence_ref_sha256")
    @classmethod
    def validate_hashes(cls, value: str | None, info) -> str | None:
        return _sha256(value, field=info.field_name) if value is not None else None


class RedactedTimeIntervalV2(_ArtifactModel):
    start_sha256: str | None = None
    end_sha256: str | None = None
    evidence_ref_sha256: str | None = None

    @field_validator("start_sha256", "end_sha256", "evidence_ref_sha256")
    @classmethod
    def validate_hashes(cls, value: str | None, info) -> str | None:
        return _sha256(value, field=info.field_name) if value is not None else None


class RedactedEvidenceRefV2(_ArtifactModel):
    scope_id: str
    evidence_ref_sha256: str
    quote_sha256: str
    unit_text_sha256: str
    locator_present: StrictBool
    locator_valid: StrictBool
    source_span_valid: StrictBool

    @field_validator("scope_id", "evidence_ref_sha256", "quote_sha256", "unit_text_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)


class RedactedRawClaimV2(_ArtifactModel):
    scope_id: str
    claim_id_sha256: str
    content_fingerprint: str
    raw_predicate_sha256: str
    source_mention_id_sha256: str
    source_surface_sha256: str
    source_type_hint_present: StrictBool
    source_type_hint_sha256: str | None = None
    source_evidence_ref_sha256: str
    target_mention_id_sha256: str
    target_surface_sha256: str
    target_type_hint_present: StrictBool
    target_type_hint_sha256: str | None = None
    target_evidence_ref_sha256: str
    direction: Direction
    negation: StrictBool
    negation_evidence_ref_sha256: str | None = None
    modality_value_sha256: str | None = None
    modality_evidence_ref_sha256: str | None = None
    qualifiers: tuple[RedactedQualifierV2, ...] = Field(max_length=32)
    valid_time: RedactedTimeIntervalV2 | None = None
    effective_time: RedactedTimeIntervalV2 | None = None
    evidence_refs: tuple[RedactedEvidenceRefV2, ...] = Field(min_length=1, max_length=16)

    @field_validator(
        "scope_id",
        "claim_id_sha256",
        "content_fingerprint",
        "raw_predicate_sha256",
        "source_mention_id_sha256",
        "source_surface_sha256",
        "source_evidence_ref_sha256",
        "target_mention_id_sha256",
        "target_surface_sha256",
        "target_evidence_ref_sha256",
        "source_type_hint_sha256",
        "target_type_hint_sha256",
        "modality_value_sha256",
        "negation_evidence_ref_sha256",
        "modality_evidence_ref_sha256",
    )
    @classmethod
    def validate_hashes(cls, value: str | None, info) -> str | None:
        return _sha256(value, field=info.field_name) if value is not None else None

    @model_validator(mode="after")
    def validate_presence_and_refs(self) -> RedactedRawClaimV2:
        if self.source_type_hint_present != (self.source_type_hint_sha256 is not None):
            raise ValueError("source type hint presence does not match its hash")
        if self.target_type_hint_present != (self.target_type_hint_sha256 is not None):
            raise ValueError("target type hint presence does not match its hash")
        evidence_refs = [item.evidence_ref_sha256 for item in self.evidence_refs]
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("v2 evidence references must be unique")
        evidence_set = set(evidence_refs)
        if any(item.scope_id != self.scope_id for item in self.evidence_refs):
            raise ValueError("v2 evidence reference crosses revision scope")
        bound_refs = {
            self.source_evidence_ref_sha256,
            self.target_evidence_ref_sha256,
            *(
                value
                for value in (
                    self.negation_evidence_ref_sha256,
                    self.modality_evidence_ref_sha256,
                )
                if value is not None
            ),
            *(item.evidence_ref_sha256 for item in self.qualifiers if item.evidence_ref_sha256 is not None),
            *(item.evidence_ref_sha256 for item in (self.valid_time, self.effective_time) if item is not None),
        }
        if not bound_refs.issubset(evidence_set):
            raise ValueError("v2 claim contains a dangling evidence reference")
        return self


class ClaimShadowReplayArtifactV2(_ArtifactModel):
    schema_version: ClaimShadowReplayArtifactSchemaVersionV2 = "claim_shadow_replay_artifact_v2"
    artifact_id_sha256: str
    run_id_sha256: str
    provider_key_sha256: str
    model_key_sha256: str
    config_sha256: str
    prompt_sha256: str
    artifact_producer_version: str
    schema_producer_version: str
    projection_producer_version: str
    created_at: datetime
    status: ArtifactStatus
    completed_stages: tuple[str, ...] = Field(max_length=MAX_COMPLETED_STAGES)
    scopes: tuple[ReplayScopeV1, ...] = Field(max_length=MAX_SCOPES)
    raw_claims: tuple[RedactedRawClaimV2, ...] = Field(max_length=MAX_RECORDS)
    occurrences: tuple[RedactedOccurrenceV1, ...] = Field(max_length=MAX_RECORDS)
    decisions: tuple[RedactedDecisionV1, ...] = Field(max_length=MAX_RECORDS)
    aggregate_counts: tuple[ReplayCountV1, ...] = Field(max_length=MAX_COUNT_ENTRIES)
    metrics: ReplayMetricsV1 | None = None
    failure: ReplayFailureV1 | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return strict_datetime(value, field="created_at")

    @field_validator(
        "artifact_id_sha256",
        "run_id_sha256",
        "provider_key_sha256",
        "model_key_sha256",
        "config_sha256",
        "prompt_sha256",
    )
    @classmethod
    def validate_artifact_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator(
        "artifact_producer_version",
        "schema_producer_version",
        "projection_producer_version",
        mode="before",
    )
    @classmethod
    def validate_artifact_versions(cls, value: Any, info) -> str:
        return _safe_token(value, field=info.field_name)

    @field_validator("completed_stages", mode="before")
    @classmethod
    def validate_stages(cls, value: Any) -> tuple[str, ...]:
        return _safe_token_tuple(value, field="completed_stages", maximum=MAX_COMPLETED_STAGES)

    @model_validator(mode="after")
    def validate_artifact(self) -> ClaimShadowReplayArtifactV2:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include an explicit timezone")
        if self.status == "success" and (self.metrics is None or self.failure is not None):
            raise ValueError("success artifacts require metrics and no failure")
        if self.status == "failed" and (self.metrics is not None or self.failure is None):
            raise ValueError("failed artifacts require metrics=null and failure metadata")

        scope_map = {scope.scope_id: scope for scope in self.scopes}
        if len(scope_map) != len(self.scopes):
            raise ValueError("scopes must be unique")
        claims = {claim.claim_id_sha256: claim for claim in self.raw_claims}
        if len(claims) != len(self.raw_claims):
            raise ValueError("claim records must be unique")
        occurrences = {row.occurrence_id_sha256: row for row in self.occurrences}
        if len(occurrences) != len(self.occurrences):
            raise ValueError("occurrence records must be unique")
        decisions = {row.decision_id_sha256: row for row in self.decisions}
        if len(decisions) != len(self.decisions):
            raise ValueError("decision records must be unique")
        if self.status == "success":
            _validate_success_metric_counts(
                self.metrics,
                unique_core_count=len(claims),
                occurrence_count=len(occurrences),
            )

        for claim in self.raw_claims:
            if claim.scope_id not in scope_map:
                raise ValueError("claim record references an unknown scope")
        for occurrence in self.occurrences:
            claim = claims.get(occurrence.claim_id_sha256)
            if occurrence.scope_id not in scope_map or claim is None:
                raise ValueError("occurrence record references an unknown claim or scope")
            if claim.scope_id != occurrence.scope_id:
                raise ValueError("occurrence crosses claim revision scope")
        for decision in self.decisions:
            claim = claims.get(decision.claim_id_sha256)
            if decision.scope_id not in scope_map or claim is None:
                raise ValueError("decision record references an unknown claim or scope")
            if claim.scope_id != decision.scope_id:
                raise ValueError("decision crosses claim revision scope")
            if decision.occurrence_id_sha256 is not None:
                occurrence = occurrences.get(decision.occurrence_id_sha256)
                if occurrence is None or occurrence.scope_id != decision.scope_id:
                    raise ValueError("decision occurrence crosses claim revision scope")
        return self


def canonical_claim_shadow_replay_v2_json(
    value: ClaimShadowReplayArtifactV2 | Mapping[str, Any],
) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, ClaimShadowReplayArtifactV2) else value
    artifact = ClaimShadowReplayArtifactV2.model_validate(payload)
    payload = artifact.model_dump(mode="json")
    payload["created_at"] = canonical_datetime_string(payload["created_at"], field="created_at")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("claim shadow replay v2 artifact exceeds the byte limit")
    return encoded


def validate_claim_shadow_replay_artifact_v2(
    value: ClaimShadowReplayArtifactV2 | Mapping[str, Any],
) -> ClaimShadowReplayArtifactV2:
    payload = value.model_dump(mode="json") if isinstance(value, ClaimShadowReplayArtifactV2) else value
    return ClaimShadowReplayArtifactV2.model_validate(payload)


def validate_claim_shadow_replay_artifact_any(
    value: Mapping[str, Any],
) -> ClaimShadowReplayArtifactV1 | ClaimShadowReplayArtifactV2:
    if not isinstance(value, Mapping):
        raise ValueError("artifact root must be an object")
    schema_version = value.get("schema_version")
    if schema_version in (None, "claim_shadow_replay_artifact_v1"):
        return validate_claim_shadow_replay_artifact(value)
    if schema_version == "claim_shadow_replay_artifact_v2":
        return validate_claim_shadow_replay_artifact_v2(value)
    raise ValueError("unsupported claim shadow replay artifact schema")


def replay_scope_v2_from_raw_claim(claim: Any) -> ReplayScopeV1:
    from app.schemas.raw_claim import RawClaimV1

    if not isinstance(claim, RawClaimV1):
        raise TypeError("v2 redaction requires RawClaimV1")
    library_id_sha256 = _v2_hash_text("library_id", str(claim.library_id))
    document_id_sha256 = _v2_hash_text("document_id", str(claim.document_id))
    revision_id_sha256 = _v2_hash_text("document_revision_id", str(claim.document_revision_id))
    return ReplayScopeV1(
        scope_id=scope_id_for(
            library_id_sha256=library_id_sha256,
            document_id_sha256=document_id_sha256,
            revision_id_sha256=revision_id_sha256,
            revision_no=claim.revision_no,
        ),
        library_id_sha256=library_id_sha256,
        document_id_sha256=document_id_sha256,
        revision_id_sha256=revision_id_sha256,
        revision_no=claim.revision_no,
    )


def _v2_evidence_identity(reference: Any) -> str:
    payload = {
        "evidence_id": str(reference.evidence_id),
        "document_id": str(reference.document_id),
        "document_revision_id": str(reference.document_revision_id),
        "revision_no": reference.revision_no,
        "unit_id": str(reference.unit_id),
        "chunk_id": str(reference.chunk_id) if reference.chunk_id is not None else None,
        "block_id": str(reference.block_id) if reference.block_id is not None else None,
    }
    return _v2_hash_json("evidence_identity", payload)


def _v2_time_interval(value: Any, evidence_identities: Mapping[str, str]) -> RedactedTimeIntervalV2 | None:
    if value is None:
        return None
    return RedactedTimeIntervalV2(
        start_sha256=_v2_hash_text("time_value", value.start) if value.start is not None else None,
        end_sha256=_v2_hash_text("time_value", value.end) if value.end is not None else None,
        evidence_ref_sha256=(
            evidence_identities[value.evidence_ref] if value.evidence_ref is not None else None
        ),
    )


def redact_raw_claim_v2(claim: Any) -> RedactedRawClaimV2:
    """Create the deterministic, non-text v2 record from a validated RawClaimV1."""

    from app.schemas.raw_claim import RawClaimV1, revalidate_raw_claim

    if not isinstance(claim, RawClaimV1):
        raise TypeError("v2 redaction requires RawClaimV1")
    try:
        claim = revalidate_raw_claim(claim)
    except Exception as exc:
        raise ValueError("raw claim contract invalid") from exc
    evidence_identities = {
        reference.ref_id: _v2_evidence_identity(reference) for reference in claim.evidence_refs
    }
    qualifiers = tuple(
        sorted(
            (
                RedactedQualifierV2(
                    key_sha256=_v2_hash_text("qualifier_key", qualifier.key),
                    value_sha256=_v2_hash_json("qualifier_value", qualifier.value),
                    evidence_ref_sha256=(
                        evidence_identities[qualifier.evidence_ref]
                        if qualifier.evidence_ref is not None
                        else None
                    ),
                )
                for qualifier in claim.qualifiers
            ),
            key=lambda item: json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )
    evidence_refs = tuple(
        sorted(
            (
                RedactedEvidenceRefV2(
                    scope_id=replay_scope_v2_from_raw_claim(claim).scope_id,
                    evidence_ref_sha256=evidence_identities[reference.ref_id],
                    quote_sha256=reference.quote_sha256,
                    unit_text_sha256=reference.unit_text_sha256,
                    locator_present=reference.locator is not None,
                    locator_valid=reference.locator is not None,
                    source_span_valid=reference.source_span is not None,
                )
                for reference in claim.evidence_refs
            ),
            key=lambda item: item.evidence_ref_sha256,
        )
    )
    return RedactedRawClaimV2(
        scope_id=replay_scope_v2_from_raw_claim(claim).scope_id,
        claim_id_sha256=_v2_hash_text("claim_id", str(claim.claim_id)),
        content_fingerprint=claim.content_scoped_claim_fingerprint,
        raw_predicate_sha256=_v2_hash_text("raw_predicate", claim.raw_predicate),
        source_mention_id_sha256=_v2_hash_text("mention_local_id", claim.source_mention.local_id),
        source_surface_sha256=_v2_hash_text("mention_surface", claim.source_mention.surface),
        source_type_hint_present=claim.source_mention.entity_type_hint is not None,
        source_type_hint_sha256=(
            _v2_hash_text("entity_type_hint", claim.source_mention.entity_type_hint)
            if claim.source_mention.entity_type_hint is not None
            else None
        ),
        source_evidence_ref_sha256=evidence_identities[claim.source_mention.evidence_ref],
        target_mention_id_sha256=_v2_hash_text("mention_local_id", claim.target_mention.local_id),
        target_surface_sha256=_v2_hash_text("mention_surface", claim.target_mention.surface),
        target_type_hint_present=claim.target_mention.entity_type_hint is not None,
        target_type_hint_sha256=(
            _v2_hash_text("entity_type_hint", claim.target_mention.entity_type_hint)
            if claim.target_mention.entity_type_hint is not None
            else None
        ),
        target_evidence_ref_sha256=evidence_identities[claim.target_mention.evidence_ref],
        direction=claim.surface_direction,
        negation=claim.negation.value,
        negation_evidence_ref_sha256=(
            evidence_identities[claim.negation.evidence_ref]
            if claim.negation.evidence_ref is not None
            else None
        ),
        modality_value_sha256=(
            _v2_hash_text("modality_value", claim.modality.value)
            if claim.modality.value is not None
            else None
        ),
        modality_evidence_ref_sha256=(
            evidence_identities[claim.modality.evidence_ref]
            if claim.modality.evidence_ref is not None
            else None
        ),
        qualifiers=qualifiers,
        valid_time=_v2_time_interval(claim.valid_time, evidence_identities),
        effective_time=_v2_time_interval(claim.effective_time, evidence_identities),
        evidence_refs=evidence_refs,
    )


def canonical_claim_shadow_replay_json(
    value: ClaimShadowReplayArtifactV1 | Mapping[str, Any],
) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, ClaimShadowReplayArtifactV1) else value
    artifact = ClaimShadowReplayArtifactV1.model_validate(payload)
    payload = artifact.model_dump(mode="json")
    payload["created_at"] = canonical_datetime_string(payload["created_at"], field="created_at")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("claim shadow replay artifact exceeds the byte limit")
    return encoded


def validate_claim_shadow_replay_artifact(
    value: ClaimShadowReplayArtifactV1 | Mapping[str, Any],
) -> ClaimShadowReplayArtifactV1:
    payload = value.model_dump(mode="json") if isinstance(value, ClaimShadowReplayArtifactV1) else value
    return ClaimShadowReplayArtifactV1.model_validate(payload)


def claim_shadow_replay_filename(artifact: ClaimShadowReplayArtifactV1) -> str:
    return f"claim-shadow-replay-{artifact.run_id_sha256[:16]}.json"


def write_claim_shadow_replay_artifact(
    output_dir: Path,
    artifact: ClaimShadowReplayArtifactV1 | Mapping[str, Any],
) -> Path:
    validated = validate_claim_shadow_replay_artifact(artifact)
    payload = (canonical_claim_shadow_replay_json(validated) + "\n").encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / claim_shadow_replay_filename(validated)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    return path
