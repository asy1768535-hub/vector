"""Pure M4 quarantine/sidecar projection for validated canonical mappings.

The existing ``GraphExtractionPayload`` cannot retain canonical mapping
authority or direct entity references.  This module therefore stops at a
typed, DB-free sidecar.  It never stages candidates or calls materialization
or publication code.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.schemas.canonical_mapping import (
    CanonicalDirectionV1,
    CanonicalEndpointV1,
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    EndpointTransform,
    EvidenceValidationBindingV1,
    MappingOutcome,
    PredicateTransform,
    canonical_mapping_input_json,
    canonical_mapping_json,
    canonical_mapping_json_value,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GENERIC_FALLBACK_RELATION_KEY = "related_to"
DEFAULT_CANONICAL_MAPPING_BRIDGE_MIN_CONFIDENCE = 0.85


class _BridgeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )


class CanonicalMappingBridgeEffect(str, Enum):
    DISABLED = "disabled"
    QUARANTINED = "quarantined"
    PROJECTED = "projected"


class CanonicalMappingBridgeReason(str, Enum):
    BRIDGE_DISABLED = "bridge_disabled"
    AMBIGUOUS_RESULT = "ambiguous_mapping_result"
    BLOCKED_RESULT = "blocked_mapping_result"
    REJECTED_RESULT = "rejected_mapping_result"
    FALLBACK_RELATION = "fallback_relation_not_allowed"
    LOW_CONFIDENCE = "mapping_confidence_below_threshold"


class CanonicalMappingBridgeErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_AUTHORITY = "invalid_authority"
    PROJECTION_ERROR = "projection_error"


class CanonicalMappingBridgeStage(str, Enum):
    ARGUMENTS = "arguments"
    INPUT_AUTHORITY = "input_authority"
    RESULT_AUTHORITY = "result_authority"
    PROJECTION = "projection"


class CanonicalMappingBridgeError(ValueError):
    """Machine-readable bridge rejection without source payload text."""

    def __init__(
        self,
        *,
        code: CanonicalMappingBridgeErrorCode,
        stage: CanonicalMappingBridgeStage,
    ) -> None:
        self.code = code
        self.stage = stage
        super().__init__(code.value)


class CanonicalMappingBridgeConfigV1(_BridgeModel):
    enabled: StrictBool = False
    minimum_mapping_confidence: float = DEFAULT_CANONICAL_MAPPING_BRIDGE_MIN_CONFIDENCE

    @field_validator("minimum_mapping_confidence", mode="before")
    @classmethod
    def validate_minimum_confidence(cls, value: Any) -> float:
        if type(value) not in (int, float):
            raise ValueError("minimum_mapping_confidence must be a native number")
        normalized = float(value)
        if not math.isfinite(normalized) or not 0 <= normalized <= 1:
            raise ValueError("minimum_mapping_confidence must be finite and between zero and one")
        return normalized


class CanonicalCandidateEndpointReferenceV1(_BridgeModel):
    role: Literal["source", "target"]
    mention_local_id: str = Field(min_length=1, max_length=128)
    entity_type_key: str = Field(min_length=1, max_length=128)
    reference_kind: Literal["entity", "entity_candidate"]
    entity_id: UUID | None = None
    entity_candidate_key_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    endpoint_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    link_decision_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    resolution_attestation_fingerprint: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_reference_shape(self) -> CanonicalCandidateEndpointReferenceV1:
        if self.reference_kind == "entity":
            if self.entity_id is None or self.entity_candidate_key_hash is not None:
                raise ValueError("entity endpoint requires only an entity id")
        elif self.entity_candidate_key_hash is None or self.entity_id is not None:
            raise ValueError("entity candidate endpoint requires only a candidate key hash")
        if len(self.evidence_ref_ids) != len(set(self.evidence_ref_ids)):
            raise ValueError("endpoint evidence references must be unique")
        return self


class CanonicalMappingCandidateSidecarV1(_BridgeModel):
    schema_version: Literal["canonical_mapping_candidate_sidecar_v1"] = (
        "canonical_mapping_candidate_sidecar_v1"
    )
    mapping_result_id: UUID
    mapping_result_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    relation_type_key: str = Field(min_length=1, max_length=128)
    direction: CanonicalDirectionV1
    endpoint_transform: EndpointTransform
    predicate_transform: PredicateTransform
    source_endpoint: CanonicalCandidateEndpointReferenceV1
    target_endpoint: CanonicalCandidateEndpointReferenceV1
    mapping_confidence: float = Field(ge=0, le=1)
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_bindings: tuple[EvidenceValidationBindingV1, ...] = Field(min_length=1, max_length=32)
    authoritative_input_json: str = Field(min_length=2, max_length=131_072)
    mapping_result_json: str = Field(min_length=2, max_length=131_072)

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> CanonicalMappingCandidateSidecarV1:
        if self.source_endpoint.role != "source" or self.target_endpoint.role != "target":
            raise ValueError("candidate endpoint roles must remain source and target")
        binding_ids = {binding.evidence_ref_id for binding in self.evidence_bindings}
        if len(binding_ids) != len(self.evidence_bindings) or binding_ids != set(self.evidence_ref_ids):
            raise ValueError("candidate evidence must match the verified typed bindings")
        return self


class CanonicalMappingBridgeResultV1(_BridgeModel):
    effect: CanonicalMappingBridgeEffect
    reason_code: CanonicalMappingBridgeReason | None = None
    mapping_outcome: MappingOutcome
    mapping_result_id: UUID
    mapping_result_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    sidecar: CanonicalMappingCandidateSidecarV1 | None = None

    @model_validator(mode="after")
    def validate_effect_shape(self) -> CanonicalMappingBridgeResultV1:
        if self.effect == CanonicalMappingBridgeEffect.PROJECTED:
            if self.mapping_outcome != "mapped" or self.reason_code is not None or self.sidecar is None:
                raise ValueError("projected bridge result requires one mapped sidecar")
        elif self.sidecar is not None or self.reason_code is None:
            raise ValueError("disabled or quarantined bridge result cannot contain a sidecar")
        return self


_DEFAULT_CONFIG = CanonicalMappingBridgeConfigV1()


def _bridge_error(
    *,
    code: CanonicalMappingBridgeErrorCode,
    stage: CanonicalMappingBridgeStage,
    cause: Exception | None = None,
) -> None:
    error = CanonicalMappingBridgeError(code=code, stage=stage)
    if cause is None:
        raise error
    raise error from cause


def _validated_config(value: CanonicalMappingBridgeConfigV1) -> CanonicalMappingBridgeConfigV1:
    if not isinstance(value, CanonicalMappingBridgeConfigV1):
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_ARGUMENT,
            stage=CanonicalMappingBridgeStage.ARGUMENTS,
        )
    if type(value.enabled) is not bool or type(value.minimum_mapping_confidence) not in (int, float):
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_ARGUMENT,
            stage=CanonicalMappingBridgeStage.ARGUMENTS,
        )
    try:
        return CanonicalMappingBridgeConfigV1.model_validate_json(
            canonical_mapping_json_value(value.model_dump(mode="json"))
        )
    except Exception as exc:
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_ARGUMENT,
            stage=CanonicalMappingBridgeStage.ARGUMENTS,
            cause=exc,
        )


def _validated_authority(
    authoritative_input: CanonicalMappingInputV1,
    mapping_result: CanonicalMappingV1,
) -> tuple[CanonicalMappingInputV1, CanonicalMappingV1, str, str]:
    if not isinstance(authoritative_input, CanonicalMappingInputV1) or not isinstance(
        mapping_result, CanonicalMappingV1
    ):
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_ARGUMENT,
            stage=CanonicalMappingBridgeStage.ARGUMENTS,
        )
    try:
        input_json = canonical_mapping_input_json(authoritative_input)
        input_projection = CanonicalMappingInputV1.model_validate_json(input_json)
    except Exception as exc:
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_AUTHORITY,
            stage=CanonicalMappingBridgeStage.INPUT_AUTHORITY,
            cause=exc,
        )
    try:
        result_json = canonical_mapping_json(authoritative_input, mapping_result)
        result_projection = CanonicalMappingV1.model_validate_json(result_json)
    except Exception as exc:
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.INVALID_AUTHORITY,
            stage=CanonicalMappingBridgeStage.RESULT_AUTHORITY,
            cause=exc,
        )
    return input_projection, result_projection, input_json, result_json


def _mapped_gate_reason(
    result: CanonicalMappingV1,
    *,
    minimum_confidence: float,
) -> CanonicalMappingBridgeReason | None:
    # canonical_mapping_json already re-runs the complete M0 input-aware
    # ontology, endpoint, evidence, scope, direction, and authorization gates.
    # M4 adds only the two bridge-specific policy checks below.
    if result.canonical_relation_key == _GENERIC_FALLBACK_RELATION_KEY:
        return CanonicalMappingBridgeReason.FALLBACK_RELATION
    if result.mapping_confidence is None or result.mapping_confidence < minimum_confidence:
        return CanonicalMappingBridgeReason.LOW_CONFIDENCE
    return None


def _endpoint_reference(endpoint: CanonicalEndpointV1) -> CanonicalCandidateEndpointReferenceV1:
    link = endpoint.entity_link
    reference_kind: Literal["entity", "entity_candidate"] = (
        "entity" if link.status == "resolved" else "entity_candidate"
    )
    return CanonicalCandidateEndpointReferenceV1(
        role=endpoint.role,
        mention_local_id=endpoint.mention_local_id,
        entity_type_key=endpoint.entity_type_key,
        reference_kind=reference_kind,
        entity_id=link.entity_id,
        entity_candidate_key_hash=link.entity_candidate_key_hash,
        evidence_ref_ids=endpoint.resolution_attestation.evidence_ref_ids,
        endpoint_fingerprint=endpoint.endpoint_fingerprint,
        link_decision_fingerprint=link.link_decision_fingerprint,
        resolution_attestation_fingerprint=endpoint.resolution_attestation.attestation_fingerprint,
    )


def _result_without_sidecar(
    result: CanonicalMappingV1,
    *,
    effect: CanonicalMappingBridgeEffect,
    reason: CanonicalMappingBridgeReason,
) -> CanonicalMappingBridgeResultV1:
    if result.mapping_result_id is None or result.mapping_result_fingerprint is None:
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.PROJECTION_ERROR,
            stage=CanonicalMappingBridgeStage.PROJECTION,
        )
    return CanonicalMappingBridgeResultV1(
        effect=effect,
        reason_code=reason,
        mapping_outcome=result.outcome,
        mapping_result_id=result.mapping_result_id,
        mapping_result_fingerprint=result.mapping_result_fingerprint,
    )


def project_canonical_mapping_to_candidate_sidecar(
    authoritative_input: CanonicalMappingInputV1,
    mapping_result: CanonicalMappingV1,
    *,
    config: CanonicalMappingBridgeConfigV1 = _DEFAULT_CONFIG,
) -> CanonicalMappingBridgeResultV1:
    """Return a deterministic sidecar or quarantine result without writes."""
    validated_config = _validated_config(config)
    _, result, input_json, result_json = _validated_authority(
        authoritative_input,
        mapping_result,
    )

    if not validated_config.enabled:
        return _result_without_sidecar(
            result,
            effect=CanonicalMappingBridgeEffect.DISABLED,
            reason=CanonicalMappingBridgeReason.BRIDGE_DISABLED,
        )

    non_mapped_reasons = {
        "ambiguous": CanonicalMappingBridgeReason.AMBIGUOUS_RESULT,
        "blocked": CanonicalMappingBridgeReason.BLOCKED_RESULT,
        "rejected": CanonicalMappingBridgeReason.REJECTED_RESULT,
    }
    if result.outcome != "mapped":
        return _result_without_sidecar(
            result,
            effect=CanonicalMappingBridgeEffect.QUARANTINED,
            reason=non_mapped_reasons[result.outcome],
        )

    gate_reason = _mapped_gate_reason(
        result,
        minimum_confidence=validated_config.minimum_mapping_confidence,
    )
    if gate_reason is not None:
        return _result_without_sidecar(
            result,
            effect=CanonicalMappingBridgeEffect.QUARANTINED,
            reason=gate_reason,
        )

    source_endpoint = result.canonical_source_endpoint
    target_endpoint = result.canonical_target_endpoint
    if (
        source_endpoint is None
        or target_endpoint is None
        or result.canonical_relation_key is None
        or result.canonical_direction is None
        or result.endpoint_transform is None
        or result.predicate_transform is None
        or result.mapping_confidence is None
        or result.mapping_result_id is None
        or result.mapping_result_fingerprint is None
    ):
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.PROJECTION_ERROR,
            stage=CanonicalMappingBridgeStage.PROJECTION,
        )

    try:
        sidecar = CanonicalMappingCandidateSidecarV1(
            mapping_result_id=result.mapping_result_id,
            mapping_result_fingerprint=result.mapping_result_fingerprint,
            relation_type_key=result.canonical_relation_key,
            direction=result.canonical_direction,
            endpoint_transform=result.endpoint_transform,
            predicate_transform=result.predicate_transform,
            source_endpoint=_endpoint_reference(source_endpoint),
            target_endpoint=_endpoint_reference(target_endpoint),
            mapping_confidence=result.mapping_confidence,
            evidence_ref_ids=result.mapping_evidence_ref_ids,
            evidence_bindings=result.evidence_bindings,
            authoritative_input_json=input_json,
            mapping_result_json=result_json,
        )
        return CanonicalMappingBridgeResultV1(
            effect=CanonicalMappingBridgeEffect.PROJECTED,
            mapping_outcome=result.outcome,
            mapping_result_id=result.mapping_result_id,
            mapping_result_fingerprint=result.mapping_result_fingerprint,
            sidecar=sidecar,
        )
    except CanonicalMappingBridgeError:
        raise
    except Exception as exc:
        _bridge_error(
            code=CanonicalMappingBridgeErrorCode.PROJECTION_ERROR,
            stage=CanonicalMappingBridgeStage.PROJECTION,
            cause=exc,
        )


__all__ = [
    "CanonicalCandidateEndpointReferenceV1",
    "CanonicalMappingBridgeConfigV1",
    "CanonicalMappingBridgeEffect",
    "CanonicalMappingBridgeError",
    "CanonicalMappingBridgeErrorCode",
    "CanonicalMappingBridgeReason",
    "CanonicalMappingBridgeResultV1",
    "CanonicalMappingBridgeStage",
    "CanonicalMappingCandidateSidecarV1",
    "DEFAULT_CANONICAL_MAPPING_BRIDGE_MIN_CONFIDENCE",
    "project_canonical_mapping_to_candidate_sidecar",
]
