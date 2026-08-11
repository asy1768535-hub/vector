"""Pure, explicit CanonicalMappingV1 builder for Phase 3 M1.

This module is deliberately a thin orchestration boundary around the M0
contract. It does not infer mappings, call a provider, mutate a RawClaim, or
connect to persistence and publication paths.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from enum import Enum
from typing import Literal

from app.schemas._strict_datetime import strict_datetime
from app.schemas.canonical_mapping import (
    CanonicalMappingInputV1,
    CanonicalMappingProposalV1,
    CanonicalMappingV1,
    MappingActorProvenanceV1,
    MappingOutcome,
    MappingRemapProvenanceV1,
    MappingSourceProvenanceV1,
    MapperProvenanceV1,
    build_canonical_mapping as build_contract_mapping,
    canonical_mapping_input_json,
    canonical_mapping_json,
    semantic_projection_from_claim,
)


class CanonicalMappingBuilderErrorCode(str, Enum):
    MAPPER_ERROR = "mapper_error"


class CanonicalMappingBuilderStage(str, Enum):
    ARGUMENTS = "arguments"
    INPUT_AUTHORITY = "input_authority"
    FACTORY = "factory"
    RESULT_SERIALIZER = "result_serializer"


class CanonicalMappingBuilderError(ValueError):
    """Machine-readable fail-closed error without input or exception text."""

    def __init__(
        self,
        *,
        stage: CanonicalMappingBuilderStage,
        code: CanonicalMappingBuilderErrorCode = CanonicalMappingBuilderErrorCode.MAPPER_ERROR,
    ) -> None:
        self.code = code
        self.stage = stage
        super().__init__(code.value)


# Compatibility alias for the first M1 test-facing name. The typed error is
# intentionally exposed under the more explicit BuilderError name above.
CanonicalMappingBuildError = CanonicalMappingBuilderError


_SEMANTIC_REASON_ORDER: tuple[tuple[str, str], ...] = (
    ("negation_value", "unsupported_negation"),
    ("modality_present", "unsupported_modality"),
    ("qualifier_present", "unsupported_qualifier"),
    ("valid_time_present", "unsupported_valid_time"),
    ("effective_time_present", "unsupported_effective_time"),
)


def _semantic_failure_reason(input_value: CanonicalMappingInputV1) -> str | None:
    projection = semantic_projection_from_claim(input_value.claim)
    for field, reason in _SEMANTIC_REASON_ORDER:
        if getattr(projection, field):
            return reason
    return None


def _raise_arguments_error() -> None:
    raise CanonicalMappingBuilderError(stage=CanonicalMappingBuilderStage.ARGUMENTS)


def _validate_arguments(
    *,
    provenance: MapperProvenanceV1,
    source_provenance: MappingSourceProvenanceV1,
    actor_provenance: MappingActorProvenanceV1,
    created_at: datetime,
    first_created_at: datetime | None,
    mapping_version: int,
    proposal: CanonicalMappingProposalV1 | None,
    remap_provenance: MappingRemapProvenanceV1 | None,
) -> None:
    if not isinstance(provenance, MapperProvenanceV1):
        _raise_arguments_error()
    if not isinstance(source_provenance, MappingSourceProvenanceV1):
        _raise_arguments_error()
    if not isinstance(actor_provenance, MappingActorProvenanceV1):
        _raise_arguments_error()
    if proposal is not None and not isinstance(proposal, CanonicalMappingProposalV1):
        _raise_arguments_error()
    if remap_provenance is not None and not isinstance(remap_provenance, MappingRemapProvenanceV1):
        _raise_arguments_error()
    for value in (created_at, first_created_at):
        if value is not None:
            if not isinstance(value, datetime):
                _raise_arguments_error()
            try:
                strict_datetime(value, field="created_at")
            except (TypeError, ValueError, OverflowError):
                _raise_arguments_error()
    if isinstance(mapping_version, bool) or not isinstance(mapping_version, int) or mapping_version < 1:
        _raise_arguments_error()


def _validated_confidence(value: object, *, required: bool) -> float | None:
    if value is None:
        if required:
            _raise_arguments_error()
        return None
    if type(value) not in (int, float):
        _raise_arguments_error()
    try:
        normalized = float(value)
    except Exception:
        _raise_arguments_error()
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        _raise_arguments_error()
    return normalized


def _confidence_for_outcome(value: object, outcome: MappingOutcome) -> float | None:
    if outcome == "mapped":
        return _validated_confidence(value, required=True)
    if outcome == "ambiguous":
        return _validated_confidence(value, required=False)
    if value is not None:
        _raise_arguments_error()
    return None


def _known_business_failure(
    input_value: CanonicalMappingInputV1,
    *,
    proposal_present: bool,
) -> tuple[MappingOutcome, str] | None:
    """Return only failures established from typed facts before M0 calls."""
    # These facts must win over endpoint ambiguity. A missing type is not an
    # ambiguous endpoint, even when its resolution authority is absent.
    if input_value.endpoint_type_binding.source_type_key is None:
        return "blocked", "unknown_source_type"
    if input_value.endpoint_type_binding.target_type_key is None:
        return "blocked", "unknown_target_type"
    if input_value.surface_direction == "unknown":
        return "ambiguous", "unknown_direction"

    semantic_reason = _semantic_failure_reason(input_value)
    if semantic_reason is not None:
        return "blocked", semantic_reason

    if not proposal_present:
        if input_value.surface_raw_predicate not in input_value.frozen_ontology.relation_type_keys:
            return "blocked", "unknown_predicate"
        return "blocked", "no_explicit_mapping"

    endpoint_resolutions = (
        input_value.source_endpoint_resolution,
        input_value.target_endpoint_resolution,
    )
    if any(
        resolution is None or resolution.entity_link.status != "resolved"
        for resolution in endpoint_resolutions
    ):
        return "ambiguous", "ambiguous_endpoint"
    return None


def _contract_result(
    input_value: CanonicalMappingInputV1,
    *,
    provenance: MapperProvenanceV1,
    source_provenance: MappingSourceProvenanceV1,
    actor_provenance: MappingActorProvenanceV1,
    outcome: MappingOutcome,
    reason_code: str | None,
    proposal: CanonicalMappingProposalV1 | None,
    mapping_confidence: float | None,
    semantic_status: Literal["preserved", "ambiguous", "blocked"],
    mapping_version: int,
    created_at: datetime,
    remap_provenance: MappingRemapProvenanceV1 | None,
) -> CanonicalMappingV1:
    try:
        result = build_contract_mapping(
            input_value,
            provenance=provenance,
            source_provenance=source_provenance,
            actor_provenance=actor_provenance,
            outcome=outcome,
            created_at=created_at,
            mapping_version=mapping_version,
            proposal=proposal,
            reason_code=reason_code,
            mapping_confidence=mapping_confidence,
            semantic_status=semantic_status,
            remap_provenance=remap_provenance,
        )
    except Exception as error:
        raise CanonicalMappingBuilderError(stage=CanonicalMappingBuilderStage.FACTORY) from error

    try:
        # Keep the serializer as the final trusted boundary. The builder does
        # not create a second result representation or hash algorithm.
        encoded = canonical_mapping_json(input_value, result)
        return CanonicalMappingV1.model_validate(json.loads(encoded))
    except Exception as error:
        raise CanonicalMappingBuilderError(
            stage=CanonicalMappingBuilderStage.RESULT_SERIALIZER,
        ) from error


def _failure_result(
    input_value: CanonicalMappingInputV1,
    *,
    provenance: MapperProvenanceV1,
    source_provenance: MappingSourceProvenanceV1,
    actor_provenance: MappingActorProvenanceV1,
    outcome: MappingOutcome,
    reason_code: str,
    mapping_confidence: float | None,
    mapping_version: int,
    created_at: datetime,
    remap_provenance: MappingRemapProvenanceV1 | None,
) -> CanonicalMappingV1:
    semantic_status = {
        "ambiguous": "ambiguous",
        "blocked": "blocked",
        "rejected": "preserved",
    }[outcome]
    # A failure while constructing the failure result is a mapper error. It
    # must retain its typed stage and exception chain, never be downgraded.
    return _contract_result(
        input_value,
        provenance=provenance,
        source_provenance=source_provenance,
        actor_provenance=actor_provenance,
        outcome=outcome,
        reason_code=reason_code,
        proposal=None,
        mapping_confidence=mapping_confidence,
        semantic_status=semantic_status,
        mapping_version=mapping_version,
        created_at=created_at,
        remap_provenance=remap_provenance,
    )


def build_canonical_mapping(
    input_value: CanonicalMappingInputV1,
    *,
    provenance: MapperProvenanceV1,
    source_provenance: MappingSourceProvenanceV1,
    actor_provenance: MappingActorProvenanceV1,
    created_at: datetime,
    mapping_confidence: float | None,
    first_created_at: datetime | None = None,
    mapping_version: int = 1,
    proposal: CanonicalMappingProposalV1 | None = None,
    remap_provenance: MappingRemapProvenanceV1 | None = None,
) -> CanonicalMappingV1:
    """Build one explicit mapping result through the frozen M0 contract.

    ``first_created_at`` is a caller-supplied persistence value. It is used
    for a retry when the identity already exists; timestamps never enter the
    M0 identity fingerprints. No clock, provider, registry lookup, or mapping
    inference is performed here.
    """
    if not isinstance(input_value, CanonicalMappingInputV1):
        _raise_arguments_error()
    _validate_arguments(
        provenance=provenance,
        source_provenance=source_provenance,
        actor_provenance=actor_provenance,
        created_at=created_at,
        first_created_at=first_created_at,
        mapping_version=mapping_version,
        proposal=proposal,
        remap_provenance=remap_provenance,
    )

    try:
        canonical_mapping_input_json(input_value)
    except Exception as error:
        raise CanonicalMappingBuilderError(
            stage=CanonicalMappingBuilderStage.INPUT_AUTHORITY,
        ) from error

    try:
        business_failure = _known_business_failure(
            input_value,
            proposal_present=proposal is not None,
        )
    except Exception as error:
        raise CanonicalMappingBuilderError(
            stage=CanonicalMappingBuilderStage.INPUT_AUTHORITY,
        ) from error

    if business_failure is not None:
        outcome, reason_code = business_failure
        normalized_confidence = _confidence_for_outcome(mapping_confidence, outcome)
        return _failure_result(
            input_value,
            provenance=provenance,
            source_provenance=source_provenance,
            actor_provenance=actor_provenance,
            outcome=outcome,
            reason_code=reason_code,
            mapping_confidence=normalized_confidence,
            mapping_version=mapping_version,
            created_at=first_created_at or created_at,
            remap_provenance=remap_provenance,
        )

    if input_value.decision_kind == "schema_extension_candidate":
        normalized_confidence = _confidence_for_outcome(mapping_confidence, "blocked")
        return _failure_result(
            input_value,
            provenance=provenance,
            source_provenance=source_provenance,
            actor_provenance=actor_provenance,
            outcome="blocked",
            reason_code="no_explicit_mapping",
            mapping_confidence=None,
            mapping_version=mapping_version,
            created_at=first_created_at or created_at,
            remap_provenance=remap_provenance,
        )

    # A proposal is required for the mapped branch. All remaining proposal
    # validation belongs to the M0 factory and any failure is a mapper error.
    if proposal is None:
        raise CanonicalMappingBuilderError(stage=CanonicalMappingBuilderStage.ARGUMENTS)

    normalized_confidence = _validated_confidence(mapping_confidence, required=True)

    return _contract_result(
        input_value,
        provenance=provenance,
        source_provenance=source_provenance,
        actor_provenance=actor_provenance,
        outcome="mapped",
        reason_code=None,
        proposal=proposal,
        mapping_confidence=normalized_confidence,
        semantic_status="preserved",
        mapping_version=mapping_version,
        created_at=first_created_at or created_at,
        remap_provenance=remap_provenance,
    )


__all__ = [
    "CanonicalMappingBuildError",
    "CanonicalMappingBuilderError",
    "CanonicalMappingBuilderErrorCode",
    "CanonicalMappingBuilderStage",
    "build_canonical_mapping",
]
