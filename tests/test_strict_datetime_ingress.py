from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.canonical_mapping import CanonicalMappingV1, canonical_mapping_json
from app.schemas.claim_decision import ClaimDecisionProjectionV1, canonical_claim_decision_json
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    canonical_claim_shadow_replay_json,
    canonical_claim_shadow_replay_v2_json,
)
from tests.test_canonical_mapping import _build as build_mapping, _input as mapping_input
from tests.test_claim_decision import _build as build_decision
from tests.test_claim_shadow_replay_artifact import _artifact as artifact_v1
from tests.test_claim_shadow_replay_v2 import _artifact as artifact_v2, _claim as claim_v2


ACCEPTED_VALUES = (
    datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    datetime(2026, 8, 9, 20, 0, tzinfo=timezone(timedelta(hours=8))),
    "2026-08-09T12:00:00Z",
    "2026-08-09T20:00:00+08:00",
    "2026-08-09T04:00:00-08:00",
    "2026-08-09T12:00:00.123456Z",
)

REJECTED_VALUES = (
    True,
    0,
    1,
    1.0,
    Decimal("1"),
    datetime(2026, 8, 9, 12, 0),
    "2026-08-09 12:00:00Z",
    "2026-08-09T12:00:00+0800",
    "2026-08-09T12:00:00,123Z",
    "2026-08-09T12:00:00.1234567Z",
    "2026-08-09t12:00:00Z",
    "2026-08-09T12:00:00z",
    "2026-08-09",
    "2026-08-09T12:00:00",
    "bogus",
    "infinity",
    float("inf"),
    float("nan"),
)


def _mapping_payload(*, field: str, value: object) -> dict[str, object]:
    payload = build_mapping().model_dump(mode="json")
    if field == "created_at":
        payload[field] = value
    else:
        payload["evidence_bindings"][0]["attestation"][field] = value
    return payload


def _decision_payload(value: object) -> dict[str, object]:
    payload = build_decision().model_dump(mode="json")
    payload["created_at"] = value
    return payload


def _artifact_payload(version: str, value: object) -> dict[str, object]:
    if version == "v1":
        payload = artifact_v1()
    else:
        payload = artifact_v2(claim_v2()).model_dump(mode="json")
    payload["created_at"] = value
    return payload


@pytest.mark.parametrize("value", ACCEPTED_VALUES)
def test_strict_datetime_accepts_only_explicit_timezone(value: object) -> None:
    CanonicalMappingV1.model_validate(_mapping_payload(field="created_at", value=value))
    CanonicalMappingV1.model_validate(_mapping_payload(field="validated_at", value=value))
    ClaimDecisionProjectionV1.model_validate(_decision_payload(value))
    ClaimShadowReplayArtifactV1.model_validate(_artifact_payload("v1", value))
    ClaimShadowReplayArtifactV2.model_validate(_artifact_payload("v2", value))


@pytest.mark.parametrize("value", REJECTED_VALUES)
def test_strict_datetime_rejects_coercion_and_noncanonical_rfc3339(value: object) -> None:
    with pytest.raises((ValidationError, ValueError)):
        CanonicalMappingV1.model_validate(_mapping_payload(field="created_at", value=value))
    with pytest.raises((ValidationError, ValueError)):
        CanonicalMappingV1.model_validate(_mapping_payload(field="validated_at", value=value))
    with pytest.raises((ValidationError, ValueError)):
        ClaimDecisionProjectionV1.model_validate(_decision_payload(value))
    with pytest.raises((ValidationError, ValueError)):
        ClaimShadowReplayArtifactV1.model_validate(_artifact_payload("v1", value))
    with pytest.raises((ValidationError, ValueError)):
        ClaimShadowReplayArtifactV2.model_validate(_artifact_payload("v2", value))


def test_same_utc_instant_has_one_canonical_timestamp_and_mapping_identity() -> None:
    input_value = mapping_input()
    base = build_mapping(input_value, created_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    offset_payload = base.model_dump(mode="json")
    offset_payload["created_at"] = "2026-08-09T20:00:00+08:00"
    offset = CanonicalMappingV1.model_validate(offset_payload)

    base_json = canonical_mapping_json(input_value, base)
    offset_json = canonical_mapping_json(input_value, offset)
    assert base.mapping_result_fingerprint == offset.mapping_result_fingerprint
    assert base_json == offset_json
    assert json.loads(offset_json)["created_at"] == "2026-08-09T12:00:00Z"

    attestation_payload = base.model_dump(mode="json")
    attestation_payload["evidence_bindings"][0]["attestation"]["validated_at"] = (
        "2026-08-09T20:00:00+08:00"
    )
    offset_attestation = CanonicalMappingV1.model_validate(attestation_payload)
    assert canonical_mapping_json(input_value, offset_attestation) == base_json


def test_decision_and_artifact_serializers_normalize_offsets_without_identity_drift() -> None:
    decision = build_decision()
    decision_payload = decision.model_dump(mode="json")
    decision_payload["created_at"] = "2026-08-07T20:00:00+08:00"
    offset_decision = ClaimDecisionProjectionV1.model_validate(decision_payload)
    assert canonical_claim_decision_json(offset_decision) == canonical_claim_decision_json(decision)

    v1 = artifact_v1()
    v1_offset = _artifact_payload("v1", "2026-08-07T08:00:00+08:00")
    assert canonical_claim_shadow_replay_json(v1_offset) == canonical_claim_shadow_replay_json(v1)

    v2 = artifact_v2(claim_v2())
    v2_payload = v2.model_dump(mode="json")
    v2_payload["created_at"] = "2026-08-07T08:00:00+08:00"
    v2_offset = ClaimShadowReplayArtifactV2.model_validate(v2_payload)
    assert canonical_claim_shadow_replay_v2_json(v2_offset) == canonical_claim_shadow_replay_v2_json(v2)
