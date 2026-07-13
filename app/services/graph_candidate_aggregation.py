from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Iterable


def _require_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} JSON object keys must be strings")
            _require_json_value(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def canonical_graph_json_v1(value: Any) -> str:
    _require_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_graph_value_hash_v1(value: Any) -> str:
    return hashlib.sha256(canonical_graph_json_v1(value).encode("utf-8")).hexdigest()


def _canonical_uuid(value: uuid.UUID | str | None, *, nullable: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        raise TypeError("UUID value cannot be null")
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("value must be a UUID") from exc


def _require_non_empty(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def entity_candidate_key_v1(
    *,
    ontology_version_id: uuid.UUID | str,
    entity_type_key: str,
    normalized_name: str,
) -> str:
    return canonical_graph_value_hash_v1(
        {
            "ontology_version_id": _canonical_uuid(ontology_version_id),
            "entity_type_key": _require_non_empty(entity_type_key, field="entity_type_key"),
            "normalized_name": _require_non_empty(normalized_name, field="normalized_name"),
        }
    )


def relation_candidate_key_v1(
    *,
    ontology_version_id: uuid.UUID | str,
    source_candidate_key: str,
    relation_type_key: str,
    target_candidate_key: str,
    properties: dict[str, Any],
) -> str:
    if not isinstance(properties, dict):
        raise TypeError("properties must be an object")
    return canonical_graph_value_hash_v1(
        {
            "ontology_version_id": _canonical_uuid(ontology_version_id),
            "source_candidate_key": _require_non_empty(
                source_candidate_key, field="source_candidate_key"
            ),
            "relation_type_key": _require_non_empty(
                relation_type_key, field="relation_type_key"
            ),
            "target_candidate_key": _require_non_empty(
                target_candidate_key, field="target_candidate_key"
            ),
            "properties": properties,
        }
    )


def merge_candidate_key_v1(
    *,
    job_id: uuid.UUID | str,
    entity_candidate_id: uuid.UUID | str,
    suggested_target_entity_id: uuid.UUID | str | None,
    reason: str,
) -> str:
    return canonical_graph_value_hash_v1(
        {
            "job_id": _canonical_uuid(job_id),
            "entity_candidate_id": _canonical_uuid(entity_candidate_id),
            "suggested_target_entity_id": _canonical_uuid(
                suggested_target_entity_id, nullable=True
            ),
            "reason": _require_non_empty(reason, field="reason"),
        }
    )


def _sorted_uuid_strings(values: Iterable[uuid.UUID | str]) -> list[str]:
    return sorted({_canonical_uuid(value) for value in values})


def _canonical_conflicting_fields(
    conflicting_fields: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_field: dict[str, set[str]] = {}
    for item in conflicting_fields:
        if not isinstance(item, dict) or set(item) != {"field", "value_hashes"}:
            raise ValueError("conflicting_fields entries must contain field and value_hashes")
        field = _require_non_empty(item["field"], field="conflicting field")
        hashes = item["value_hashes"]
        if not isinstance(hashes, list):
            raise TypeError("value_hashes must be a list")
        target = by_field.setdefault(field, set())
        for value_hash in hashes:
            target.add(_require_non_empty(value_hash, field="value_hash"))
    return [
        {"field": field, "value_hashes": sorted(value_hashes)}
        for field, value_hashes in sorted(by_field.items())
    ]


def conflict_key_v1(
    *,
    job_id: uuid.UUID | str,
    conflict_type: str,
    entity_candidate_ids: Iterable[uuid.UUID | str],
    relation_candidate_ids: Iterable[uuid.UUID | str],
    conflicting_fields: Iterable[dict[str, Any]],
) -> str:
    entity_ids = _sorted_uuid_strings(entity_candidate_ids)
    relation_ids = _sorted_uuid_strings(relation_candidate_ids)
    if not entity_ids and not relation_ids:
        raise ValueError("conflict must reference at least one Candidate")
    return canonical_graph_value_hash_v1(
        {
            "job_id": _canonical_uuid(job_id),
            "conflict_type": _require_non_empty(conflict_type, field="conflict_type"),
            "entity_candidate_ids": entity_ids,
            "relation_candidate_ids": relation_ids,
            "conflicting_fields": _canonical_conflicting_fields(conflicting_fields),
        }
    )
