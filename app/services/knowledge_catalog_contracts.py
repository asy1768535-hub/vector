from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


CatalogOverallState = Literal["processing", "usable", "partial", "failed"]
CatalogCapabilityState = Literal[
    "disabled",
    "unavailable",
    "processing",
    "ready",
    "pending_review",
    "failed",
]
CatalogClassificationState = Literal[
    "unclassified",
    "pending_review",
    "classified",
    "failed",
]

_CAPABILITY_STATES = {
    "disabled",
    "unavailable",
    "processing",
    "ready",
    "pending_review",
    "failed",
}
_CLASSIFICATION_STATES = {
    "unclassified",
    "pending_review",
    "classified",
    "failed",
}
_DOCUMENT_STATUSES = {"pending", "processing", "ready", "failed"}
_CURSOR_VERSION = "catalog-document-cursor-v1"
_CURSOR_MAX_LENGTH = 2048
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class KnowledgeCatalogError(RuntimeError):
    def __init__(self, code: str, message: str = "Knowledge Catalog operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def fail_catalog(code: str, message: str = "Knowledge Catalog operation failed") -> None:
    raise KnowledgeCatalogError(code, message)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_title_query(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail_catalog("catalog_filter_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > 160:
        fail_catalog("catalog_filter_invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class CatalogDocumentQuery:
    title_query: str | None = None
    document_status: str | None = None
    classification_state: CatalogClassificationState | None = None
    label_id: uuid.UUID | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "title_query", _normalize_title_query(self.title_query))
        if self.document_status is not None and self.document_status not in _DOCUMENT_STATUSES:
            fail_catalog("catalog_filter_invalid")
        if (
            self.classification_state is not None
            and self.classification_state not in _CLASSIFICATION_STATES
        ):
            fail_catalog("catalog_filter_invalid")
        if self.label_id is not None and not isinstance(self.label_id, uuid.UUID):
            fail_catalog("catalog_filter_invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            fail_catalog("catalog_limit_invalid")

    def canonical_payload(self) -> dict[str, str | int | None]:
        return {
            "title_query": self.title_query,
            "document_status": self.document_status,
            "classification_state": self.classification_state,
            "label_id": str(self.label_id) if self.label_id is not None else None,
            "limit": self.limit,
        }


def catalog_filter_fingerprint(
    library_id: uuid.UUID,
    query: CatalogDocumentQuery,
) -> str:
    if not isinstance(library_id, uuid.UUID) or not isinstance(query, CatalogDocumentQuery):
        fail_catalog("catalog_filter_invalid")
    return _sha256(
        {
            "contract_version": "catalog-document-filter-v1",
            "library_id": str(library_id),
            **query.canonical_payload(),
        }
    )


@dataclass(frozen=True, slots=True)
class CatalogDocumentCursor:
    updated_at: datetime
    document_id: uuid.UUID
    filter_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
            or not isinstance(self.document_id, uuid.UUID)
            or not isinstance(self.filter_fingerprint, str)
            or _SHA256_RE.fullmatch(self.filter_fingerprint) is None
        ):
            fail_catalog("catalog_cursor_invalid")


def encode_catalog_document_cursor(cursor: CatalogDocumentCursor) -> str:
    if not isinstance(cursor, CatalogDocumentCursor):
        fail_catalog("catalog_cursor_invalid")
    payload = {
        "document_id": str(cursor.document_id),
        "filter_fingerprint": cursor.filter_fingerprint,
        "updated_at": cursor.updated_at.astimezone(timezone.utc).isoformat(),
        "version": _CURSOR_VERSION,
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload).encode("ascii"))
    return encoded.decode("ascii").rstrip("=")


def decode_catalog_document_cursor(
    value: str | None,
    *,
    expected_filter_fingerprint: str,
) -> CatalogDocumentCursor | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _CURSOR_MAX_LENGTH
        or _SHA256_RE.fullmatch(expected_filter_fingerprint) is None
    ):
        fail_catalog("catalog_cursor_invalid")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {
            "document_id",
            "filter_fingerprint",
            "updated_at",
            "version",
        }:
            raise ValueError("cursor shape")
        if (
            payload["version"] != _CURSOR_VERSION
            or payload["filter_fingerprint"] != expected_filter_fingerprint
        ):
            raise ValueError("cursor identity")
        updated_at = datetime.fromisoformat(payload["updated_at"])
        cursor = CatalogDocumentCursor(
            updated_at=updated_at,
            document_id=uuid.UUID(payload["document_id"]),
            filter_fingerprint=payload["filter_fingerprint"],
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeCatalogError("catalog_cursor_invalid") from exc
    if encode_catalog_document_cursor(cursor) != value:
        fail_catalog("catalog_cursor_invalid")
    return cursor


@dataclass(frozen=True, slots=True)
class CatalogCapabilityProjection:
    overall_state: CatalogOverallState
    source: CatalogCapabilityState
    search: CatalogCapabilityState
    chat: CatalogCapabilityState
    summary: CatalogCapabilityState
    outline: CatalogCapabilityState
    classification: CatalogCapabilityState
    graph: CatalogCapabilityState


def project_catalog_capabilities(
    *,
    document_status: str,
    revision_status: str,
    source_available: bool,
    summary_state: CatalogCapabilityState,
    outline_state: CatalogCapabilityState,
    classification_state: CatalogCapabilityState,
    graph_state: CatalogCapabilityState,
) -> CatalogCapabilityProjection:
    derived = (summary_state, outline_state, classification_state, graph_state)
    if (
        document_status not in _DOCUMENT_STATUSES
        or revision_status not in _DOCUMENT_STATUSES | {"superseded", "deleted"}
        or not isinstance(source_available, bool)
        or any(value not in _CAPABILITY_STATES for value in derived)
    ):
        fail_catalog("catalog_state_invalid")
    base_ready = document_status == "ready" and revision_status == "ready"
    base_failed = document_status == "failed" or revision_status in {
        "failed",
        "superseded",
        "deleted",
    }
    source_state: CatalogCapabilityState
    if source_available:
        source_state = "ready"
    elif base_failed:
        source_state = "failed"
    elif base_ready:
        source_state = "unavailable"
    else:
        source_state = "processing"
    if base_ready:
        search_state: CatalogCapabilityState = "ready"
        chat_state: CatalogCapabilityState = "ready"
        overall: CatalogOverallState = (
            "usable"
            if source_state == "ready"
            and all(value in {"ready", "disabled"} for value in derived)
            else "partial"
        )
    elif base_failed:
        search_state = "failed"
        chat_state = "failed"
        overall = "failed"
    else:
        search_state = "processing"
        chat_state = "processing"
        overall = "processing"
    return CatalogCapabilityProjection(
        overall,
        source_state,
        search_state,
        chat_state,
        summary_state,
        outline_state,
        classification_state,
        graph_state,
    )
