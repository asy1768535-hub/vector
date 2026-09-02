from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.fact_foundation import StablePredicateIdentity


PropertyKey = Annotated[str, Field(min_length=1)]
TemporalClass = Literal["static_fact", "state_fact", "measurement_slot", "event_fact"]


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnitOrCurrencyPolicy(_PolicyModel):
    source: Literal["fixed", "property", "none"]
    value: PropertyKey | None = None
    property_key: PropertyKey | None = None

    @model_validator(mode="after")
    def validate_source(self) -> UnitOrCurrencyPolicy:
        if self.source == "fixed":
            if self.value is None or self.property_key is not None:
                raise ValueError("fixed source requires value and forbids property_key")
        elif self.source == "property":
            if self.property_key is None or self.value is not None:
                raise ValueError("property source requires property_key and forbids value")
        elif self.value is not None or self.property_key is not None:
            raise ValueError("none source forbids value and property_key")
        return self


class ObjectPolicy(_PolicyModel):
    source: Literal["target_entity", "property_literal", "none"]
    property_key: PropertyKey | None = None
    literal_type: Literal["decimal", "ratio", "money", "integer", "date", "boolean", "string"] | None = None
    unit_policy: UnitOrCurrencyPolicy | None = None
    currency_policy: UnitOrCurrencyPolicy | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ObjectPolicy:
        if self.source == "property_literal":
            if self.property_key is None or self.literal_type is None:
                raise ValueError("property_literal source requires property_key and literal_type")
            if self.unit_policy is None or self.currency_policy is None:
                raise ValueError("property_literal source requires unit_policy and currency_policy")
        elif any(
            value is not None
            for value in (self.property_key, self.literal_type, self.unit_policy, self.currency_policy)
        ):
            raise ValueError("target_entity and none sources forbid literal policy fields")
        return self


class MeasurementPolicy(_PolicyModel):
    value_property_key: PropertyKey
    value_type: Literal["decimal", "ratio", "money", "integer"]
    unit_policy: UnitOrCurrencyPolicy
    currency_policy: UnitOrCurrencyPolicy


class QualifierPolicy(_PolicyModel):
    identity_bearing: tuple[PropertyKey, ...]
    assertion_bearing: tuple[PropertyKey, ...]
    evidence_only: tuple[PropertyKey, ...]

    @model_validator(mode="after")
    def validate_disjoint_classes(self) -> QualifierPolicy:
        keys = self.identity_bearing + self.assertion_bearing + self.evidence_only
        if len(keys) != len(set(keys)):
            raise ValueError("qualifier keys must belong to exactly one class")
        return self


class PolarityPolicy(_PolicyModel):
    source: Literal["fixed", "property"]
    value: Literal["affirmed", "negated", "unknown"] | None = None
    property_key: PropertyKey | None = None
    mapping: dict[PropertyKey, Literal["affirmed", "negated", "unknown"]] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> PolarityPolicy:
        if self.source == "fixed":
            if self.value is None or self.property_key is not None or self.mapping is not None:
                raise ValueError("fixed polarity requires value only")
        elif self.property_key is None or not self.mapping or self.value is not None:
            raise ValueError("property polarity requires property_key and mapping")
        return self


class ModalityPolicy(_PolicyModel):
    source: Literal["fixed", "property"]
    value: Literal["planned", "possible", "expected", "confirmed", "completed", "unknown"] | None = None
    property_key: PropertyKey | None = None
    mapping: dict[
        PropertyKey,
        Literal["planned", "possible", "expected", "confirmed", "completed", "unknown"],
    ] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ModalityPolicy:
        if self.source == "fixed":
            if self.value is None or self.property_key is not None or self.mapping is not None:
                raise ValueError("fixed modality requires value only")
        elif self.property_key is None or not self.mapping or self.value is not None:
            raise ValueError("property modality requires property_key and mapping")
        return self


class TimePolicy(_PolicyModel):
    source: Literal["none", "instant_property", "range_properties"]
    property_key: PropertyKey | None = None
    from_property_key: PropertyKey | None = None
    to_property_key: PropertyKey | None = None

    @model_validator(mode="after")
    def validate_source(self) -> TimePolicy:
        if self.source == "none":
            if any(value is not None for value in (self.property_key, self.from_property_key, self.to_property_key)):
                raise ValueError("none time source forbids property keys")
        elif self.source == "instant_property":
            if (
                self.property_key is None
                or self.from_property_key is not None
                or self.to_property_key is not None
            ):
                raise ValueError("instant_property requires property_key only")
        elif (
            self.property_key is not None
            or self.from_property_key is None
            or self.to_property_key is None
        ):
            raise ValueError("range_properties requires from_property_key and to_property_key only")
        return self


class EventTemporalIdentityPolicy(_PolicyModel):
    source: Literal["valid_time", "effective_time", "explicit_property"]
    property_key: PropertyKey | None = None

    @model_validator(mode="after")
    def validate_source(self) -> EventTemporalIdentityPolicy:
        if self.source == "explicit_property":
            if self.property_key is None:
                raise ValueError("explicit_property requires property_key")
        elif self.property_key is not None:
            raise ValueError("valid_time and effective_time forbid property_key")
        return self


class StablePredicateResolutionPolicy(_PolicyModel):
    schema_version: Literal["p2_v1"]
    temporal_class: TemporalClass
    object_policy: ObjectPolicy
    measurement_policy: MeasurementPolicy | None
    qualifier_policy: QualifierPolicy
    polarity_policy: PolarityPolicy
    modality_policy: ModalityPolicy
    valid_time_policy: TimePolicy
    effective_time_policy: TimePolicy
    event_temporal_identity_policy: EventTemporalIdentityPolicy | None

    @model_validator(mode="after")
    def validate_temporal_class(self) -> StablePredicateResolutionPolicy:
        if self.temporal_class in ("static_fact", "state_fact"):
            if self.measurement_policy is not None or self.event_temporal_identity_policy is not None:
                raise ValueError("static_fact and state_fact forbid measurement and event policies")
        elif self.temporal_class == "measurement_slot":
            if self.measurement_policy is None or self.event_temporal_identity_policy is not None:
                raise ValueError("measurement_slot requires measurement policy and forbids event policy")
        elif self.measurement_policy is not None or self.event_temporal_identity_policy is None:
            raise ValueError("event_fact requires event temporal identity policy and forbids measurement policy")

        if self.temporal_class == "event_fact" and self.event_temporal_identity_policy is not None:
            source = self.event_temporal_identity_policy.source
            if source == "valid_time" and self.valid_time_policy.source == "none":
                raise ValueError("event valid_time identity requires a valid time source")
            if source == "effective_time" and self.effective_time_policy.source == "none":
                raise ValueError("event effective_time identity requires an effective time source")
        return self


def parse_stable_predicate_resolution_policy(
    policy: Mapping[str, object],
) -> StablePredicateResolutionPolicy:
    return StablePredicateResolutionPolicy.model_validate(dict(policy))


def is_predicate_ready_for_fact_resolution(
    predicate: StablePredicateIdentity,
    *,
    active_mapping_count: int,
) -> bool:
    if (
        active_mapping_count != 1
        or predicate.resolution_status != "resolved"
        or predicate.identity_policy_version == "legacy_v1"
        or not isinstance(predicate.resolution_policy, Mapping)
    ):
        return False

    try:
        policy = parse_stable_predicate_resolution_policy(predicate.resolution_policy)
    except (TypeError, ValueError, ValidationError):
        return False
    return policy.temporal_class == predicate.temporal_class
