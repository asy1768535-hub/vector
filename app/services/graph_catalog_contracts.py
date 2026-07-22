from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Literal, ParamSpec, TypeVar

from pydantic import ValidationError

from app.models.library import Library
from app.models.entity import (
    GRAPH_FACT_STATUS_ACTIVE,
    GRAPH_FACT_STATUS_DELETED,
    GRAPH_FACT_STATUS_DISABLED,
    GRAPH_FACT_STATUS_DRAFT,
    GRAPH_FACT_STATUS_PENDING_REVIEW,
    GRAPH_FACT_STATUS_REJECTED,
    GRAPH_FACT_STATUS_STALE,
    GRAPH_SOURCE_EXTRACTED,
    GRAPH_SOURCE_IMPORTED,
    GRAPH_SOURCE_MANUAL,
)


GraphCatalogFactStatus = Literal[
    "draft",
    "pending_review",
    "active",
    "rejected",
    "stale",
    "disabled",
]
GraphCatalogSourceType = Literal["manual", "imported", "extracted"]
GraphCatalogPublicationState = Literal["all", "published", "staged"]

FACT_STATUSES: tuple[GraphCatalogFactStatus, ...] = (
    GRAPH_FACT_STATUS_DRAFT,
    GRAPH_FACT_STATUS_PENDING_REVIEW,
    GRAPH_FACT_STATUS_ACTIVE,
    GRAPH_FACT_STATUS_REJECTED,
    GRAPH_FACT_STATUS_STALE,
    GRAPH_FACT_STATUS_DISABLED,
)
SOURCE_TYPES: tuple[GraphCatalogSourceType, ...] = (
    GRAPH_SOURCE_MANUAL,
    GRAPH_SOURCE_IMPORTED,
    GRAPH_SOURCE_EXTRACTED,
)
PUBLICATION_STATES: tuple[GraphCatalogPublicationState, ...] = (
    "all",
    "published",
    "staged",
)
REVIEW_STATUSES = ("pending_review", "approved", "rejected", "not_required")

MAX_SCOPE_ITEMS = 20
MAX_FILTER_ITEMS = 20
MAX_QUERY_LENGTH = 160
MAX_PAGE_SIZE = 100
_CURSOR_MAX_LENGTH = 4096
_ENTITY_CURSOR_VERSION = "graph-catalog-entity-cursor-v1"
_RELATION_CURSOR_VERSION = "graph-catalog-relation-cursor-v1"
_TYPE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class GraphCatalogError(RuntimeError):
    def __init__(self, code: str, message: str = "Graph Catalog operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class GraphCatalogScopeIncompatibility:
    library_slug: str
    reason_codes: tuple[str, ...]


class GraphCatalogScopeError(GraphCatalogError):
    def __init__(
        self,
        code: str,
        *,
        incompatibilities: tuple[GraphCatalogScopeIncompatibility, ...] = (),
    ) -> None:
        super().__init__(code)
        self.incompatibilities = incompatibilities


def fail_graph_catalog(
    code: str,
    message: str = "Graph Catalog operation failed",
) -> None:
    raise GraphCatalogError(code, message)


def graph_catalog_invariant_boundary(
    function: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    @wraps(function)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await function(*args, **kwargs)
        except ValidationError as exc:
            raise GraphCatalogError("graph_catalog_unavailable") from exc

    return wrapped


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _normalize_query(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail_graph_catalog("graph_catalog_filter_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > MAX_QUERY_LENGTH:
        fail_graph_catalog("graph_catalog_filter_invalid")
    return normalized


def _validate_uuid_tuple(value: tuple[uuid.UUID, ...]) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) > MAX_FILTER_ITEMS
        or len(set(value)) != len(value)
        or any(not isinstance(item, uuid.UUID) for item in value)
    ):
        fail_graph_catalog("graph_catalog_filter_invalid")


def _validate_string_tuple(
    value: tuple[str, ...],
    *,
    allowed: tuple[str, ...] | None = None,
    type_keys: bool = False,
) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) > MAX_FILTER_ITEMS
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or not item for item in value)
        or (allowed is not None and any(item not in allowed for item in value))
        or (type_keys and any(_TYPE_KEY_RE.fullmatch(item) is None for item in value))
    ):
        fail_graph_catalog("graph_catalog_filter_invalid")


@dataclass(frozen=True, slots=True)
class GraphCatalogSelection:
    organization_id: uuid.UUID
    library_slugs: tuple[str, ...] | None = None
    scope_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, uuid.UUID):
            fail_graph_catalog("graph_catalog_scope_invalid")
        has_slugs = self.library_slugs is not None
        has_scope = self.scope_id is not None
        if has_slugs == has_scope:
            fail_graph_catalog("graph_catalog_scope_invalid")
        if has_slugs:
            slugs = self.library_slugs
            if (
                not isinstance(slugs, tuple)
                or not 1 <= len(slugs) <= MAX_SCOPE_ITEMS
                or len(set(slugs)) != len(slugs)
                or any(
                    not isinstance(slug, str) or _SLUG_RE.fullmatch(slug) is None
                    for slug in slugs
                )
            ):
                fail_graph_catalog("graph_catalog_scope_invalid")
        elif not isinstance(self.scope_id, uuid.UUID):
            fail_graph_catalog("graph_catalog_scope_invalid")


@dataclass(frozen=True, slots=True)
class GraphCatalogResolvedScope:
    organization_id: uuid.UUID
    libraries: tuple[Library, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.organization_id, uuid.UUID)
            or not isinstance(self.libraries, tuple)
            or not 1 <= len(self.libraries) <= MAX_SCOPE_ITEMS
            or any(not isinstance(item, Library) for item in self.libraries)
        ):
            fail_graph_catalog("graph_catalog_scope_invalid")


@dataclass(frozen=True, slots=True)
class GraphEntityCatalogQuery:
    selection: GraphCatalogSelection
    query_text: str | None = None
    ontology_version_ids: tuple[uuid.UUID, ...] = ()
    type_keys: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    publication_state: GraphCatalogPublicationState = "all"
    limit: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.selection, GraphCatalogSelection):
            fail_graph_catalog("graph_catalog_request_invalid")
        object.__setattr__(self, "query_text", _normalize_query(self.query_text))
        _validate_uuid_tuple(self.ontology_version_ids)
        _validate_string_tuple(self.type_keys, type_keys=True)
        _validate_string_tuple(self.statuses, allowed=FACT_STATUSES)
        _validate_string_tuple(self.source_types, allowed=SOURCE_TYPES)
        if (
            self.publication_state not in PUBLICATION_STATES
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_PAGE_SIZE
        ):
            fail_graph_catalog("graph_catalog_filter_invalid")

    def filter_payload(self) -> dict[str, object]:
        return {
            "ontology_version_ids": [str(item) for item in self.ontology_version_ids],
            "publication_state": self.publication_state,
            "query_text": self.query_text,
            "source_types": list(self.source_types),
            "statuses": list(self.statuses),
            "type_keys": list(self.type_keys),
        }


@dataclass(frozen=True, slots=True)
class GraphRelationCatalogQuery:
    selection: GraphCatalogSelection
    query_text: str | None = None
    ontology_version_ids: tuple[uuid.UUID, ...] = ()
    type_keys: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    review_statuses: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    publication_state: GraphCatalogPublicationState = "all"
    limit: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.selection, GraphCatalogSelection):
            fail_graph_catalog("graph_catalog_request_invalid")
        object.__setattr__(self, "query_text", _normalize_query(self.query_text))
        _validate_uuid_tuple(self.ontology_version_ids)
        _validate_string_tuple(self.type_keys, type_keys=True)
        _validate_string_tuple(self.statuses, allowed=FACT_STATUSES)
        _validate_string_tuple(self.review_statuses, allowed=REVIEW_STATUSES)
        _validate_string_tuple(self.source_types, allowed=SOURCE_TYPES)
        if (
            self.publication_state not in PUBLICATION_STATES
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_PAGE_SIZE
        ):
            fail_graph_catalog("graph_catalog_filter_invalid")

    def filter_payload(self) -> dict[str, object]:
        return {
            "ontology_version_ids": [str(item) for item in self.ontology_version_ids],
            "publication_state": self.publication_state,
            "query_text": self.query_text,
            "review_statuses": list(self.review_statuses),
            "source_types": list(self.source_types),
            "statuses": list(self.statuses),
            "type_keys": list(self.type_keys),
        }


def graph_catalog_filter_fingerprint(
    *,
    kind: Literal["entity", "relation"],
    organization_id: uuid.UUID,
    library_ids: tuple[uuid.UUID, ...],
    filters: dict[str, object],
) -> str:
    if (
        kind not in {"entity", "relation"}
        or not isinstance(organization_id, uuid.UUID)
        or not isinstance(library_ids, tuple)
        or not library_ids
        or any(not isinstance(item, uuid.UUID) for item in library_ids)
        or not isinstance(filters, dict)
    ):
        fail_graph_catalog("graph_catalog_filter_invalid")
    return _sha256(
        {
            "contract_version": "graph-catalog-filter-v1",
            "filters": filters,
            "kind": kind,
            "library_ids": [str(item) for item in library_ids],
            "organization_id": str(organization_id),
        }
    )


@dataclass(frozen=True, slots=True)
class GraphEntityCatalogCursor:
    normalized_name: str
    library_slug: str
    entity_id: uuid.UUID
    filter_fingerprint: str


@dataclass(frozen=True, slots=True)
class GraphRelationCatalogCursor:
    relation_type_key: str
    source_normalized_name: str
    target_normalized_name: str
    library_slug: str
    relation_id: uuid.UUID
    filter_fingerprint: str


def _encode_cursor(version: str, values: dict[str, str]) -> str:
    payload = {"version": version, **values}
    encoded = base64.urlsafe_b64encode(_canonical_json(payload).encode("ascii"))
    return encoded.decode("ascii").rstrip("=")


def encode_entity_cursor(cursor: GraphEntityCatalogCursor) -> str:
    _validate_entity_cursor(cursor)
    return _encode_cursor(
        _ENTITY_CURSOR_VERSION,
        {
            "entity_id": str(cursor.entity_id),
            "filter_fingerprint": cursor.filter_fingerprint,
            "library_slug": cursor.library_slug,
            "normalized_name": cursor.normalized_name,
        },
    )


def encode_relation_cursor(cursor: GraphRelationCatalogCursor) -> str:
    _validate_relation_cursor(cursor)
    return _encode_cursor(
        _RELATION_CURSOR_VERSION,
        {
            "filter_fingerprint": cursor.filter_fingerprint,
            "library_slug": cursor.library_slug,
            "relation_id": str(cursor.relation_id),
            "relation_type_key": cursor.relation_type_key,
            "source_normalized_name": cursor.source_normalized_name,
            "target_normalized_name": cursor.target_normalized_name,
        },
    )


def _decode_cursor(value: str | None) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _CURSOR_MAX_LENGTH:
        fail_graph_catalog("graph_catalog_cursor_invalid")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GraphCatalogError("graph_catalog_cursor_invalid") from exc
    if not isinstance(payload, dict):
        fail_graph_catalog("graph_catalog_cursor_invalid")
    return payload


def decode_entity_cursor(
    value: str | None,
    *,
    expected_filter_fingerprint: str,
) -> GraphEntityCatalogCursor | None:
    payload = _decode_cursor(value)
    if payload is None:
        return None
    if set(payload) != {
        "entity_id",
        "filter_fingerprint",
        "library_slug",
        "normalized_name",
        "version",
    }:
        fail_graph_catalog("graph_catalog_cursor_invalid")
    try:
        cursor = GraphEntityCatalogCursor(
            normalized_name=payload["normalized_name"],
            library_slug=payload["library_slug"],
            entity_id=uuid.UUID(payload["entity_id"]),
            filter_fingerprint=payload["filter_fingerprint"],
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise GraphCatalogError("graph_catalog_cursor_invalid") from exc
    if (
        payload["version"] != _ENTITY_CURSOR_VERSION
        or cursor.filter_fingerprint != expected_filter_fingerprint
        or encode_entity_cursor(cursor) != value
    ):
        fail_graph_catalog("graph_catalog_cursor_invalid")
    return cursor


def decode_relation_cursor(
    value: str | None,
    *,
    expected_filter_fingerprint: str,
) -> GraphRelationCatalogCursor | None:
    payload = _decode_cursor(value)
    if payload is None:
        return None
    if set(payload) != {
        "filter_fingerprint",
        "library_slug",
        "relation_id",
        "relation_type_key",
        "source_normalized_name",
        "target_normalized_name",
        "version",
    }:
        fail_graph_catalog("graph_catalog_cursor_invalid")
    try:
        cursor = GraphRelationCatalogCursor(
            relation_type_key=payload["relation_type_key"],
            source_normalized_name=payload["source_normalized_name"],
            target_normalized_name=payload["target_normalized_name"],
            library_slug=payload["library_slug"],
            relation_id=uuid.UUID(payload["relation_id"]),
            filter_fingerprint=payload["filter_fingerprint"],
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise GraphCatalogError("graph_catalog_cursor_invalid") from exc
    if (
        payload["version"] != _RELATION_CURSOR_VERSION
        or cursor.filter_fingerprint != expected_filter_fingerprint
        or encode_relation_cursor(cursor) != value
    ):
        fail_graph_catalog("graph_catalog_cursor_invalid")
    return cursor


def _validate_entity_cursor(cursor: GraphEntityCatalogCursor) -> None:
    if (
        not isinstance(cursor, GraphEntityCatalogCursor)
        or not isinstance(cursor.normalized_name, str)
        or not cursor.normalized_name
        or len(cursor.normalized_name) > 512
        or _SLUG_RE.fullmatch(cursor.library_slug) is None
        or not isinstance(cursor.entity_id, uuid.UUID)
        or _SHA256_RE.fullmatch(cursor.filter_fingerprint) is None
    ):
        fail_graph_catalog("graph_catalog_cursor_invalid")


def _validate_relation_cursor(cursor: GraphRelationCatalogCursor) -> None:
    if (
        not isinstance(cursor, GraphRelationCatalogCursor)
        or _TYPE_KEY_RE.fullmatch(cursor.relation_type_key) is None
        or not isinstance(cursor.source_normalized_name, str)
        or not cursor.source_normalized_name
        or len(cursor.source_normalized_name) > 512
        or not isinstance(cursor.target_normalized_name, str)
        or not cursor.target_normalized_name
        or len(cursor.target_normalized_name) > 512
        or _SLUG_RE.fullmatch(cursor.library_slug) is None
        or not isinstance(cursor.relation_id, uuid.UUID)
        or _SHA256_RE.fullmatch(cursor.filter_fingerprint) is None
    ):
        fail_graph_catalog("graph_catalog_cursor_invalid")


assert GRAPH_FACT_STATUS_DELETED not in FACT_STATUSES
