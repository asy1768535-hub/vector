from __future__ import annotations

import json
import re

import yaml
from pydantic import ValidationError

from app.schemas.schema_lifecycle import SchemaImportFileRequest, SchemaImportRequest
from app.services.schema_lifecycle_contracts import SchemaLifecycleError


_SUPPORTED_EXTENSIONS = {"json", "yaml", "yml"}
_EXTERNAL_TYPE_MAP = {
    "string": "string",
    "text": "text",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
}


def _external_key(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "External Schema key is invalid"
        )
    return text


def _external_property_type(value: object) -> tuple[str, list[str] | None]:
    type_name = str(value).strip()
    optional = type_name.endswith("?")
    if optional:
        type_name = type_name[:-1]
    if type_name.endswith("[]"):
        return "json", None
    if type_name.startswith("enum[") and type_name.endswith("]"):
        values = [item.strip() for item in type_name[5:-1].split(",") if item.strip()]
        if not values:
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "External enum property is empty"
            )
        return "enum", values
    mapped = _EXTERNAL_TYPE_MAP.get(type_name)
    if mapped is None:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "External Schema property type is unsupported"
        )
    return mapped, None


def _convert_external_schema(document: dict) -> dict:
    """Convert the richer extraction Schema document into lifecycle input."""
    if "schema_id" not in document:
        return document

    external_entities = document.get("entity_types")
    external_relations = document.get("relation_types")
    if not isinstance(external_entities, list) or not isinstance(external_relations, list):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid",
            "External Schema must define entity_types and relation_types",
        )

    entity_keys = {
        str(item.get("id")): _external_key(item.get("id"))
        for item in external_entities
        if isinstance(item, dict) and item.get("id") is not None
    }
    if len(entity_keys) != len(external_entities):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "External entity ids must be unique"
        )

    entity_types = []
    attributes = []
    attribute_keys: set[tuple[str, str]] = set()
    for item in external_entities:
        if not isinstance(item, dict) or not item.get("id") or not item.get("label"):
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "External entity definition is invalid"
            )
        key = entity_keys[str(item["id"])]
        properties = item.get("properties") or {}
        if not isinstance(properties, dict):
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "External entity properties must be an object"
            )
        required = set(item.get("required_properties") or [])
        properties_schema = {
            "type": "object",
            "properties": properties,
            "required": sorted(required),
        }
        entity_types.append(
            {
                "key": key,
                "label": item["label"],
                "description": item.get("layer"),
                "properties_schema": properties_schema,
            }
        )
        for property_name, property_type in properties.items():
            attribute_key = _external_key(property_name)
            identity = (key, attribute_key)
            if identity in attribute_keys:
                raise SchemaLifecycleError(
                    "schema_lifecycle_request_invalid", "External property keys must be unique"
                )
            attribute_keys.add(identity)
            value_type, enum_values = _external_property_type(property_type)
            attributes.append(
                {
                    "owner_kind": "entity_type",
                    "owner_key": key,
                    "key": attribute_key,
                    "label": str(property_name),
                    "value_type": value_type,
                    "required": property_name in required,
                    "enum_values": enum_values,
                }
            )

    relation_keys = {
        str(item.get("id")): _external_key(item.get("id"))
        for item in external_relations
        if isinstance(item, dict) and item.get("id") is not None
    }
    if len(relation_keys) != len(external_relations):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "External relation ids must be unique"
        )
    relation_types = []
    constraints = []
    for item in external_relations:
        if not isinstance(item, dict) or not item.get("id") or not item.get("source"):
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "External relation definition is invalid"
            )
        source_key = entity_keys.get(str(item["source"]))
        targets = item.get("target")
        targets = targets if isinstance(targets, list) else [targets]
        if source_key is None or not targets or any(entity_keys.get(str(target)) is None for target in targets):
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "External relation endpoint is unavailable"
            )
        relation_key = relation_keys[str(item["id"])]
        relation_types.append(
            {
                "key": relation_key,
                "label": str(item["id"]),
                "direction": "directed",
                "default_review_policy": "pending_review",
            }
        )
        for target in targets:
            constraints.append(
                {
                    "relation_type_key": relation_key,
                    "source_entity_type_key": source_key,
                    "target_entity_type_key": entity_keys[str(target)],
                }
            )

    return {
        "version_key": _external_key(document["schema_id"]),
        "description": document.get("purpose") or document.get("domain"),
        "entity_types": entity_types,
        "relation_types": relation_types,
        "attributes": attributes,
        "constraints": constraints,
    }


def parse_schema_import_file(body: SchemaImportFileRequest) -> SchemaImportRequest:
    extension = body.file_name.rsplit(".", 1)[-1].lower() if "." in body.file_name else ""
    if extension not in _SUPPORTED_EXTENSIONS:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid",
            "Schema file must use .json, .yaml, or .yml",
        )

    try:
        parsed = json.loads(body.content) if extension == "json" else yaml.safe_load(body.content)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema file content is invalid"
        ) from exc

    if not isinstance(parsed, dict):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema file must contain an object"
        )
    if "idempotency_key" in parsed:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema file contains a forbidden field"
        )
    try:
        converted = _convert_external_schema(parsed)
        return SchemaImportRequest.model_validate(
            {**converted, "idempotency_key": body.idempotency_key}
        )
    except ValidationError as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema file does not match the Schema format"
        ) from exc
