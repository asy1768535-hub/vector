from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, update

from app.config import settings
from app.models.attribute_definition import AttributeDefinition
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkEvidence
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.graph_candidate_validation import (
    OntologySnapshotError,
    load_ontology_rule_set_v1,
)
from app.services.graph_extraction_prompt import (
    graph_extraction_prompt_hash,
    graph_extraction_prompt_version,
)
from app.services.graph_extraction_provider import graph_extraction_provider_name


_ACTIVE = "active"
_READY = "ready"
_TRIGGER_TYPES = {"manual", "revision_published", "full_rerun", "repair", "eval"}
_EXECUTION_MODES = {"production", "eval", "repair"}
_JSON_SCHEMA_TYPES = {
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
}
_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class GraphExtractionJobError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphExtractionUnitPlan:
    ordinal: int
    center_chunk_id: uuid.UUID
    center_evidence_id: uuid.UUID
    unit_fingerprint: str


def _fail(code: str, message: str) -> None:
    raise GraphExtractionJobError(code, message)


def _hash(value: Any) -> str:
    try:
        return canonical_graph_value_hash_v1(value)
    except (TypeError, ValueError) as exc:
        raise GraphExtractionJobError(
            "invalid_frozen_config",
            "graph extraction frozen configuration must be canonical JSON",
        ) from exc


def _validate_version(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _VERSION_TOKEN.fullmatch(value) is None:
        _fail("invalid_frozen_config", f"{label} must be a stable version token")
    return value


def _validate_json_schema(value: Any, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _fail("invalid_ontology_snapshot", f"{label} must be an object or null")
    required = value.get("required", [])
    if required is None:
        required = []
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
        _fail("invalid_ontology_snapshot", f"{label}.required must be a string array")
    properties = value.get("properties", {})
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        _fail("invalid_ontology_snapshot", f"{label}.properties must be an object")
    for key, rule in properties.items():
        if not isinstance(key, str) or not key or not isinstance(rule, dict):
            _fail("invalid_ontology_snapshot", f"{label}.properties is malformed")
        type_value = rule.get("type")
        if type_value is not None:
            type_names = type_value if isinstance(type_value, list) else [type_value]
            if not type_names or any(
                not isinstance(item, str) or item not in _JSON_SCHEMA_TYPES for item in type_names
            ):
                _fail(
                    "invalid_ontology_snapshot",
                    f"{label}.properties.{key}.type is unsupported",
                )
        if "enum" in rule and not isinstance(rule["enum"], list):
            _fail(
                "invalid_ontology_snapshot",
                f"{label}.properties.{key}.enum must be an array",
            )


def _scoped(row: Any, *, library_id: uuid.UUID, ontology_id: uuid.UUID) -> bool:
    return (
        getattr(row, "library_id", library_id) == library_id
        and getattr(row, "ontology_version_id", ontology_id) == ontology_id
        and getattr(row, "status", _ACTIVE) == _ACTIVE
    )


def _attribute_payload(row: AttributeDefinition) -> dict[str, Any]:
    _validate_json_schema(
        row.validation_schema,
        label=f"attribute_definitions.{row.key}.validation_schema",
    )
    if row.enum_values is not None and not isinstance(row.enum_values, list):
        _fail(
            "invalid_ontology_snapshot",
            f"attribute_definitions.{row.key}.enum_values must be an array or null",
        )
    return {
        "key": row.key,
        "value_type": row.value_type,
        "required": row.required,
        "enum_values": deepcopy(row.enum_values),
        "validation_schema": deepcopy(row.validation_schema),
    }


async def build_ontology_rule_snapshot(
    db,
    *,
    library: Library,
    ontology_version_id: uuid.UUID,
    allow_draft: bool = False,
) -> tuple[dict[str, Any], str]:
    ontology = await db.get(OntologyVersion, ontology_version_id)
    allowed_statuses = {_ACTIVE, "draft"} if allow_draft else {_ACTIVE}
    if ontology is None or ontology.library_id != library.id or ontology.status not in allowed_statuses:
        _fail(
            "active_ontology_required",
            "an active ontology in the same Library is required",
        )

    entity_result = await db.execute(
        select(EntityType).where(
            EntityType.library_id == library.id,
            EntityType.ontology_version_id == ontology_version_id,
            EntityType.status == _ACTIVE,
        )
    )
    relation_result = await db.execute(
        select(RelationType).where(
            RelationType.library_id == library.id,
            RelationType.ontology_version_id == ontology_version_id,
            RelationType.status == _ACTIVE,
        )
    )
    constraint_result = await db.execute(
        select(RelationTypeConstraint).where(
            RelationTypeConstraint.library_id == library.id,
            RelationTypeConstraint.ontology_version_id == ontology_version_id,
            RelationTypeConstraint.status == _ACTIVE,
        )
    )
    attribute_result = await db.execute(
        select(AttributeDefinition).where(
            AttributeDefinition.library_id == library.id,
            AttributeDefinition.ontology_version_id == ontology_version_id,
            AttributeDefinition.status == _ACTIVE,
        )
    )

    entity_types = [
        row
        for row in entity_result.scalars().all()
        if _scoped(row, library_id=library.id, ontology_id=ontology_version_id)
    ]
    relation_types = [
        row
        for row in relation_result.scalars().all()
        if _scoped(row, library_id=library.id, ontology_id=ontology_version_id)
    ]
    constraints = [
        row
        for row in constraint_result.scalars().all()
        if _scoped(row, library_id=library.id, ontology_id=ontology_version_id)
    ]
    attributes = [
        row
        for row in attribute_result.scalars().all()
        if _scoped(row, library_id=library.id, ontology_id=ontology_version_id)
    ]
    if not entity_types and not allow_draft:
        _fail(
            "invalid_ontology_snapshot",
            "active ontology must contain at least one active Entity Type",
        )

    entity_ids = {row.id for row in entity_types}
    relation_ids = {row.id for row in relation_types}
    if len(entity_ids) != len(entity_types) or len({row.key for row in entity_types}) != len(entity_types):
        _fail("invalid_ontology_snapshot", "active Entity Type keys and IDs must be unique")
    if len(relation_ids) != len(relation_types) or len({row.key for row in relation_types}) != len(
        relation_types
    ):
        _fail(
            "invalid_ontology_snapshot",
            "active Relation Type keys and IDs must be unique",
        )

    attributes_by_owner: dict[tuple[str, uuid.UUID], list[AttributeDefinition]] = {}
    seen_attribute_keys: set[tuple[str, uuid.UUID, str]] = set()
    for row in attributes:
        owner_key = (row.owner_kind, row.owner_type_id)
        if row.owner_kind == "entity_type":
            owner_exists = row.owner_type_id in entity_ids
        elif row.owner_kind == "relation_type":
            owner_exists = row.owner_type_id in relation_ids
        else:
            owner_exists = False
        if not owner_exists:
            _fail(
                "invalid_ontology_snapshot",
                "active Attribute Definition references an unknown active owner",
            )
        unique_key = (*owner_key, row.key)
        if unique_key in seen_attribute_keys:
            _fail(
                "invalid_ontology_snapshot",
                "active Attribute Definition keys must be unique per owner",
            )
        seen_attribute_keys.add(unique_key)
        attributes_by_owner.setdefault(owner_key, []).append(row)

    entity_payload = []
    for row in sorted(entity_types, key=lambda item: (item.key, str(item.id))):
        _validate_json_schema(
            row.properties_schema,
            label=f"entity_types.{row.key}.properties_schema",
        )
        owner_attributes = attributes_by_owner.get(("entity_type", row.id), [])
        entity_payload.append(
            {
                "id": str(row.id),
                "key": row.key,
                "label": row.label,
                "description": row.description,
                "properties_schema": deepcopy(row.properties_schema),
                "active_attribute_definitions": [
                    _attribute_payload(item) for item in sorted(owner_attributes, key=lambda item: item.key)
                ],
            }
        )

    relation_payload = []
    for row in sorted(relation_types, key=lambda item: (item.key, str(item.id))):
        _validate_json_schema(
            row.properties_schema,
            label=f"relation_types.{row.key}.properties_schema",
        )
        owner_attributes = attributes_by_owner.get(("relation_type", row.id), [])
        relation_payload.append(
            {
                "id": str(row.id),
                "key": row.key,
                "label": row.label,
                "description": row.description,
                "direction": row.direction,
                "requires_evidence": row.requires_evidence,
                "default_review_policy": row.default_review_policy,
                "properties_schema": deepcopy(row.properties_schema),
                "active_attribute_definitions": [
                    _attribute_payload(item) for item in sorted(owner_attributes, key=lambda item: item.key)
                ],
            }
        )

    constraint_payload = []
    seen_constraints: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()
    for row in sorted(
        constraints,
        key=lambda item: (
            str(item.relation_type_id),
            str(item.source_entity_type_id),
            str(item.target_entity_type_id),
        ),
    ):
        key = (
            row.relation_type_id,
            row.source_entity_type_id,
            row.target_entity_type_id,
        )
        if (
            key in seen_constraints
            or row.relation_type_id not in relation_ids
            or row.source_entity_type_id not in entity_ids
            or row.target_entity_type_id not in entity_ids
        ):
            _fail(
                "invalid_ontology_snapshot",
                "active Relation Constraint references must be unique and active",
            )
        seen_constraints.add(key)
        constraint_payload.append(
            {
                "relation_type_id": str(row.relation_type_id),
                "source_entity_type_id": str(row.source_entity_type_id),
                "target_entity_type_id": str(row.target_entity_type_id),
                "cardinality": row.cardinality,
                "requires_review": row.requires_review,
            }
        )

    snapshot = {
        "ontology_version_id": str(ontology_version_id),
        "entity_types": entity_payload,
        "relation_types": relation_payload,
        "relation_constraints": constraint_payload,
    }
    if getattr(ontology, "status", None) == "draft":
        snapshot.update({"schema_state": "ai_discovery_pending", "confirmed": False})
    snapshot_hash = _hash(snapshot)
    try:
        load_ontology_rule_set_v1(
            SimpleNamespace(
                ontology_version_id=ontology_version_id,
                ontology_snapshot=snapshot,
                ontology_snapshot_hash=snapshot_hash,
                normalization_rule_version="normalization_v1",
                confidence_policy_version="v1",
            )
        )
    except OntologySnapshotError as exc:
        raise GraphExtractionJobError(exc.code, str(exc)) from exc
    return snapshot, snapshot_hash


async def select_active_ontology(db, *, library: Library) -> OntologyVersion:
    result = await db.execute(
        select(OntologyVersion)
        .where(
            OntologyVersion.library_id == library.id,
            OntologyVersion.status == _ACTIVE,
        )
        .order_by(
            OntologyVersion.version_key.asc(),
            OntologyVersion.version_no.desc(),
            OntologyVersion.id.asc(),
        )
        .limit(2)
    )
    rows = [row for row in result.scalars().all() if row.library_id == library.id and row.status == _ACTIVE]
    if not rows:
        _fail("active_ontology_required", "Library has no active ontology")
    if len(rows) != 1:
        _fail(
            "ambiguous_active_ontology",
            "Library must have exactly one active ontology for graph extraction",
        )
    return rows[0]


def _same_evidence_scope(
    row: Any,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> bool:
    return (
        row.library_id == library_id
        and row.document_id == document_id
        and row.document_revision_id == revision_id
        and row.status == _ACTIVE
    )


async def plan_graph_extraction_units(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
) -> tuple[GraphExtractionUnitPlan, ...]:
    chunk_result = await db.execute(
        select(Chunk)
        .where(
            Chunk.library_id == library.id,
            Chunk.document_id == document.id,
            Chunk.document_revision_id == revision.id,
        )
        .order_by(Chunk.seq.asc(), Chunk.id.asc())
    )
    chunks = sorted(
        chunk_result.scalars().all(),
        key=lambda row: (row.seq, str(row.id)),
    )
    chunk_ids = [row.id for row in chunks]
    link_result = await db.execute(
        select(ChunkEvidence)
        .where(
            ChunkEvidence.chunk_id.in_(chunk_ids),
            ChunkEvidence.document_revision_id == revision.id,
        )
        .order_by(
            ChunkEvidence.chunk_id.asc(),
            ChunkEvidence.seq.asc(),
            ChunkEvidence.evidence_id.asc(),
        )
    )
    links = [
        row
        for row in link_result.scalars().all()
        if row.chunk_id in set(chunk_ids) and row.document_revision_id == revision.id
    ]
    evidence_ids = {row.evidence_id for row in links}
    evidence_ids.update(row.evidence_id for row in chunks if row.evidence_id is not None)
    evidence_result = await db.execute(
        select(EvidenceUnit).where(
            EvidenceUnit.id.in_(evidence_ids),
            EvidenceUnit.library_id == library.id,
            EvidenceUnit.document_id == document.id,
            EvidenceUnit.document_revision_id == revision.id,
            EvidenceUnit.status == _ACTIVE,
        )
    )
    evidence_by_id = {
        row.id: row
        for row in evidence_result.scalars().all()
        if _same_evidence_scope(
            row,
            library_id=library.id,
            document_id=document.id,
            revision_id=revision.id,
        )
    }
    links_by_chunk: dict[uuid.UUID, list[Any]] = {}
    for row in sorted(
        links,
        key=lambda item: (str(item.chunk_id), item.seq, str(item.evidence_id)),
    ):
        links_by_chunk.setdefault(row.chunk_id, []).append(row)

    plans: list[GraphExtractionUnitPlan] = []
    for chunk in chunks:
        direct = evidence_by_id.get(chunk.evidence_id)
        center_evidence_id = direct.id if direct is not None else None
        if center_evidence_id is None:
            for link in links_by_chunk.get(chunk.id, []):
                linked = evidence_by_id.get(link.evidence_id)
                if linked is not None:
                    center_evidence_id = linked.id
                    break
        if center_evidence_id is None:
            continue
        unit_fingerprint = _hash(
            {
                "document_revision_id": str(revision.id),
                "center_chunk_id": str(chunk.id),
                "center_evidence_id": str(center_evidence_id),
                "chunk_seq": chunk.seq,
                "chunk_text_hash": hashlib.sha256((chunk.text or "").encode("utf-8")).hexdigest(),
            }
        )
        plans.append(
            GraphExtractionUnitPlan(
                ordinal=len(plans),
                center_chunk_id=chunk.id,
                center_evidence_id=center_evidence_id,
                unit_fingerprint=unit_fingerprint,
            )
        )
    if not plans:
        _fail(
            "no_eligible_units",
            "current ready revision has no Chunk with active same-scope Evidence",
        )
    return tuple(plans)


def _validate_creation_scope(
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    trigger_type: str,
    execution_mode: str,
    rerun_of_job_id: uuid.UUID | None,
) -> None:
    if trigger_type not in _TRIGGER_TYPES:
        _fail("invalid_trigger_type", "unsupported graph extraction trigger type")
    if execution_mode not in _EXECUTION_MODES:
        _fail("invalid_execution_mode", "unsupported graph extraction execution mode")
    if trigger_type == "full_rerun" and rerun_of_job_id is None:
        _fail("rerun_source_required", "full rerun requires a source Job")
    if trigger_type != "full_rerun" and rerun_of_job_id is not None:
        _fail("invalid_rerun_scope", "only full rerun may reference a source Job")
    if trigger_type == "eval" and execution_mode != "eval":
        _fail("invalid_execution_mode", "eval trigger requires eval execution mode")
    if trigger_type != "eval" and execution_mode == "eval":
        _fail("invalid_execution_mode", "eval execution mode requires eval trigger")
    if not settings.graph_extraction_enabled:
        _fail("graph_extraction_disabled", "graph extraction is disabled globally")
    if getattr(library, "deleted_at", None) is not None:
        _fail("library_deleted", "Library is deleted")
    if not library.graph_extraction_enabled:
        _fail("library_graph_disabled", "Library graph extraction is disabled")
    if not library.external_llm_enabled:
        _fail("library_external_llm_disabled", "Library external LLM access is disabled")
    allowed_levels = library.graph_extraction_allowed_security_levels
    if not isinstance(allowed_levels, list) or not allowed_levels:
        _fail("security_allowlist_empty", "Library security allowlist is empty")
    if document.library_id != library.id or revision.library_id != library.id:
        _fail("scope_mismatch", "document and revision must belong to the Library")
    if revision.document_id != document.id:
        _fail("scope_mismatch", "revision must belong to the document")
    if document.deleted_at is not None:
        _fail("document_deleted", "document is deleted")
    if document.current_revision_id != revision.id:
        _fail("historical_revision", "only the current revision may be extracted")
    if revision.status != _READY or document.status != _READY:
        _fail("revision_not_ready", "current document revision must be ready")
    security_level = revision.security_level
    if not isinstance(security_level, str) or not security_level.strip():
        _fail("security_level_missing", "current revision security level is required")
    if security_level.strip() not in allowed_levels:
        _fail("security_level_denied", "current revision security level is not allowed")


_BUILD_MODE_SETTINGS = {
    "fast": {"batch_size": 8, "max_output_tokens": 2_500},
    "standard": {"batch_size": 6, "max_output_tokens": 4_000},
    "deep": {"batch_size": 3, "max_output_tokens": 8_000},
}


def _build_mode(value: str | None) -> str:
    mode = value or settings.graph_extraction_default_build_mode
    if mode not in _BUILD_MODE_SETTINGS:
        _fail("invalid_build_mode", "build mode must be fast, standard, or deep")
    return mode


def _model_config_snapshot(*, build_mode: str | None = None) -> dict[str, Any]:
    from app.services.graph_extraction_batch_eval import (
        BATCH_PROMPT_VERSION,
        SCHEMA_ROUTER_VERSION,
        batch_graph_extraction_prompt_hash,
    )

    mode = _build_mode(build_mode)
    mode_settings = _BUILD_MODE_SETTINGS[mode]
    snapshot = {
        "provider": graph_extraction_provider_name(
            base_url=settings.graph_extraction_base_url,
            model=settings.graph_extraction_model,
        ),
        "base_url": settings.graph_extraction_base_url,
        "model": settings.graph_extraction_model,
        "timeout_seconds": settings.graph_extraction_timeout_seconds,
        "temperature": settings.graph_extraction_temperature,
        "response_format": settings.graph_extraction_response_format,
        "max_context_chars": settings.graph_extraction_max_context_chars,
        "context_window_tokens": settings.graph_extraction_context_window_tokens,
        "previous_chunks": settings.graph_extraction_previous_chunks,
        "next_chunks": settings.graph_extraction_next_chunks,
        "build_mode": mode,
        "unit_planning_version": "v1",
        "batch_prompt_version": BATCH_PROMPT_VERSION,
        "batch_prompt_hash": batch_graph_extraction_prompt_hash(),
        "schema_routing_version": (
            SCHEMA_ROUTER_VERSION if settings.graph_extraction_schema_routing_enabled else "full-v1"
        ),
        "schema_routing_enabled": settings.graph_extraction_schema_routing_enabled,
        "cache_policy_version": ("semantic-v1" if settings.graph_extraction_cache_enabled else "disabled-v1"),
    }
    if settings.graph_extraction_output_budget_enabled:
        snapshot["max_output_tokens"] = min(
            settings.graph_extraction_max_output_tokens,
            mode_settings["max_output_tokens"],
        )
    snapshot["batch_size"] = min(settings.graph_extraction_batch_size, mode_settings["batch_size"])
    return snapshot


def _policy_config_snapshot(*, candidate_review_policy: str, build_mode: str | None = None) -> dict[str, Any]:
    mode = _build_mode(build_mode)
    evidence_types = sorted(
        {item.strip() for item in settings.graph_extraction_auto_evidence_types.split(",") if item.strip()}
    )
    snapshot = {
        "confidence_weights": {
            "model": settings.graph_extraction_weight_model,
            "evidence": settings.graph_extraction_weight_evidence,
            "schema": settings.graph_extraction_weight_schema,
            "normalization": settings.graph_extraction_weight_normalization,
        },
        "evidence_score_map": {"direct_statement": 1.0, "table_cell": 0.95},
        "schema_score_map": {
            "valid": 1.0,
            "warning": 0.6,
            "boundary_unclear": 0.4,
            "invalid": 0.0,
        },
        "normalization_score_map": {
            "exact_normalized_match": 1.0,
            "exact_alias_match": 0.95,
            "new_entity": 0.9,
            "ambiguous": 0.0,
        },
        "entity_materialization_threshold": (settings.graph_extraction_entity_materialization_threshold),
        "relation_draft_threshold": settings.graph_extraction_relation_draft_threshold,
        "auto_evidence_types": evidence_types,
        "evidence_group_policy": settings.graph_extraction_evidence_group_policy,
        "candidate_review_policy": candidate_review_policy,
        "build_mode": mode,
    }
    if settings.graph_extraction_center_only_enabled:
        snapshot["center_only"] = True
    return snapshot


def build_full_rerun_idempotency_key(
    source_job_id: uuid.UUID,
    client_idempotency_key: str,
) -> str:
    if not isinstance(client_idempotency_key, str):
        _fail("idempotency_key_required", "full rerun idempotency key is required")
    value = client_idempotency_key.strip()
    if len(value) < 8 or len(value) > 128:
        _fail(
            "invalid_idempotency_key",
            "full rerun idempotency key must contain 8 to 128 characters",
        )
    return _hash(
        {
            "action": "full_rerun",
            "source_job_id": str(source_job_id),
            "client_idempotency_key": value,
        }
    )


async def _job_by_idempotency_key(db, value: str) -> GraphExtractionJob | None:
    result = await db.execute(
        select(GraphExtractionJob).where(GraphExtractionJob.idempotency_key == value).limit(1)
    )
    return result.scalars().first()


def _require_existing_scope(
    job: Any,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
) -> None:
    if (
        getattr(job, "library_id", library.id) != library.id
        or getattr(job, "document_id", document.id) != document.id
        or getattr(job, "document_revision_id", revision.id) != revision.id
    ):
        _fail(
            "idempotency_key_conflict",
            "idempotency key already belongs to a different extraction scope",
        )


async def create_graph_extraction_job(
    db,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    trigger_type: str,
    execution_mode: str,
    requested_by: Any,
    idempotency_key: str | None,
    rerun_of_job_id: uuid.UUID | None = None,
    build_mode: str | None = None,
    ontology_version: OntologyVersion | None = None,
    ontology_snapshot: dict[str, Any] | None = None,
    ontology_snapshot_hash: str | None = None,
    schema_discovery_run_id: uuid.UUID | None = None,
    waiting_schema: bool = False,
) -> GraphExtractionJob:
    _validate_creation_scope(
        library=library,
        document=document,
        revision=revision,
        trigger_type=trigger_type,
        execution_mode=execution_mode,
        rerun_of_job_id=rerun_of_job_id,
    )

    stored_idempotency_key: str | None = None
    if trigger_type == "full_rerun":
        assert rerun_of_job_id is not None
        stored_idempotency_key = build_full_rerun_idempotency_key(
            rerun_of_job_id,
            idempotency_key or "",
        )
    elif idempotency_key is not None:
        stored_idempotency_key = idempotency_key.strip()
        if not stored_idempotency_key or len(stored_idempotency_key) > 128:
            _fail(
                "invalid_idempotency_key",
                "idempotency key must contain 1 to 128 characters",
            )

    if stored_idempotency_key is not None:
        existing = await _job_by_idempotency_key(db, stored_idempotency_key)
        if existing is not None:
            _require_existing_scope(
                existing,
                library=library,
                document=document,
                revision=revision,
            )
            return existing

    if trigger_type == "full_rerun":
        source_job = await db.get(GraphExtractionJob, rerun_of_job_id)
        if source_job is None:
            _fail("rerun_source_not_found", "source graph extraction Job was not found")
        try:
            _require_existing_scope(
                source_job,
                library=library,
                document=document,
                revision=revision,
            )
        except GraphExtractionJobError as exc:
            raise GraphExtractionJobError(
                "rerun_source_not_found",
                "source graph extraction Job was not found in this scope",
            ) from exc

    try:
        if ontology_version is not None:
            ontology = ontology_version
            if ontology.library_id != library.id:
                _fail("ontology_scope_mismatch", "ontology does not belong to the Library")
            if ontology_snapshot is None or ontology_snapshot_hash is None:
                _fail("invalid_ontology_snapshot", "coordinated Schema snapshot is required")
        else:
            ontology = await select_active_ontology(db, library=library)
        if ontology_version is None and getattr(library, "schema_mode", None) == "explore":
            _fail(
                "schema_discovery_run_required",
                "explore extraction must be coordinated by a SchemaDiscoveryRun",
            )
        if ontology_version is None and (
            getattr(library, "schema_mode", None) == "governed"
            and str(getattr(ontology, "version_key", "")).startswith("ai-draft")
        ):
            _fail(
                "active_ontology_required",
                "AI Schema draft must be confirmed before strict extraction",
            )
        elif ontology_version is None:
            ontology_snapshot, ontology_snapshot_hash = await build_ontology_rule_snapshot(
                db,
                library=library,
                ontology_version_id=ontology.id,
            )
    except GraphExtractionJobError:
        raise
    unit_plans = await plan_graph_extraction_units(
        db,
        library=library,
        document=document,
        revision=revision,
    )
    mode = _build_mode(build_mode)
    model_snapshot = _model_config_snapshot(build_mode=mode)
    if getattr(ontology, "status", None) == "draft" or waiting_schema:
        model_snapshot["schema_discovery"] = "pending"
        model_snapshot["schema_state"] = "ai_draft"
    policy_snapshot = _policy_config_snapshot(
        candidate_review_policy=(
            "precision_first_auto" if execution_mode == "production" else "manual_review"
        ),
        build_mode=mode,
    )
    model_config_hash = _hash(model_snapshot)
    policy_config_hash = _hash(policy_snapshot)
    center_only = policy_snapshot.get("center_only", False)
    prompt_content_hash = graph_extraction_prompt_hash(center_only=center_only)

    versions = {
        "prompt_version": _validate_version(
            graph_extraction_prompt_version(center_only=center_only),
            label="prompt_version",
        ),
        "extractor_version": _validate_version(
            settings.graph_extraction_extractor_version,
            label="extractor_version",
        ),
        "output_parser_version": _validate_version(
            settings.graph_extraction_output_parser_version,
            label="output_parser_version",
        ),
        "context_policy_version": _validate_version(
            settings.graph_extraction_context_policy_version,
            label="context_policy_version",
        ),
        "extraction_policy_version": _validate_version(
            settings.graph_extraction_policy_version,
            label="extraction_policy_version",
        ),
        "normalization_rule_version": _validate_version(
            settings.graph_extraction_normalization_rule_version,
            label="normalization_rule_version",
        ),
        "confidence_policy_version": _validate_version(
            settings.graph_extraction_confidence_policy_version,
            label="confidence_policy_version",
        ),
    }
    if ontology_snapshot is not None:
        frozen_ontology_snapshot = ontology_snapshot
        frozen_ontology_snapshot_hash = ontology_snapshot_hash
    else:
        frozen_ontology_snapshot, frozen_ontology_snapshot_hash = await build_ontology_rule_snapshot(
            db,
            library=library,
            ontology_version_id=ontology.id,
            allow_draft=getattr(ontology, "status", None) == "draft",
        )
    input_fingerprint = _hash(
        {
            "library_id": str(library.id),
            "document_id": str(document.id),
            "document_revision_id": str(revision.id),
            "ontology_version_id": str(ontology.id),
            "execution_mode": execution_mode,
            "unit_fingerprints": [row.unit_fingerprint for row in unit_plans],
            **versions,
            "model_config_hash": model_config_hash,
            "policy_config_hash": policy_config_hash,
            "ontology_snapshot_hash": frozen_ontology_snapshot_hash,
            "prompt_content_hash": prompt_content_hash,
            "document_parser_version": revision.parser_version,
            "chunking_strategy_version": revision.chunking_strategy_version,
        }
    )

    if trigger_type != "full_rerun":
        duplicate_result = await db.execute(
            select(GraphExtractionJob)
            .where(GraphExtractionJob.input_fingerprint == input_fingerprint)
            .order_by(GraphExtractionJob.created_at.asc(), GraphExtractionJob.id.asc())
            .limit(1)
        )
        duplicate = duplicate_result.scalars().first()
        if duplicate is not None:
            _require_existing_scope(
                duplicate,
                library=library,
                document=document,
                revision=revision,
            )
            return duplicate

    stored_idempotency_key = stored_idempotency_key or input_fingerprint
    requested_by_id = getattr(requested_by, "id", requested_by)
    job = GraphExtractionJob(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        ontology_version_id=ontology.id,
        trigger_type=trigger_type,
        execution_mode=execution_mode,
        status="waiting_schema" if waiting_schema else "queued",
        current_stage="waiting_schema" if waiting_schema else "preparing",
        input_fingerprint=input_fingerprint,
        idempotency_key=stored_idempotency_key,
        rerun_of_job_id=rerun_of_job_id,
        retry_generation=0,
        model_provider=model_snapshot["provider"],
        model_name=settings.graph_extraction_model,
        **versions,
        document_parser_version=revision.parser_version,
        chunking_strategy_version=revision.chunking_strategy_version,
        model_config_snapshot=model_snapshot,
        policy_config_snapshot=policy_snapshot,
        model_config_hash=model_config_hash,
        policy_config_hash=policy_config_hash,
        ontology_snapshot=frozen_ontology_snapshot,
        ontology_snapshot_hash=frozen_ontology_snapshot_hash,
        schema_discovery_run_id=schema_discovery_run_id,
        prompt_content_hash=prompt_content_hash,
        requested_by=requested_by_id,
        counts={
            "total": len(unit_plans),
            "queued": len(unit_plans),
            "processing": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        },
        statistics={},
    )
    db.add(job)
    await db.flush()
    for plan in unit_plans:
        db.add(
            GraphExtractionUnit(
                job_id=job.id,
                library_id=library.id,
                document_revision_id=revision.id,
                ordinal=plan.ordinal,
                center_chunk_id=plan.center_chunk_id,
                center_evidence_id=plan.center_evidence_id,
                unit_fingerprint=plan.unit_fingerprint,
                status="queued",
                model_attempt_count=0,
                retryable=False,
            )
        )
    await db.flush()
    return job


async def get_graph_extraction_job(
    db,
    *,
    library: Library,
    job_id: uuid.UUID,
    for_update: bool = False,
) -> GraphExtractionJob:
    statement = select(GraphExtractionJob).where(
        GraphExtractionJob.id == job_id,
        GraphExtractionJob.library_id == library.id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    job = result.scalars().first()
    if job is None:
        _fail("job_not_found", "graph extraction Job was not found")
    return job


async def _job_unit_counts(db, *, job_id: uuid.UUID) -> dict[str, int]:
    result = await db.execute(
        select(GraphExtractionUnit.status, func.count(GraphExtractionUnit.id))
        .where(GraphExtractionUnit.job_id == job_id)
        .group_by(GraphExtractionUnit.status)
    )
    by_status = {status: int(count) for status, count in result.all()}
    return {
        "total": sum(by_status.values()),
        "queued": by_status.get("queued", 0),
        "processing": by_status.get("processing", 0),
        "succeeded": by_status.get("succeeded", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
    }


async def retry_graph_extraction_job(
    db,
    *,
    library: Library,
    job_id: uuid.UUID,
    max_attempts: int | None = None,
    now: datetime | None = None,
) -> GraphExtractionJob:
    max_attempts = max_attempts or settings.graph_extraction_worker_max_model_attempts
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        _fail("invalid_attempt_budget", "model attempt budget must be positive")
    retried_at = now or datetime.now(timezone.utc)
    job = await get_graph_extraction_job(
        db,
        library=library,
        job_id=job_id,
        for_update=True,
    )
    if job.status not in {"failed", "partially_succeeded"}:
        _fail("job_not_retryable", "graph extraction Job is not retryable")
    result = await db.execute(
        select(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.job_id == job.id,
            GraphExtractionUnit.status == "failed",
            GraphExtractionUnit.retryable.is_(True),
            GraphExtractionUnit.model_attempt_count < max_attempts,
        )
        .order_by(GraphExtractionUnit.ordinal)
        .with_for_update()
    )
    units = list(result.scalars().all())
    if not units:
        _fail("no_retryable_units", "no failed Units have remaining attempt budget")
    for unit in units:
        unit.status = "queued"
        unit.retryable = False
        unit.worker_id = None
        unit.claim_token = None
        unit.claimed_at = None
        unit.lease_expires_at = None
        unit.error_code = None
        unit.error_message = None
        unit.finished_at = None
    job.status = "queued"
    job.current_stage = "preparing"
    job.retry_generation += 1
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    job.updated_at = retried_at
    await db.flush()
    job.counts = await _job_unit_counts(db, job_id=job.id)
    return job


async def cancel_graph_extraction_job(
    db,
    *,
    library: Library,
    job_id: uuid.UUID,
    now: datetime | None = None,
) -> GraphExtractionJob:
    cancelled_at = now or datetime.now(timezone.utc)
    job = await get_graph_extraction_job(
        db,
        library=library,
        job_id=job_id,
        for_update=True,
    )
    if job.status == "cancelled":
        return job
    if job.status not in {"queued", "processing"}:
        _fail("job_not_cancellable", "graph extraction Job is already terminal")
    unit_ids = select(GraphExtractionUnit.id).where(GraphExtractionUnit.job_id == job.id)
    await db.execute(
        update(ExtractionRawOutputAttempt)
        .where(
            ExtractionRawOutputAttempt.extraction_unit_id.in_(unit_ids),
            ExtractionRawOutputAttempt.request_status == "pending",
        )
        .values(
            request_status="abandoned",
            abandoned_at=cancelled_at,
            abandoned_reason="unit_cancelled",
            updated_at=cancelled_at,
        )
    )
    await db.execute(
        update(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.job_id == job.id,
            GraphExtractionUnit.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            retryable=False,
            worker_id=None,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
            error_code="unit_cancelled",
            error_message=None,
            finished_at=cancelled_at,
            updated_at=cancelled_at,
        )
    )
    job.status = "cancelled"
    job.current_stage = "finalizing"
    job.error_code = "user_cancelled"
    job.error_message = None
    job.finished_at = cancelled_at
    job.updated_at = cancelled_at
    await db.flush()
    job.counts = await _job_unit_counts(db, job_id=job.id)
    return job
