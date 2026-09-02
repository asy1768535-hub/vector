"""Canonical-entity resolution with caller-owned transaction boundaries."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.canonical_entity import CanonicalEntity
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_CREATE_NEW,
    ENTITY_RESOLUTION_LINK_EXISTING,
    ENTITY_RESOLUTION_PENDING_REVIEW,
    ENTITY_RESOLUTION_REJECTED,
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    ENTITY_RESOLUTION_STATUS_SUPERSEDED,
    EntityResolutionDecision,
)
from app.models.entity_type import EntityType
from app.models.graph_candidates import GraphEntityCandidate
from app.models.library import Library
from app.services.graph_canonical import canonical_graph_json_v1, canonical_graph_value_hash_v1
from app.services.graph_normalization import normalize_graph_name_v1


RESOLVER_VERSION = "entity_resolution_v1"
MAX_CANDIDATE_SNAPSHOT = 50
MAX_EVIDENCE_REFS = 32
MAX_SNAPSHOT_JSON_BYTES = 64 * 1024
MAX_OBSERVED_NAME_LENGTH = 512
MAX_SOURCE_FINGERPRINT_LENGTH = 512
_RESOLUTION_LOCK_PREFIX = "vector-kb:canonical-entity-resolution:"
_REQUIRED_EVIDENCE_KEYS = {
    "document_id",
    "document_revision_id",
    "revision_id",
    "chunk_id",
    "evidence_id",
    "mention_id",
    "source_ref",
}
_FORBIDDEN_EVIDENCE_KEYS = {
    "body",
    "content",
    "evidence_text_snapshot",
    "mention_text",
    "quote",
    "quote_text",
    "raw_text",
    "text",
}


class CanonicalEntityResolutionError(ValueError):
    """Base error for malformed or non-persistable resolution input."""


class ResolutionScopeError(CanonicalEntityResolutionError):
    """The requested library or scope cannot be resolved safely."""


class ResolutionPersistenceError(CanonicalEntityResolutionError):
    """The decision could not be persisted without weakening invariants."""


class ResolutionConcurrencyError(ResolutionPersistenceError):
    """A concurrent resolver changed the subject and the caller should retry."""


@dataclass(frozen=True, slots=True)
class ExplicitIdentifier:
    namespace: str
    value: str
    issuer: str | None = None
    identity_semantics: str = "explicit"


@dataclass(frozen=True, slots=True)
class EntityResolutionInput:
    library_id: uuid.UUID
    observed_name: str
    observed_normalized_name: str
    source_fingerprint: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    observed_entity_type_id: uuid.UUID | None = None
    observed_entity_type_key: str | None = None
    observed_ontology_version_id: uuid.UUID | None = None
    existing_entity_id: uuid.UUID | None = None
    explicit_identifiers: tuple[ExplicitIdentifier, ...] = ()
    observed_properties: Mapping[str, Any] | None = None
    context: Mapping[str, Any] | None = None
    graph_entity_candidate_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class EntityResolutionResult:
    decision: EntityResolutionDecision
    canonical_entity: CanonicalEntity | None
    decision_created: bool
    canonical_created: bool


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    observed_name: str
    observed_normalized_name: str
    source_fingerprint: str
    evidence_refs: list[dict[str, Any]]
    identifier_snapshot: dict[str, Any] | None
    properties_snapshot: dict[str, Any] | None
    context_snapshot: dict[str, Any] | None
    subject_fingerprint: str
    validation_error: str | None


def _stable_json_value(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (uuid.UUID, datetime, date)):
        return str(value) if isinstance(value, uuid.UUID) else value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} JSON object keys must be strings")
            result[key] = _stable_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _bounded_snapshot(value: Any, *, field: str) -> Any:
    snapshot = _stable_json_value(value, path=field)
    if len(canonical_graph_json_v1(snapshot).encode("utf-8")) > MAX_SNAPSHOT_JSON_BYTES:
        raise ValueError(f"{field} is too large")
    return snapshot


def _safe_text(value: Any, *, max_length: int) -> str:
    if isinstance(value, str):
        return value.strip()[:max_length]
    return str(value)[:max_length]


def _prepare_input(request: EntityResolutionInput) -> _PreparedInput:
    raw_name = _safe_text(request.observed_name, max_length=MAX_OBSERVED_NAME_LENGTH)
    raw_normalized = _safe_text(
        request.observed_normalized_name,
        max_length=MAX_OBSERVED_NAME_LENGTH,
    )
    raw_source = _safe_text(
        request.source_fingerprint,
        max_length=MAX_SOURCE_FINGERPRINT_LENGTH,
    )
    errors: list[str] = []
    if not isinstance(request.observed_name, str) or not raw_name:
        errors.append("empty_observed_name")
    if not isinstance(request.observed_normalized_name, str) or not raw_normalized:
        errors.append("empty_observed_normalized_name")
    elif raw_name and normalize_graph_name_v1(raw_name) != raw_normalized:
        errors.append("normalization_mismatch")
    if not isinstance(request.source_fingerprint, str) or not raw_source:
        errors.append("invalid_source_fingerprint")

    try:
        identifiers = []
        for identifier in request.explicit_identifiers:
            if not isinstance(identifier, ExplicitIdentifier):
                raise TypeError("explicit identifiers must use ExplicitIdentifier")
            namespace = _safe_text(identifier.namespace, max_length=128)
            value = _safe_text(identifier.value, max_length=512)
            issuer = None if identifier.issuer is None else _safe_text(identifier.issuer, max_length=256)
            semantics = _safe_text(identifier.identity_semantics, max_length=64)
            if not namespace or not value or not semantics:
                raise ValueError("identifier namespace, value, and semantics are required")
            identifiers.append(
                {
                    "identity_semantics": semantics,
                    "issuer": issuer,
                    "namespace": namespace,
                    "value": value,
                }
            )
        identifiers.sort(key=lambda row: (row["namespace"], row["value"], row["issuer"] or "", row["identity_semantics"]))
        identifier_snapshot = {"identifiers": identifiers} if identifiers else None
    except (TypeError, ValueError):
        identifier_snapshot = None
        errors.append("invalid_identifier")

    evidence_refs: list[dict[str, Any]] = []
    if not isinstance(request.evidence_refs, (list, tuple)) or not request.evidence_refs:
        errors.append("missing_evidence")
    elif len(request.evidence_refs) > MAX_EVIDENCE_REFS:
        errors.append("too_many_evidence_refs")
    else:
        try:
            for ref in request.evidence_refs:
                if not isinstance(ref, Mapping) or not ref:
                    raise ValueError("evidence references must be non-empty mappings")
                stable_ref = _bounded_snapshot(ref, field="evidence_refs")
                if any(str(key).casefold() in _FORBIDDEN_EVIDENCE_KEYS for key in stable_ref):
                    raise ValueError("evidence references cannot contain raw text")
                if not set(stable_ref).intersection(_REQUIRED_EVIDENCE_KEYS):
                    raise ValueError("evidence references need a stable source identifier")
                evidence_refs.append(stable_ref)
        except (TypeError, ValueError):
            evidence_refs = []
            errors.append("invalid_evidence")

    properties_snapshot: dict[str, Any] | None = None
    context_snapshot: dict[str, Any] | None = None
    try:
        if request.observed_properties is not None:
            properties_snapshot = _bounded_snapshot(request.observed_properties, field="observed_properties")
            if not isinstance(properties_snapshot, dict):
                raise ValueError("observed_properties must be a mapping")
        if request.context is not None:
            context_snapshot = _bounded_snapshot(request.context, field="context")
            if not isinstance(context_snapshot, dict):
                raise ValueError("context must be a mapping")
    except (TypeError, ValueError):
        errors.append("invalid_observation_context")
        properties_snapshot = None
        context_snapshot = None

    subject_fingerprint = canonical_graph_value_hash_v1(
        {
            "contract": "entity_resolution_subject_v1",
            "existing_entity_id": str(request.existing_entity_id)
            if isinstance(request.existing_entity_id, uuid.UUID)
            else _safe_text(request.existing_entity_id, max_length=64)
            if request.existing_entity_id is not None
            else None,
            "library_id": str(request.library_id),
            "observed_entity_type_id": str(request.observed_entity_type_id)
            if isinstance(request.observed_entity_type_id, uuid.UUID)
            else _safe_text(request.observed_entity_type_id, max_length=64)
            if request.observed_entity_type_id is not None
            else None,
            "observed_entity_type_key": _safe_text(request.observed_entity_type_key, max_length=128)
            if request.observed_entity_type_key is not None
            else None,
            "observed_ontology_version_id": str(request.observed_ontology_version_id)
            if isinstance(request.observed_ontology_version_id, uuid.UUID)
            else _safe_text(request.observed_ontology_version_id, max_length=64)
            if request.observed_ontology_version_id is not None
            else None,
            "observed_name": raw_name,
            "observed_normalized_name": raw_normalized,
            "source_fingerprint": raw_source,
        }
    )
    return _PreparedInput(
        observed_name=raw_name,
        observed_normalized_name=raw_normalized,
        source_fingerprint=raw_source,
        evidence_refs=evidence_refs,
        identifier_snapshot=identifier_snapshot,
        properties_snapshot=properties_snapshot,
        context_snapshot=context_snapshot,
        subject_fingerprint=subject_fingerprint,
        validation_error=errors[0] if errors else None,
    )


def _decision_fingerprint(
    prepared: _PreparedInput,
    request: EntityResolutionInput,
    *,
    candidate_snapshot: list[dict[str, Any]],
    decision_kind: str,
    canonical_entity_id: uuid.UUID | None,
    method: str,
    reason_code: str | None,
) -> str:
    return canonical_graph_value_hash_v1(
        {
            "candidate_snapshot": candidate_snapshot,
            # CREATE NEW gets its UUID inside the transaction.  Including that
            # random value would make an identical replay non-idempotent.
            "canonical_entity_id": str(canonical_entity_id)
            if decision_kind == ENTITY_RESOLUTION_LINK_EXISTING and canonical_entity_id
            else None,
            "context": prepared.context_snapshot,
            "contract": "entity_resolution_decision_v1",
            "decision_kind": decision_kind,
            "evidence_refs": prepared.evidence_refs,
            "identifier_snapshot": prepared.identifier_snapshot,
            "method": method,
            "observed_entity_type_id": str(request.observed_entity_type_id)
            if isinstance(request.observed_entity_type_id, uuid.UUID)
            else None,
            "observed_entity_type_key": request.observed_entity_type_key,
            "observed_ontology_version_id": str(request.observed_ontology_version_id)
            if isinstance(request.observed_ontology_version_id, uuid.UUID)
            else None,
            "observed_name": prepared.observed_name,
            "observed_normalized_name": prepared.observed_normalized_name,
            "observed_properties": prepared.properties_snapshot,
            "reason_code": reason_code,
            "resolver_version": RESOLVER_VERSION,
            "subject_fingerprint": prepared.subject_fingerprint,
        }
    )


async def _scoped_rows(db, model: type[Any], library_id: uuid.UUID) -> list[Any]:
    result = await db.execute(select(model).where(model.library_id == library_id))
    return list(result.scalars().all())


def _resolution_subject_lock_key(library_id: uuid.UUID, subject_fingerprint: str) -> int:
    digest = hashlib.sha256(
        f"{_RESOLUTION_LOCK_PREFIX}{library_id}:{subject_fingerprint}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _database_dialect_name(db) -> str | None:
    try:
        bind = db.sync_session.get_bind()
    except (AttributeError, RuntimeError):
        try:
            bind = db.get_bind()
        except (AttributeError, RuntimeError):
            return None
    return getattr(getattr(bind, "dialect", None), "name", None)


async def _lock_resolution_subject(
    db,
    *,
    library_id: uuid.UUID,
    subject_fingerprint: str,
) -> None:
    dialect = _database_dialect_name(db)
    if dialect == "sqlite":
        return
    if dialect not in {None, "postgresql"}:
        raise ResolutionPersistenceError("resolution concurrency control requires PostgreSQL")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _resolution_subject_lock_key(library_id, subject_fingerprint)},
    )


async def _load_library(db, library_id: uuid.UUID) -> Library:
    library = await db.get(Library, library_id)
    if library is None:
        raise ResolutionScopeError("library_not_found")
    return library


async def _existing_decision(db, *, library_id: uuid.UUID, decision_fingerprint: str) -> EntityResolutionDecision | None:
    result = await db.execute(
        select(EntityResolutionDecision).where(
            EntityResolutionDecision.library_id == library_id,
            EntityResolutionDecision.decision_fingerprint == decision_fingerprint,
            EntityResolutionDecision.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def _active_subject_decision(db, *, library_id: uuid.UUID, subject_fingerprint: str) -> EntityResolutionDecision | None:
    decisions = await _scoped_rows(db, EntityResolutionDecision, library_id)
    active = [
        row
        for row in decisions
        if row.subject_fingerprint == subject_fingerprint
        and row.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE
    ]
    if len(active) > 1:
        raise ResolutionPersistenceError("multiple active decisions for one subject")
    return active[0] if active else None


async def _load_canonical(db, canonical_id: uuid.UUID | None) -> CanonicalEntity | None:
    if canonical_id is None:
        return None
    return await db.get(CanonicalEntity, canonical_id)


def _projection_snapshot(entity: Entity, type_by_id: Mapping[uuid.UUID, EntityType]) -> dict[str, Any]:
    entity_type = type_by_id.get(entity.entity_type_id)
    return {
        "entity_id": str(entity.id),
        "entity_type_id": str(entity.entity_type_id),
        "entity_type_key": entity_type.key if entity_type is not None else None,
        "ontology_version_id": str(entity.ontology_version_id),
    }


def _type_compatible(
    projections: list[Entity],
    type_by_id: Mapping[uuid.UUID, EntityType],
    request: EntityResolutionInput,
) -> bool:
    if not projections or (request.observed_entity_type_id is None and request.observed_entity_type_key is None):
        return True
    for projection in projections:
        if request.observed_entity_type_id is not None and projection.entity_type_id != request.observed_entity_type_id:
            continue
        if request.observed_entity_type_key is not None:
            entity_type = type_by_id.get(projection.entity_type_id)
            if entity_type is None or entity_type.key != request.observed_entity_type_key:
                continue
        return True
    return False


async def _candidate_snapshot(
    db,
    *,
    library_id: uuid.UUID,
    normalized_name: str,
    request: EntityResolutionInput,
    excluded_canonical_ids: set[uuid.UUID] | None = None,
) -> tuple[list[dict[str, Any]], list[CanonicalEntity]]:
    excluded_canonical_ids = excluded_canonical_ids or set()
    canonical_rows = [
        row
        for row in await _scoped_rows(db, CanonicalEntity, library_id)
        if row.status == "active" and row.normalized_name == normalized_name
    ]
    alias_rows = [
        row
        for row in await _scoped_rows(db, EntityAlias, library_id)
        if row.status == "active" and row.normalized_alias == normalized_name
    ]
    projection_rows = [
        row
        for row in await _scoped_rows(db, Entity, library_id)
        if row.canonical_entity_id is not None
        and row.status in {"draft", "pending_review", "active"}
    ]
    type_rows = await _scoped_rows(db, EntityType, library_id)
    type_by_id = {row.id: row for row in type_rows}
    aliases_by_canonical: dict[uuid.UUID, set[str]] = {}
    for alias in alias_rows:
        entity = await db.get(Entity, alias.entity_id)
        if entity is None or entity.library_id != library_id or entity.canonical_entity_id is None:
            continue
        aliases_by_canonical.setdefault(entity.canonical_entity_id, set()).add("entity_alias")

    by_id = {row.id: row for row in canonical_rows}
    for canonical_id in aliases_by_canonical:
        canonical = await db.get(CanonicalEntity, canonical_id)
        if canonical is not None and canonical.library_id == library_id and canonical.status == "active":
            by_id[canonical.id] = canonical

    projections_by_canonical: dict[uuid.UUID, list[Entity]] = {}
    for entity in projection_rows:
        if entity.canonical_entity_id in by_id:
            projections_by_canonical.setdefault(entity.canonical_entity_id, []).append(entity)

    all_candidates = sorted(by_id.values(), key=lambda row: str(row.id))
    all_candidates = [row for row in all_candidates if row.id not in excluded_canonical_ids]
    snapshot: list[dict[str, Any]] = []
    eligible: list[CanonicalEntity] = []
    for canonical in all_candidates:
        projections = sorted(
            projections_by_canonical.get(canonical.id, []),
            key=lambda row: str(row.id),
        )
        signals = {"canonical_name"}
        signals.update(aliases_by_canonical.get(canonical.id, set()))
        included = _type_compatible(projections, type_by_id, request)
        row = {
            "canonical_entity_id": str(canonical.id),
            "canonical_name": canonical.canonical_name,
            "excluded_reason": None if included else "type_incompatible",
            "included": included,
            "projections": [_projection_snapshot(entity, type_by_id) for entity in projections],
            "signals": sorted(signals),
            "source": "exact_name_or_alias",
        }
        snapshot.append(row)
        if included:
            eligible.append(canonical)
    if len(snapshot) > MAX_CANDIDATE_SNAPSHOT:
        snapshot = snapshot[:MAX_CANDIDATE_SNAPSHOT]
    return snapshot, eligible


async def _candidate_fk_for_decision(db, request: EntityResolutionInput) -> tuple[uuid.UUID | None, str | None]:
    candidate_id = request.graph_entity_candidate_id
    if candidate_id is None:
        return None, None
    if not isinstance(candidate_id, uuid.UUID):
        return None, "invalid_candidate_id"
    candidate = await db.get(GraphEntityCandidate, candidate_id)
    if candidate is None:
        # A candidate may already have been purge-redacted.  The decision keeps
        # its own stable snapshots and deliberately does not resurrect it.
        return None, None
    if candidate.library_id != request.library_id:
        return None, "candidate_scope_mismatch"
    return candidate.id, None


def _decision_row(
    prepared: _PreparedInput,
    request: EntityResolutionInput,
    *,
    candidate_id: uuid.UUID | None,
    candidate_snapshot: list[dict[str, Any]],
    decision_kind: str,
    canonical_entity_id: uuid.UUID | None,
    method: str,
    confidence: float | None,
    reason_code: str | None,
    entity_id: uuid.UUID | None,
    supersedes_decision_id: uuid.UUID | None,
) -> EntityResolutionDecision:
    fingerprint = _decision_fingerprint(
        prepared,
        request,
        candidate_snapshot=candidate_snapshot,
        decision_kind=decision_kind,
        canonical_entity_id=canonical_entity_id,
        method=method,
        reason_code=reason_code,
    )
    return EntityResolutionDecision(
        id=uuid.uuid4(),
        library_id=request.library_id,
        subject_fingerprint=prepared.subject_fingerprint,
        decision_fingerprint=fingerprint,
        graph_entity_candidate_id=candidate_id,
        entity_id=entity_id,
        canonical_entity_id=canonical_entity_id,
        observed_name=prepared.observed_name,
        observed_normalized_name=prepared.observed_normalized_name,
        observed_type_key=request.observed_entity_type_key,
        identifier_snapshot=prepared.identifier_snapshot,
        candidate_snapshot=candidate_snapshot,
        evidence_refs=prepared.evidence_refs,
        decision_kind=decision_kind,
        lifecycle_status=ENTITY_RESOLUTION_STATUS_ACTIVE,
        method=method,
        confidence=confidence,
        reason_code=reason_code,
        resolver_version=RESOLVER_VERSION,
        supersedes_decision_id=supersedes_decision_id,
    )


async def _persist(
    db,
    prepared: _PreparedInput,
    request: EntityResolutionInput,
    *,
    candidate_id: uuid.UUID | None,
    candidate_snapshot: list[dict[str, Any]],
    decision_kind: str,
    canonical_entity: CanonicalEntity | None,
    method: str,
    confidence: float | None,
    reason_code: str | None,
    entity_id: uuid.UUID | None,
) -> EntityResolutionResult:
    canonical_created = canonical_entity is None and decision_kind == ENTITY_RESOLUTION_CREATE_NEW
    old_active = await _active_subject_decision(
        db,
        library_id=request.library_id,
        subject_fingerprint=prepared.subject_fingerprint,
    )
    decision = _decision_row(
        prepared,
        request,
        candidate_id=candidate_id,
        candidate_snapshot=candidate_snapshot,
        decision_kind=decision_kind,
        canonical_entity_id=canonical_entity.id if canonical_entity is not None else None,
        method=method,
        confidence=confidence,
        reason_code=reason_code,
        entity_id=entity_id,
        supersedes_decision_id=old_active.id if old_active is not None else None,
    )
    existing = await _existing_decision(
        db,
        library_id=request.library_id,
        decision_fingerprint=decision.decision_fingerprint,
    )
    if existing is not None:
        existing_canonical = await _load_canonical(db, existing.canonical_entity_id)
        return EntityResolutionResult(existing, existing_canonical, False, False)
    if canonical_created:
        canonical_entity = CanonicalEntity(
            id=uuid.uuid4(),
            library_id=request.library_id,
            canonical_name=prepared.observed_name,
            normalized_name=prepared.observed_normalized_name,
            status="active",
        )
        decision.canonical_entity_id = canonical_entity.id

    try:
        async with db.begin_nested():
            if old_active is not None:
                old_active.lifecycle_status = ENTITY_RESOLUTION_STATUS_SUPERSEDED
            if canonical_created:
                db.add(canonical_entity)
            db.add(decision)
            await db.flush()
    except IntegrityError as exc:
        existing = await _existing_decision(
            db,
            library_id=request.library_id,
            decision_fingerprint=decision.decision_fingerprint,
        )
        if existing is not None:
            existing_canonical = await _load_canonical(db, existing.canonical_entity_id)
            return EntityResolutionResult(existing, existing_canonical, False, False)
        active = await _active_subject_decision(
            db,
            library_id=request.library_id,
            subject_fingerprint=prepared.subject_fingerprint,
        )
        if active is not None:
            raise ResolutionConcurrencyError("active subject changed concurrently; retry resolution") from exc
        raise ResolutionPersistenceError("resolution decision could not be persisted") from exc

    return EntityResolutionResult(decision, canonical_entity, True, canonical_created)


async def resolve_canonical_entity(db, request: EntityResolutionInput) -> EntityResolutionResult:
    """Resolve one extracted observation without changing extraction/materialization.

    The caller owns the outer transaction. A transaction advisory lock keeps
    each resolution subject serial while unrelated subjects remain concurrent.
    """

    if not isinstance(request.library_id, uuid.UUID):
        raise ResolutionScopeError("library_id must be a UUID")
    prepared = _prepare_input(request)
    await _load_library(db, request.library_id)
    await _lock_resolution_subject(
        db,
        library_id=request.library_id,
        subject_fingerprint=prepared.subject_fingerprint,
    )

    candidate_id, candidate_error = await _candidate_fk_for_decision(db, request)
    if candidate_error is not None:
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=None,
            candidate_snapshot=[],
            decision_kind=ENTITY_RESOLUTION_REJECTED,
            canonical_entity=None,
            method="validation",
            confidence=None,
            reason_code=candidate_error,
            entity_id=None,
        )

    if request.observed_entity_type_id is not None:
        if not isinstance(request.observed_entity_type_id, uuid.UUID):
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="invalid_entity_type_id",
                entity_id=None,
            )
        observed_type = await db.get(EntityType, request.observed_entity_type_id)
        if observed_type is None or observed_type.library_id != request.library_id:
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="entity_type_scope_mismatch",
                entity_id=None,
            )
        if request.observed_entity_type_key is not None and observed_type.key != request.observed_entity_type_key:
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="entity_type_mismatch",
                entity_id=None,
            )

    if request.observed_ontology_version_id is not None and not isinstance(
        request.observed_ontology_version_id, uuid.UUID
    ):
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=candidate_id,
            candidate_snapshot=[],
            decision_kind=ENTITY_RESOLUTION_REJECTED,
            canonical_entity=None,
            method="validation",
            confidence=None,
            reason_code="invalid_ontology_version_id",
            entity_id=None,
        )

    if prepared.validation_error is not None:
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=candidate_id,
            candidate_snapshot=[],
            decision_kind=ENTITY_RESOLUTION_REJECTED,
            canonical_entity=None,
            method="validation",
            confidence=None,
            reason_code=prepared.validation_error,
            entity_id=None,
        )

    existing_entity = None
    if request.existing_entity_id is not None:
        if not isinstance(request.existing_entity_id, uuid.UUID):
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="invalid_existing_entity_id",
                entity_id=None,
            )
        existing_entity = await db.get(Entity, request.existing_entity_id)
        if existing_entity is None:
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="existing_entity_not_found",
                entity_id=None,
            )
        if existing_entity.library_id != request.library_id:
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="entity_scope_mismatch",
                entity_id=None,
            )

        mapped = await _load_canonical(db, existing_entity.canonical_entity_id)
        if existing_entity.canonical_entity_id is not None and (
            mapped is None or mapped.library_id != request.library_id
        ):
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=[],
                decision_kind=ENTITY_RESOLUTION_REJECTED,
                canonical_entity=None,
                method="validation",
                confidence=None,
                reason_code="canonical_scope_mismatch",
                entity_id=existing_entity.id,
            )
        type_rows = await _scoped_rows(db, EntityType, request.library_id)
        type_by_id = {row.id: row for row in type_rows}
        mapping_type_compatible = _type_compatible([existing_entity], type_by_id, request)
        if mapped is not None and mapped.status == "active" and mapping_type_compatible:
            snapshot = [
                {
                    "canonical_entity_id": str(mapped.id),
                    "canonical_name": mapped.canonical_name,
                    "excluded_reason": None,
                    "included": True,
                    "projections": [_projection_snapshot(existing_entity, type_by_id)],
                    "signals": ["existing_mapping"],
                    "source": "existing_entity_mapping",
                }
            ]
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=snapshot,
                decision_kind=ENTITY_RESOLUTION_LINK_EXISTING,
                canonical_entity=mapped,
                method="existing_mapping",
                confidence=1.0,
                reason_code=None,
                entity_id=existing_entity.id,
            )

    active_subject = await _active_subject_decision(
        db,
        library_id=request.library_id,
        subject_fingerprint=prepared.subject_fingerprint,
    )
    excluded_canonical_ids = {
        active_subject.canonical_entity_id
    } if active_subject is not None and active_subject.decision_kind == ENTITY_RESOLUTION_CREATE_NEW and active_subject.canonical_entity_id else set()
    candidate_snapshot, eligible = await _candidate_snapshot(
        db,
        library_id=request.library_id,
        normalized_name=prepared.observed_normalized_name,
        request=request,
        excluded_canonical_ids=excluded_canonical_ids,
    )
    if len(eligible) == 1:
        if prepared.identifier_snapshot is not None:
            return await _persist(
                db,
                prepared,
                request,
                candidate_id=candidate_id,
                candidate_snapshot=candidate_snapshot,
                decision_kind=ENTITY_RESOLUTION_PENDING_REVIEW,
                canonical_entity=None,
                method="candidate_retrieval",
                confidence=None,
                reason_code="identifier_not_resolvable",
                entity_id=existing_entity.id if existing_entity is not None else None,
            )
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=candidate_id,
            candidate_snapshot=candidate_snapshot,
            decision_kind=ENTITY_RESOLUTION_PENDING_REVIEW,
            canonical_entity=None,
            method="candidate_retrieval",
            confidence=None,
            reason_code="weak_identity_evidence",
            entity_id=existing_entity.id if existing_entity is not None else None,
        )
    if len(eligible) > 1:
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=candidate_id,
            candidate_snapshot=candidate_snapshot,
            decision_kind=ENTITY_RESOLUTION_PENDING_REVIEW,
            canonical_entity=None,
            method="candidate_retrieval",
            confidence=None,
            reason_code="multiple_candidates",
            entity_id=existing_entity.id if existing_entity is not None else None,
        )

    if request.observed_ontology_version_id is not None and any(
        any(
            projection["ontology_version_id"]
            != str(request.observed_ontology_version_id)
            for projection in candidate.get("projections", [])
        )
        for candidate in candidate_snapshot
    ):
        return await _persist(
            db,
            prepared,
            request,
            candidate_id=candidate_id,
            candidate_snapshot=candidate_snapshot,
            decision_kind=ENTITY_RESOLUTION_PENDING_REVIEW,
            canonical_entity=None,
            method="candidate_retrieval",
            confidence=None,
            reason_code="cross_ontology_weak_identity",
            entity_id=existing_entity.id if existing_entity is not None else None,
        )

    reason_code = "no_compatible_candidate" if candidate_snapshot else "no_candidate"
    return await _persist(
        db,
        prepared,
        request,
        candidate_id=candidate_id,
        candidate_snapshot=candidate_snapshot,
        decision_kind=ENTITY_RESOLUTION_CREATE_NEW,
        canonical_entity=None,
        method="no_reliable_match",
        confidence=0.0,
        reason_code=reason_code,
        entity_id=existing_entity.id if existing_entity is not None else None,
    )


# Keep this module's public constants discoverable to later callers without
# requiring them to import the ORM module directly.
__all__ = [
    "CanonicalEntityResolutionError",
    "EntityResolutionInput",
    "EntityResolutionResult",
    "ExplicitIdentifier",
    "ResolutionConcurrencyError",
    "ResolutionPersistenceError",
    "ResolutionScopeError",
    "RESOLVER_VERSION",
    "resolve_canonical_entity",
]
