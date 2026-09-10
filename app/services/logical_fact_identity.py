"""The stable LogicalFact identity fingerprint shared by P2 and P3.3 writers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.models.fact_foundation import StablePredicateIdentity


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def logical_fact_identity_fingerprint_v1(
    *,
    library_id: uuid.UUID,
    predicate: StablePredicateIdentity,
    subject_canonical_entity_id: uuid.UUID,
    object_kind: str | None,
    object_canonical_entity_id: uuid.UUID | None,
    object_value: dict[str, Any] | None,
    identity_qualifiers: Mapping[str, Any],
    temporal_identity_key: str | None,
) -> str:
    """Compile the one LogicalFact identity fingerprint shared by P2 and P3.3."""

    return _fingerprint(
        {
            "identity_policy_version": predicate.identity_policy_version,
            "identity_qualifiers": dict(identity_qualifiers),
            "library_id": str(library_id),
            "object_canonical_entity_id": (
                str(object_canonical_entity_id) if object_canonical_entity_id is not None else None
            ),
            "object_kind": object_kind,
            "object_value": object_value,
            "predicate": {
                "contract_version": predicate.contract_version,
                "identity_policy_version": predicate.identity_policy_version,
                "key": predicate.key,
                "namespace": predicate.namespace,
                "predicate_id": str(predicate.id),
                "temporal_class": predicate.temporal_class,
            },
            "schema_version": "logical_fact_identity_v1",
            "subject_canonical_entity_id": str(subject_canonical_entity_id),
            "temporal_identity_key": temporal_identity_key,
        }
    )
