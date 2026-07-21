from __future__ import annotations

import json
from typing import Any


class MetadataValidationError(ValueError):
    pass


RESERVED_METADATA_FIELDS = {
    "library_id",
    "document_id",
    "document_revision_id",
    "document_revision_no",
    "document_revision",
    "document_block_id",
    "block_id",
    "evidence_id",
    "chunk_id",
    "seq",
    "text",
    "title",
    "external_id",
    "sync_source_id",
    "sync_source_key",
    "folder_id",
    "current_revision_id",
    "latest_revision_id",
    "source_start",
    "source_end",
    "page_start",
    "page_end",
    "title_path",
    "position",
    "chunk_kind",
    "block_kind",
    "evidence_kind",
    "visibility_scope",
    "security_level",
    "status",
    "created_at",
    "updated_at",
    "deleted_at",
}

RESERVED_PREFIXES = ("system_", "internal_")
RESERVED_NAMESPACES = {"_system", "_internal"}
MAX_METADATA_BYTES = 64 * 1024
MAX_METADATA_DEPTH = 8


def _depth(value: Any, current: int = 0) -> int:
    if isinstance(value, dict):
        if not value:
            return current + 1
        return max(_depth(v, current + 1) for v in value.values())
    if isinstance(value, list):
        if not value:
            return current + 1
        return max(_depth(v, current + 1) for v in value)
    return current + 1


def _check_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MetadataValidationError("metadata keys must be strings")
            if key in RESERVED_METADATA_FIELDS or key in RESERVED_NAMESPACES:
                raise MetadataValidationError(f"metadata field '{key}' is reserved")
            if key.startswith(RESERVED_PREFIXES):
                raise MetadataValidationError(f"metadata namespace '{key}' is reserved")
            _check_keys(item)
    elif isinstance(value, list):
        for item in value:
            _check_keys(item)


def validate_external_metadata(metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise MetadataValidationError("metadata must be a JSON object")
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MetadataValidationError("metadata must be JSON serializable") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise MetadataValidationError("metadata is too large")
    if _depth(metadata) > MAX_METADATA_DEPTH:
        raise MetadataValidationError("metadata is too deeply nested")
    _check_keys(metadata)
