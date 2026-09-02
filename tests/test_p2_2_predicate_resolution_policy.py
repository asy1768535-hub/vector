from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models.fact_foundation import StablePredicateIdentity
from app.services.stable_predicate_resolution_policy import (
    StablePredicateResolutionPolicy,
    is_predicate_ready_for_fact_resolution,
)


def _policy(temporal_class: str = "state_fact") -> dict[str, object]:
    return {
        "schema_version": "p2_v1",
        "temporal_class": temporal_class,
        "object_policy": {"source": "target_entity"},
        "measurement_policy": None,
        "qualifier_policy": {
            "identity_bearing": [],
            "assertion_bearing": [],
            "evidence_only": [],
        },
        "polarity_policy": {"source": "fixed", "value": "affirmed"},
        "modality_policy": {"source": "fixed", "value": "confirmed"},
        "valid_time_policy": {"source": "none"},
        "effective_time_policy": {"source": "none"},
        "event_temporal_identity_policy": None,
    }


def _measurement_policy() -> dict[str, object]:
    policy = _policy("measurement_slot")
    policy["measurement_policy"] = {
        "value_property_key": "shareholding_ratio",
        "value_type": "ratio",
        "unit_policy": {"source": "fixed", "value": "ratio"},
        "currency_policy": {"source": "none"},
    }
    return policy


def _predicate(
    *,
    resolution_status: str,
    temporal_class: str = "state_fact",
    identity_policy_version: str = "p2_identity_v1",
    resolution_policy: dict[str, object] | None = None,
) -> StablePredicateIdentity:
    return StablePredicateIdentity(
        resolution_status=resolution_status,
        temporal_class=temporal_class,
        identity_policy_version=identity_policy_version,
        resolution_policy=resolution_policy,
    )


def test_resolution_policy_accepts_complete_state_fact_contract():
    policy = StablePredicateResolutionPolicy.model_validate(_policy())

    assert policy.object_policy.source == "target_entity"
    assert policy.measurement_policy is None


def test_resolution_policy_accepts_complete_measurement_contract():
    policy = StablePredicateResolutionPolicy.model_validate(_measurement_policy())

    assert policy.measurement_policy is not None
    assert policy.measurement_policy.value_property_key == "shareholding_ratio"


def test_resolution_policy_rejects_measurement_without_value_source():
    policy = _measurement_policy()
    measurement = policy["measurement_policy"]
    assert isinstance(measurement, dict)
    measurement.pop("value_property_key")

    with pytest.raises(ValidationError):
        StablePredicateResolutionPolicy.model_validate(policy)


def test_resolution_policy_rejects_event_without_temporal_identity_source():
    with pytest.raises(ValidationError):
        StablePredicateResolutionPolicy.model_validate(_policy("event_fact"))


def test_resolution_policy_rejects_qualifier_key_in_multiple_classes():
    policy = _policy()
    qualifiers = policy["qualifier_policy"]
    assert isinstance(qualifiers, dict)
    qualifiers["identity_bearing"] = ["share_class"]
    qualifiers["assertion_bearing"] = ["share_class"]

    with pytest.raises(ValidationError):
        StablePredicateResolutionPolicy.model_validate(policy)


def test_resolution_policy_rejects_unknown_source_mode():
    policy = _policy()
    object_policy = policy["object_policy"]
    assert isinstance(object_policy, dict)
    object_policy["source"] = "guessed"

    with pytest.raises(ValidationError):
        StablePredicateResolutionPolicy.model_validate(policy)


def test_resolution_policy_rejects_property_source_without_property_key():
    policy = _policy()
    policy["polarity_policy"] = {
        "source": "property",
        "mapping": {"yes": "affirmed", "no": "negated"},
    }

    with pytest.raises(ValidationError):
        StablePredicateResolutionPolicy.model_validate(policy)


def test_pending_predicate_without_policy_is_not_ready():
    assert is_predicate_ready_for_fact_resolution(
        _predicate(resolution_status="pending"), active_mapping_count=1
    ) is False


def test_resolved_predicate_with_valid_policy_is_ready():
    assert is_predicate_ready_for_fact_resolution(
        _predicate(resolution_status="resolved", resolution_policy=_policy()), active_mapping_count=1
    ) is True


def test_resolved_predicate_requires_exactly_one_active_mapping():
    predicate = _predicate(resolution_status="resolved", resolution_policy=_policy())

    assert is_predicate_ready_for_fact_resolution(predicate, active_mapping_count=0) is False
    assert is_predicate_ready_for_fact_resolution(predicate, active_mapping_count=2) is False


def test_resolved_predicate_without_policy_is_not_ready():
    assert is_predicate_ready_for_fact_resolution(
        _predicate(resolution_status="resolved"), active_mapping_count=1
    ) is False


def test_resolved_predicate_with_invalid_policy_is_not_ready():
    assert is_predicate_ready_for_fact_resolution(
        _predicate(
            resolution_status="resolved",
            resolution_policy={"schema_version": "p2_v1"},
        ),
        active_mapping_count=1,
    ) is False


def test_legacy_pending_scaffold_is_not_ready():
    assert is_predicate_ready_for_fact_resolution(
        _predicate(
            resolution_status="pending",
            identity_policy_version="legacy_v1",
        ),
        active_mapping_count=1,
    ) is False


def test_predicate_identity_exposes_nullable_policy_with_static_checks():
    table = StablePredicateIdentity.__table__
    checks = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}

    assert isinstance(table.c.resolution_policy.type, JSONB)
    assert table.c.resolution_policy.nullable is True
    assert table.c.resolution_policy.default is None
    assert table.c.resolution_policy.server_default is None
    assert {
        "ck_stable_predicate_identities_resolution_policy_json",
        "ck_stable_predicate_identities_resolved_requires_policy",
    } <= checks
