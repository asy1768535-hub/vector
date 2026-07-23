from __future__ import annotations

import re
import uuid
from typing import Any

from app.models.library import Library
from app.schemas.schema_lifecycle import (
    SchemaValidationIssueRead,
    SchemaValidationRead,
)
from app.services.schema_lifecycle_read import (
    SchemaVersionBundle,
    schema_version_state_hash,
)


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ENABLED = "draft"
_ROW_STATUSES = {_ENABLED, "disabled"}
_DIRECTIONS = {"directed", "undirected"}
_REVIEW_POLICIES = {"auto_active", "pending_review", "manual_only"}
_VALUE_TYPES = {
    "string",
    "text",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "enum",
    "json",
}
_CARDINALITIES = {None, "one_to_one", "one_to_many", "many_to_one", "many_to_many"}
_JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _issue(
    issues: list[SchemaValidationIssueRead],
    code: str,
    kind: str,
    item_id: uuid.UUID | None = None,
    field: str | None = None,
) -> None:
    issues.append(
        SchemaValidationIssueRead(
            code=code,
            item_kind=kind,
            item_id=item_id,
            field=field,
        )
    )


def _json_schema_valid(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    required = value.get("required", [])
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) or not item for item in required)
    ):
        return False
    properties = value.get("properties", {})
    if properties is not None:
        if not isinstance(properties, dict):
            return False
        for key, rule in properties.items():
            if not isinstance(key, str) or not key or not isinstance(rule, dict):
                return False
            type_value = rule.get("type")
            if type_value is not None:
                names = type_value if isinstance(type_value, list) else [type_value]
                if not names or any(name not in _JSON_TYPES for name in names):
                    return False
            if "enum" in rule and not isinstance(rule["enum"], list):
                return False
    root_type = value.get("type")
    if root_type is not None:
        names = root_type if isinstance(root_type, list) else [root_type]
        if not names or any(name not in _JSON_TYPES for name in names):
            return False
    return True


def _duplicate_keys(rows) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        if row.status != _ENABLED:
            continue
        if row.key in seen:
            duplicates.add(row.key)
        seen.add(row.key)
    return duplicates


def validate_schema_draft_bundle(
    library: Library,
    bundle: SchemaVersionBundle,
) -> SchemaValidationRead:
    issues: list[SchemaValidationIssueRead] = []
    version = bundle.version
    if version.library_id != library.id or version.status != "draft":
        _issue(issues, "ontology_version_not_draft", "ontology_version", version.id, "status")

    collections = (
        ("entity_type", bundle.entity_types),
        ("relation_type", bundle.relation_types),
        ("attribute", bundle.attributes),
        ("constraint", bundle.constraints),
    )
    for kind, rows in collections:
        for row in rows:
            if (
                row.library_id != library.id
                or row.ontology_version_id != version.id
            ):
                _issue(issues, "item_scope_invalid", kind, row.id)
            if row.status not in _ROW_STATUSES:
                _issue(issues, "item_status_invalid", kind, row.id, "status")

    enabled_entities = {
        row.id: row for row in bundle.entity_types if row.status == _ENABLED
    }
    enabled_relations = {
        row.id: row for row in bundle.relation_types if row.status == _ENABLED
    }
    if not enabled_entities:
        _issue(issues, "entity_type_required", "ontology_version", version.id)

    for kind, rows in (
        ("entity_type", bundle.entity_types),
        ("relation_type", bundle.relation_types),
    ):
        for row in rows:
            if row.status != _ENABLED:
                continue
            if _KEY_RE.fullmatch(row.key) is None:
                _issue(issues, "type_key_invalid", kind, row.id, "key")
            if not row.label.strip():
                _issue(issues, "type_label_required", kind, row.id, "label")
            if not _json_schema_valid(row.properties_schema):
                _issue(issues, "properties_schema_invalid", kind, row.id, "properties_schema")
        for key in _duplicate_keys(rows):
            row = next(item for item in rows if item.key == key)
            _issue(issues, "type_key_duplicate", kind, row.id, "key")

    for row in enabled_relations.values():
        if row.direction not in _DIRECTIONS:
            _issue(issues, "relation_direction_invalid", "relation_type", row.id, "direction")
        if row.default_review_policy not in _REVIEW_POLICIES:
            _issue(
                issues,
                "relation_review_policy_invalid",
                "relation_type",
                row.id,
                "default_review_policy",
            )

    attribute_keys: set[tuple[str, uuid.UUID, str]] = set()
    for row in bundle.attributes:
        if row.status != _ENABLED:
            continue
        key = (row.owner_kind, row.owner_type_id, row.key)
        if key in attribute_keys:
            _issue(issues, "attribute_key_duplicate", "attribute", row.id, "key")
        attribute_keys.add(key)
        owners = enabled_entities if row.owner_kind == "entity_type" else enabled_relations
        if row.owner_kind not in {"entity_type", "relation_type"}:
            _issue(issues, "attribute_owner_kind_invalid", "attribute", row.id, "owner_kind")
        elif row.owner_type_id not in owners:
            _issue(issues, "attribute_owner_missing", "attribute", row.id, "owner_type_id")
        if _KEY_RE.fullmatch(row.key) is None:
            _issue(issues, "attribute_key_invalid", "attribute", row.id, "key")
        if row.value_type not in _VALUE_TYPES:
            _issue(issues, "attribute_value_type_invalid", "attribute", row.id, "value_type")
        if row.value_type == "enum" and not row.enum_values:
            _issue(
                issues,
                "attribute_enum_values_required",
                "attribute",
                row.id,
                "enum_values",
            )
        if row.value_type != "enum" and row.enum_values is not None:
            _issue(
                issues,
                "attribute_enum_values_forbidden",
                "attribute",
                row.id,
                "enum_values",
            )
        if not _json_schema_valid(row.validation_schema):
            _issue(
                issues,
                "attribute_validation_schema_invalid",
                "attribute",
                row.id,
                "validation_schema",
            )

    constraint_keys: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()
    for row in bundle.constraints:
        if row.status != _ENABLED:
            continue
        key = (
            row.relation_type_id,
            row.source_entity_type_id,
            row.target_entity_type_id,
        )
        if key in constraint_keys:
            _issue(issues, "constraint_duplicate", "constraint", row.id)
        constraint_keys.add(key)
        if row.relation_type_id not in enabled_relations:
            _issue(issues, "constraint_relation_missing", "constraint", row.id, "relation_type_id")
        if row.source_entity_type_id not in enabled_entities:
            _issue(issues, "constraint_source_missing", "constraint", row.id, "source_entity_type_id")
        if row.target_entity_type_id not in enabled_entities:
            _issue(issues, "constraint_target_missing", "constraint", row.id, "target_entity_type_id")
        if row.cardinality not in _CARDINALITIES:
            _issue(issues, "constraint_cardinality_invalid", "constraint", row.id, "cardinality")

    issues.sort(
        key=lambda item: (
            item.code,
            item.item_kind,
            str(item.item_id or ""),
            item.field or "",
        )
    )
    issues = issues[:100]
    return SchemaValidationRead(
        library_id=library.id,
        ontology_version_id=version.id,
        version_state_hash=schema_version_state_hash(bundle),
        valid=not issues,
        issues=issues,
    )
