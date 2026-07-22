from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, or_, select

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import (
    GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
    GraphPublication,
)
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, OntologyVersion
from app.models.revision_purge_operation import RevisionPurgeOperation
from app.models.revision_retention import RevisionRetentionRecord
from app.services import audit_log
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    GraphPublicationSnapshot,
    build_graph_publication_snapshot,
    graph_publication_snapshot_manifest_hash,
    plan_explicit_graph_publication_snapshot,
)
from app.services.revision_cleanup import (
    LockedCleanupScope,
    cleanup_now,
    cleanup_scope_error,
    lock_cleanup_scope,
    require_cleanup_worker_id,
)
from app.services.revision_retention import refresh_revision_retention


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_CURRENT_PUBLICATION_STATUSES = ("active", "degraded")


class CoordinatedPurgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class CoordinatedPurgePreview:
    retention_record_id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_file_id: uuid.UUID
    source_publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    include_drafts: bool
    source_manifest_hash: str
    replacement_manifest_hash: str
    impact_snapshot: dict[str, object]
    impact_hash: str
    confirmation_hash: str
    replacement_snapshot: GraphPublicationSnapshot = field(repr=False)


@dataclass(frozen=True, slots=True)
class CoordinatedPurgePlanResult:
    operation: RevisionPurgeOperation
    preview: CoordinatedPurgePreview
    reused: bool = False


@dataclass(frozen=True, slots=True)
class CoordinatedPurgeClaim:
    operation_id: uuid.UUID
    library_id: uuid.UUID
    retention_record_id: uuid.UUID
    document_revision_id: uuid.UUID
    source_publication_id: uuid.UUID
    replacement_publication_id: uuid.UUID
    replacement_manifest_hash: str
    confirmation_hash: str
    claim_token: uuid.UUID
    attempt_count: int


@dataclass(frozen=True, slots=True)
class CoordinatedPurgeExecutionResult:
    operation_id: uuid.UUID
    status: str
    code: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _item_key(item: GraphPublicationItem) -> tuple[str, uuid.UUID]:
    if item.item_kind == "entity" and item.entity_id is not None:
        return item.item_kind, item.entity_id
    if item.item_kind == "relation" and item.relation_id is not None:
        return item.item_kind, item.relation_id
    raise CoordinatedPurgeError(
        "publication_item_invalid", "publication item target is invalid"
    )


def _clone_item(item: GraphPublicationItem) -> GraphPublicationItem:
    return GraphPublicationItem(
        library_id=item.library_id,
        ontology_version_id=item.ontology_version_id,
        item_kind=item.item_kind,
        entity_id=item.entity_id,
        relation_id=item.relation_id,
        item_hash=item.item_hash,
        status="planned",
        support_evidence_ids=copy.deepcopy(item.support_evidence_ids or []),
        support_counts=copy.deepcopy(item.support_counts or {}),
        fact_snapshot=copy.deepcopy(item.fact_snapshot or {}),
    )


def _support_ids(item: GraphPublicationItem) -> set[uuid.UUID]:
    try:
        return {
            uuid.UUID(str(value)) for value in (item.support_evidence_ids or [])
        }
    except (TypeError, ValueError, AttributeError):
        raise CoordinatedPurgeError(
            "publication_item_invalid", "publication support Evidence is invalid"
        ) from None


def _relation_endpoint_ids(item: GraphPublicationItem) -> set[uuid.UUID]:
    if item.item_kind != "relation":
        return set()
    snapshot = dict(item.fact_snapshot or {})
    values = (snapshot.get("source_entity_id"), snapshot.get("target_entity_id"))
    try:
        return {uuid.UUID(str(value)) for value in values if value is not None}
    except (TypeError, ValueError, AttributeError):
        raise CoordinatedPurgeError(
            "publication_item_invalid", "relation endpoints are invalid"
        ) from None


def build_minimal_replacement_snapshot(
    *,
    source_items: Iterable[GraphPublicationItem],
    candidate: GraphPublicationSnapshot,
    target_evidence_ids: set[uuid.UUID],
    max_affected_items: int,
) -> tuple[GraphPublicationSnapshot, dict[str, object], tuple[str, ...]]:
    source_rows = list(source_items)
    source_by_key = {_item_key(item): item for item in source_rows}
    if len(source_by_key) != len(source_rows):
        raise CoordinatedPurgeError(
            "publication_item_duplicate", "source publication contains duplicate items"
        )
    candidate_by_key = {_item_key(item): item for item in candidate.items}
    directly_affected = {
        key
        for key, item in source_by_key.items()
        if _support_ids(item).intersection(target_evidence_ids)
    }
    affected_entity_ids = {
        key[1] for key in directly_affected if key[0] == "entity"
    }
    affected = set(directly_affected)
    for key, item in source_by_key.items():
        if _relation_endpoint_ids(item).intersection(affected_entity_ids):
            affected.add(key)
    if not affected:
        raise CoordinatedPurgeError(
            "affected_publication_not_found",
            "current publication does not cite target Evidence",
        )
    if len(affected) > max_affected_items:
        raise CoordinatedPurgeError(
            "affected_item_limit_exceeded", "affected publication item limit exceeded"
        )

    replacement_items: list[GraphPublicationItem] = []
    changed: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for key in sorted(source_by_key, key=lambda value: (value[0], str(value[1]))):
        source = source_by_key[key]
        current = candidate_by_key.get(key)
        logical_key = f"{key[0]}:{key[1]}"
        if key not in affected:
            if current is None or current.item_hash != source.item_hash:
                raise CoordinatedPurgeError(
                    "unrelated_publication_drift",
                    "unrelated publication item changed",
                )
            replacement_items.append(_clone_item(source))
            unchanged += 1
            continue
        if current is None:
            removed.append(logical_key)
            continue
        if _support_ids(current).intersection(target_evidence_ids):
            raise CoordinatedPurgeError(
                "target_evidence_still_eligible",
                "replacement still cites target Evidence",
            )
        replacement_items.append(_clone_item(current))
        if current.item_hash != source.item_hash:
            changed.append(logical_key)

    replacement = GraphPublicationSnapshot(
        items=tuple(replacement_items),
        blocked_counts={
            **dict(candidate.blocked_counts),
            "coordinated_purge_removed_items": len(removed),
        },
        policy_snapshot=copy.deepcopy(candidate.policy_snapshot),
        policy_snapshot_hash=candidate.policy_snapshot_hash,
    )
    affected_keys = tuple(
        f"{kind}:{item_id}"
        for kind, item_id in sorted(affected, key=lambda value: (value[0], str(value[1])))
    )
    diff = {
        "source_item_count": len(source_by_key),
        "replacement_item_count": len(replacement_items),
        "affected_item_count": len(affected),
        "affected_entity_count": sum(key[0] == "entity" for key in affected),
        "affected_relation_count": sum(key[0] == "relation" for key in affected),
        "recomputed_item_count": len(changed),
        "removed_item_count": len(removed),
        "unchanged_item_count": unchanged,
        "candidate_only_item_count": len(set(candidate_by_key) - set(source_by_key)),
    }
    return replacement, diff, affected_keys


async def _load_scope_unlocked(
    db,
    record_id: uuid.UUID,
) -> LockedCleanupScope | None:
    record = await db.get(RevisionRetentionRecord, record_id)
    if record is None:
        return None
    library = await db.get(Library, record.library_id)
    document = await db.get(Document, record.document_id)
    old_revision = await db.get(DocumentRevision, record.document_revision_id)
    replacement = await db.get(DocumentRevision, record.replacement_revision_id)
    revision_file = await db.get(DocumentRevisionFile, record.revision_file_id)
    if any(
        value is None
        for value in (
            library,
            document,
            old_revision,
            replacement,
            revision_file,
        )
    ):
        return None
    return LockedCleanupScope(
        library=library,
        record=record,
        document=document,
        old_revision=old_revision,
        replacement_revision=replacement,
        revision_file=revision_file,
    )


def _validate_scope(scope: LockedCleanupScope, current_time: datetime) -> None:
    if (
        scope.record.status != "blocked"
        or scope.record.block_code != "active_graph_dependency"
    ):
        raise CoordinatedPurgeError(
            "retention_not_graph_blocked",
            "retention record is not blocked by graph dependency",
        )
    problem = cleanup_scope_error(scope, current_time)
    if problem is not None:
        raise CoordinatedPurgeError(problem[0], "retention scope is not purge-ready")
    if scope.revision_file.lifecycle_status != "available":
        raise CoordinatedPurgeError(
            "revision_file_unavailable", "revision file is not available"
        )


async def _bounded_target_evidence_ids(
    db,
    scope: LockedCleanupScope,
    config: Settings,
) -> tuple[uuid.UUID, ...]:
    rows = tuple(
        (
            await db.execute(
                select(EvidenceUnit.id)
                .where(
                    EvidenceUnit.library_id == scope.record.library_id,
                    EvidenceUnit.document_id == scope.record.document_id,
                    EvidenceUnit.document_revision_id
                    == scope.record.document_revision_id,
                )
                .order_by(EvidenceUnit.id)
                .limit(config.revision_coordinated_purge_max_evidence + 1)
            )
        ).scalars().all()
    )
    if len(rows) > config.revision_coordinated_purge_max_evidence:
        raise CoordinatedPurgeError(
            "target_evidence_limit_exceeded", "target Evidence limit exceeded"
        )
    if not rows:
        raise CoordinatedPurgeError(
            "target_evidence_not_found", "target Evidence was not found"
        )
    return rows


async def _current_publications_and_items(db, library_id: uuid.UUID):
    publications = list(
        (
            await db.execute(
                select(GraphPublication)
                .where(
                    GraphPublication.library_id == library_id,
                    GraphPublication.status.in_(_CURRENT_PUBLICATION_STATUSES),
                )
                .order_by(GraphPublication.id)
            )
        ).scalars().all()
    )
    publication_ids = [row.id for row in publications]
    if not publication_ids:
        return publications, []
    items = list(
        (
            await db.execute(
                select(GraphPublicationItem)
                .where(GraphPublicationItem.publication_id.in_(publication_ids))
                .order_by(GraphPublicationItem.publication_id, GraphPublicationItem.id)
            )
        ).scalars().all()
    )
    return publications, items


async def _build_preview(
    db,
    scope: LockedCleanupScope,
    *,
    current_time: datetime,
    config: Settings,
) -> CoordinatedPurgePreview:
    _validate_scope(scope, current_time)
    target_ids = await _bounded_target_evidence_ids(db, scope, config)
    target_set = set(target_ids)
    publications, items = await _current_publications_and_items(
        db, scope.record.library_id
    )
    items_by_publication: dict[uuid.UUID, list[GraphPublicationItem]] = {}
    for item in items:
        items_by_publication.setdefault(item.publication_id, []).append(item)
    affected_publications = [
        publication
        for publication in publications
        if any(
            _support_ids(item).intersection(target_set)
            for item in items_by_publication.get(publication.id, [])
        )
    ]
    if len(affected_publications) != 1:
        raise CoordinatedPurgeError(
            "affected_publication_scope_invalid",
            "exactly one affected current publication is required",
        )
    source = affected_publications[0]
    ontology = await db.get(OntologyVersion, source.ontology_version_id)
    if (
        ontology is None
        or ontology.library_id != scope.record.library_id
        or ontology.status != ONTOLOGY_STATUS_ACTIVE
    ):
        raise CoordinatedPurgeError(
            "ontology_not_active", "affected publication ontology is not active"
        )
    candidate = await build_graph_publication_snapshot(
        db,
        scope.library,
        ontology,
        include_drafts=source.include_drafts,
        config=config,
    )
    replacement, diff, affected_keys = build_minimal_replacement_snapshot(
        source_items=items_by_publication[source.id],
        candidate=candidate,
        target_evidence_ids=target_set,
        max_affected_items=config.revision_coordinated_purge_max_affected_items,
    )
    replacement_manifest_hash = graph_publication_snapshot_manifest_hash(
        library_id=scope.record.library_id,
        ontology_version_id=source.ontology_version_id,
        source_mode=GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
        parent_publication_id=source.id,
        include_drafts=source.include_drafts,
        snapshot=replacement,
        config=config,
    )
    sample_limit = config.revision_coordinated_purge_impact_sample
    target_hash = _sha256([str(value) for value in target_ids])
    affected_hash = _sha256(list(affected_keys))
    impact = {
        **diff,
        "target_evidence_count": len(target_ids),
        "target_evidence_hash": target_hash,
        "affected_item_hash": affected_hash,
        "target_evidence_id_sample": [str(value) for value in target_ids[:sample_limit]],
        "affected_item_key_sample": list(affected_keys[:sample_limit]),
    }
    impact_encoded = _canonical(impact)
    if len(impact_encoded) > 16_384:
        raise CoordinatedPurgeError(
            "purge_impact_too_large", "coordinated purge impact is too large"
        )
    impact_hash = hashlib.sha256(impact_encoded).hexdigest()
    confirmation_hash = _sha256(
        {
            "retention_record_id": str(scope.record.id),
            "revision_file_id": str(scope.revision_file.id),
            "source_publication_id": str(source.id),
            "source_manifest_hash": source.manifest_hash,
            "replacement_manifest_hash": replacement_manifest_hash,
            "target_evidence_hash": target_hash,
            "affected_item_hash": affected_hash,
            "impact_hash": impact_hash,
        }
    )
    return CoordinatedPurgePreview(
        retention_record_id=scope.record.id,
        library_id=scope.record.library_id,
        document_id=scope.record.document_id,
        document_revision_id=scope.record.document_revision_id,
        revision_file_id=scope.revision_file.id,
        source_publication_id=source.id,
        ontology_version_id=source.ontology_version_id,
        include_drafts=source.include_drafts,
        source_manifest_hash=source.manifest_hash,
        replacement_manifest_hash=replacement_manifest_hash,
        impact_snapshot=impact,
        impact_hash=impact_hash,
        confirmation_hash=confirmation_hash,
        replacement_snapshot=replacement,
    )


async def preview_coordinated_revision_purge(
    db,
    *,
    retention_record_id: uuid.UUID,
    at: datetime | None = None,
    config: Settings = settings,
) -> CoordinatedPurgePreview:
    if not config.revision_coordinated_purge_enabled:
        raise CoordinatedPurgeError(
            "coordinated_purge_disabled", "coordinated purge is disabled"
        )
    scope = await _load_scope_unlocked(db, retention_record_id)
    if scope is None:
        raise CoordinatedPurgeError(
            "retention_not_found", "retention record was not found"
        )
    return await _build_preview(
        db,
        scope,
        current_time=cleanup_now(at),
        config=config,
    )


async def _rebuild_existing_preview(
    db,
    *,
    scope: LockedCleanupScope,
    operation: RevisionPurgeOperation,
    config: Settings,
) -> CoordinatedPurgePreview:
    if (
        operation.library_id != scope.library.id
        or operation.retention_record_id != scope.record.id
        or operation.revision_file_id != scope.revision_file.id
        or operation.document_id != scope.document.id
        or operation.document_revision_id != scope.old_revision.id
    ):
        raise CoordinatedPurgeError(
            "coordinated_purge_scope_changed",
            "coordinated purge operation scope changed",
        )
    source = await db.get(GraphPublication, operation.source_publication_id)
    replacement = await db.get(
        GraphPublication, operation.replacement_publication_id
    )
    if (
        source is None
        or replacement is None
        or source.library_id != operation.library_id
        or replacement.library_id != operation.library_id
        or source.ontology_version_id != replacement.ontology_version_id
        or source.manifest_hash != operation.source_manifest_hash
        or replacement.manifest_hash != operation.replacement_manifest_hash
        or replacement.source_mode != GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE
        or replacement.parent_publication_id != source.id
    ):
        raise CoordinatedPurgeError(
            "coordinated_purge_operation_changed",
            "coordinated purge publication identity changed",
        )
    impact = copy.deepcopy(dict(operation.impact_snapshot or {}))
    if _sha256(impact) != operation.impact_hash:
        raise CoordinatedPurgeError(
            "coordinated_purge_operation_changed",
            "coordinated purge impact identity changed",
        )
    options = dict(replacement.plan_options or {})
    if (
        options.get("retention_record_id") != str(operation.retention_record_id)
        or options.get("confirmation_hash") != operation.confirmation_hash
        or options.get("target_evidence_hash")
        != impact.get("target_evidence_hash")
    ):
        raise CoordinatedPurgeError(
            "coordinated_purge_operation_changed",
            "coordinated purge plan identity changed",
        )
    items = tuple(
        _clone_item(item)
        for item in (
            (
                await db.execute(
                    select(GraphPublicationItem)
                    .where(
                        GraphPublicationItem.publication_id == replacement.id
                    )
                    .order_by(GraphPublicationItem.item_kind, GraphPublicationItem.id)
                )
            )
            .scalars()
            .all()
        )
    )
    snapshot = GraphPublicationSnapshot(
        items=items,
        blocked_counts=copy.deepcopy(dict(replacement.blocked_counts or {})),
        policy_snapshot=copy.deepcopy(dict(replacement.policy_snapshot or {})),
        policy_snapshot_hash=_sha256(dict(replacement.policy_snapshot or {})),
    )
    if (
        replacement.entity_count
        != sum(item.item_kind == "entity" for item in items)
        or replacement.relation_count
        != sum(item.item_kind == "relation" for item in items)
    ):
        raise CoordinatedPurgeError(
            "coordinated_purge_operation_changed",
            "coordinated purge replacement counts changed",
        )
    rebuilt_manifest = graph_publication_snapshot_manifest_hash(
        library_id=operation.library_id,
        ontology_version_id=replacement.ontology_version_id,
        source_mode=GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
        parent_publication_id=source.id,
        include_drafts=replacement.include_drafts,
        snapshot=snapshot,
        config=config,
    )
    if rebuilt_manifest != operation.replacement_manifest_hash:
        raise CoordinatedPurgeError(
            "coordinated_purge_operation_changed",
            "coordinated purge replacement snapshot changed",
        )
    return CoordinatedPurgePreview(
        retention_record_id=operation.retention_record_id,
        library_id=operation.library_id,
        document_id=operation.document_id,
        document_revision_id=operation.document_revision_id,
        revision_file_id=operation.revision_file_id,
        source_publication_id=operation.source_publication_id,
        ontology_version_id=replacement.ontology_version_id,
        include_drafts=replacement.include_drafts,
        source_manifest_hash=operation.source_manifest_hash,
        replacement_manifest_hash=operation.replacement_manifest_hash,
        impact_snapshot=impact,
        impact_hash=operation.impact_hash,
        confirmation_hash=operation.confirmation_hash,
        replacement_snapshot=snapshot,
    )


async def plan_coordinated_revision_purge(
    db,
    *,
    retention_record_id: uuid.UUID,
    expected_confirmation_hash: str,
    idempotency_key: str,
    requested_by_user_id: uuid.UUID | None = None,
    at: datetime | None = None,
    config: Settings = settings,
) -> CoordinatedPurgePlanResult:
    if not config.revision_coordinated_purge_enabled:
        raise CoordinatedPurgeError(
            "coordinated_purge_disabled", "coordinated purge is disabled"
        )
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
        idempotency_key
    ):
        raise CoordinatedPurgeError(
            "idempotency_key_invalid", "idempotency key is invalid"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", expected_confirmation_hash or ""):
        raise CoordinatedPurgeError(
            "confirmation_hash_invalid", "confirmation hash is invalid"
        )
    scope = await lock_cleanup_scope(db, retention_record_id)
    if scope is None:
        raise CoordinatedPurgeError(
            "retention_not_found", "retention record was not found"
        )
    existing_rows = list(
        (
            await db.execute(
                select(RevisionPurgeOperation).where(
                    or_(
                        RevisionPurgeOperation.retention_record_id
                        == retention_record_id,
                        and_(
                            RevisionPurgeOperation.library_id == scope.library.id,
                            RevisionPurgeOperation.idempotency_key == idempotency_key,
                        ),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    existing = next(
        (
            row
            for row in existing_rows
            if row.retention_record_id == retention_record_id
        ),
        None,
    )
    if existing is not None:
        if existing.idempotency_key != idempotency_key:
            raise CoordinatedPurgeError(
                "idempotency_conflict", "coordinated purge operation conflicts"
            )
        if existing.confirmation_hash != expected_confirmation_hash:
            raise CoordinatedPurgeError(
                "confirmation_hash_changed", "coordinated purge preview changed"
            )
        preview = await _rebuild_existing_preview(
            db,
            scope=scope,
            operation=existing,
            config=config,
        )
        return CoordinatedPurgePlanResult(existing, preview, reused=True)
    if existing_rows:
        raise CoordinatedPurgeError(
            "idempotency_conflict", "coordinated purge operation conflicts"
        )
    preview = await _build_preview(
        db,
        scope,
        current_time=cleanup_now(at),
        config=config,
    )
    if preview.confirmation_hash != expected_confirmation_hash:
        raise CoordinatedPurgeError(
            "confirmation_hash_changed", "coordinated purge preview changed"
        )
    try:
        publication_result = await plan_explicit_graph_publication_snapshot(
            db,
            scope.library,
            ontology_version_id=preview.ontology_version_id,
            snapshot=preview.replacement_snapshot,
            source_mode=GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
            include_drafts=preview.include_drafts,
            idempotency_key=f"purge:{idempotency_key}",
            expected_parent_publication_id=preview.source_publication_id,
            requested_by_user_id=requested_by_user_id,
            plan_options={
                "retention_record_id": str(retention_record_id),
                "confirmation_hash": preview.confirmation_hash,
                "target_evidence_hash": preview.impact_snapshot[
                    "target_evidence_hash"
                ],
            },
            config=config,
        )
    except GraphPublicationPlanError as exc:
        raise CoordinatedPurgeError(exc.code, str(exc)) from exc
    operation = RevisionPurgeOperation(
        id=uuid.uuid4(),
        library_id=preview.library_id,
        retention_record_id=preview.retention_record_id,
        revision_file_id=preview.revision_file_id,
        document_id=preview.document_id,
        document_revision_id=preview.document_revision_id,
        source_publication_id=preview.source_publication_id,
        replacement_publication_id=publication_result.publication.id,
        status="planned",
        idempotency_key=idempotency_key,
        confirmation_hash=preview.confirmation_hash,
        impact_snapshot=copy.deepcopy(preview.impact_snapshot),
        impact_hash=preview.impact_hash,
        source_manifest_hash=preview.source_manifest_hash,
        replacement_manifest_hash=preview.replacement_manifest_hash,
        requested_by_user_id=requested_by_user_id,
        attempt_count=0,
        available_at=cleanup_now(at),
    )
    db.add(operation)
    await db.flush()
    await audit_log.record(
        db,
        requested_by_user_id,
        "revision_purge.planned",
        {
            "operation_id": str(operation.id),
            "retention_record_id": str(operation.retention_record_id),
            "library_id": str(operation.library_id),
            "source_publication_id": str(operation.source_publication_id),
            "replacement_publication_id": str(operation.replacement_publication_id),
            "confirmation_hash": operation.confirmation_hash,
            "impact_hash": operation.impact_hash,
        },
    )
    return CoordinatedPurgePlanResult(operation, preview)


@dataclass(frozen=True, slots=True)
class _LockedOperationScope:
    operation: RevisionPurgeOperation
    retention_scope: LockedCleanupScope
    source_publication: GraphPublication
    replacement_publication: GraphPublication


async def _lock_operation_scope(
    db,
    operation_id: uuid.UUID,
) -> _LockedOperationScope | None:
    initial = (
        await db.execute(
            select(
                RevisionPurgeOperation.id,
                RevisionPurgeOperation.library_id,
            ).where(RevisionPurgeOperation.id == operation_id)
        )
    ).first()
    if initial is None:
        return None
    library = (
        await db.execute(
            select(Library)
            .where(Library.id == initial.library_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if library is None:
        return None
    operation = (
        await db.execute(
            select(RevisionPurgeOperation)
            .where(RevisionPurgeOperation.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if operation is None or operation.library_id != library.id:
        return None
    retention_scope = await lock_cleanup_scope(db, operation.retention_record_id)
    if retention_scope is None or retention_scope.library.id != library.id:
        return None
    publications = list(
        (
            await db.execute(
                select(GraphPublication)
                .where(
                    GraphPublication.id.in_(
                        (
                            operation.source_publication_id,
                            operation.replacement_publication_id,
                        )
                    )
                )
                .order_by(GraphPublication.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    publications_by_id = {row.id: row for row in publications}
    source = publications_by_id.get(operation.source_publication_id)
    replacement = publications_by_id.get(operation.replacement_publication_id)
    if source is None or replacement is None:
        return None
    return _LockedOperationScope(
        operation=operation,
        retention_scope=retention_scope,
        source_publication=source,
        replacement_publication=replacement,
    )


def _clear_operation_claim(operation: RevisionPurgeOperation) -> None:
    operation.worker_id = None
    operation.claim_token = None
    operation.claimed_at = None
    operation.lease_expires_at = None


def _operation_scope_error(scope: _LockedOperationScope) -> str | None:
    operation = scope.operation
    retention = scope.retention_scope.record
    revision_file = scope.retention_scope.revision_file
    replacement = scope.replacement_publication
    if (
        operation.library_id != retention.library_id
        or operation.retention_record_id != retention.id
        or operation.revision_file_id != revision_file.id
        or operation.document_id != retention.document_id
        or operation.document_revision_id != retention.document_revision_id
    ):
        return "coordinated_purge_scope_changed"
    if operation.source_manifest_hash != scope.source_publication.manifest_hash:
        return "source_publication_changed"
    if operation.replacement_manifest_hash != replacement.manifest_hash:
        return "replacement_publication_changed"
    if replacement.source_mode != GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE:
        return "replacement_publication_invalid"
    if replacement.parent_publication_id != scope.source_publication.id:
        return "replacement_parent_changed"
    if replacement.status not in {"planned", "activating", "active"}:
        return "replacement_publication_failed"
    if revision_file.lifecycle_status != "available":
        return "revision_file_unavailable"
    return None


def _claim_from_scope(scope: _LockedOperationScope) -> CoordinatedPurgeClaim:
    operation = scope.operation
    if operation.claim_token is None:
        raise CoordinatedPurgeError(
            "coordinated_purge_claim_invalid", "operation claim token is unavailable"
        )
    return CoordinatedPurgeClaim(
        operation_id=operation.id,
        library_id=operation.library_id,
        retention_record_id=operation.retention_record_id,
        document_revision_id=operation.document_revision_id,
        source_publication_id=operation.source_publication_id,
        replacement_publication_id=operation.replacement_publication_id,
        replacement_manifest_hash=operation.replacement_manifest_hash,
        confirmation_hash=operation.confirmation_hash,
        claim_token=operation.claim_token,
        attempt_count=operation.attempt_count,
    )


async def claim_coordinated_purge_operation(
    db,
    *,
    operation_id: uuid.UUID,
    worker_id: str,
    at: datetime | None = None,
    config: Settings = settings,
) -> CoordinatedPurgeClaim | None:
    if not config.revision_coordinated_purge_enabled:
        return None
    worker_id = require_cleanup_worker_id(worker_id)
    current_time = cleanup_now(at)
    scope = await _lock_operation_scope(db, operation_id)
    if scope is None:
        return None
    operation = scope.operation
    claimable = (
        operation.status in {"planned", "failed"}
        and operation.available_at is not None
        and operation.available_at <= current_time
    ) or (
        operation.status == "processing"
        and operation.lease_expires_at is not None
        and operation.lease_expires_at <= current_time
    )
    if (
        not claimable
        or operation.attempt_count
        >= config.revision_coordinated_purge_max_attempts
    ):
        return None
    problem = _operation_scope_error(scope)
    if problem is not None:
        operation.attempt_count += 1
        operation.status = "failed"
        operation.available_at = current_time + timedelta(seconds=60)
        operation.last_error_code = problem
        operation.finished_at = None
        _clear_operation_claim(operation)
        return None
    operation.status = "processing"
    operation.attempt_count += 1
    operation.available_at = None
    operation.worker_id = worker_id
    operation.claim_token = uuid.uuid4()
    operation.claimed_at = current_time
    operation.lease_expires_at = current_time + timedelta(
        seconds=config.revision_coordinated_purge_lease_seconds
    )
    operation.finished_at = None
    operation.last_error_code = None
    return _claim_from_scope(scope)


def _require_live_operation_claim(
    scope: _LockedOperationScope,
    claim: CoordinatedPurgeClaim,
) -> None:
    operation = scope.operation
    if (
        operation.status != "processing"
        or operation.id != claim.operation_id
        or operation.library_id != claim.library_id
        or operation.retention_record_id != claim.retention_record_id
        or operation.document_revision_id != claim.document_revision_id
        or operation.source_publication_id != claim.source_publication_id
        or operation.replacement_publication_id != claim.replacement_publication_id
        or operation.replacement_manifest_hash != claim.replacement_manifest_hash
        or operation.confirmation_hash != claim.confirmation_hash
        or operation.claim_token != claim.claim_token
    ):
        raise CoordinatedPurgeError(
            "coordinated_purge_claim_stale",
            "coordinated purge claim is no longer authoritative",
        )


async def fail_coordinated_purge_operation(
    db,
    *,
    claim: CoordinatedPurgeClaim,
    error_code: str,
    at: datetime | None = None,
) -> CoordinatedPurgeExecutionResult:
    current_time = cleanup_now(at)
    code = error_code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) else "purge_failed"
    scope = await _lock_operation_scope(db, claim.operation_id)
    if scope is None:
        raise CoordinatedPurgeError(
            "coordinated_purge_claim_stale", "operation scope is unavailable"
        )
    _require_live_operation_claim(scope, claim)
    operation = scope.operation
    operation.status = "failed"
    operation.available_at = current_time + timedelta(
        seconds=min(3_600, 2 ** min(operation.attempt_count, 10))
    )
    operation.finished_at = None
    operation.last_error_code = code
    _clear_operation_claim(operation)
    await audit_log.record(
        db,
        operation.requested_by_user_id,
        "revision_purge.failed",
        {
            "operation_id": str(operation.id),
            "library_id": str(operation.library_id),
            "attempt_count": operation.attempt_count,
            "error_code": code,
        },
    )
    return CoordinatedPurgeExecutionResult(operation.id, "failed", code)


async def finalize_coordinated_purge_activation(
    db,
    *,
    claim: CoordinatedPurgeClaim,
    at: datetime | None = None,
    config: Settings = settings,
) -> CoordinatedPurgeExecutionResult:
    scope = await _lock_operation_scope(db, claim.operation_id)
    if scope is None:
        raise CoordinatedPurgeError(
            "coordinated_purge_claim_stale", "operation scope is unavailable"
        )
    _require_live_operation_claim(scope, claim)
    operation = scope.operation
    if (
        scope.replacement_publication.status != "active"
        or scope.replacement_publication.manifest_hash
        != operation.replacement_manifest_hash
        or scope.source_publication.status != "superseded"
        or scope.source_publication.superseded_by_publication_id
        != scope.replacement_publication.id
    ):
        raise CoordinatedPurgeError(
            "replacement_publication_not_active",
            "replacement publication is not active",
        )
    from app.services.graph_evidence import (
        mark_document_revision_graph_evidence_stale,
    )

    await mark_document_revision_graph_evidence_stale(
        db,
        scope.retention_scope.library,
        document_revision_id=operation.document_revision_id,
    )
    retention = await refresh_revision_retention(
        db,
        record_id=operation.retention_record_id,
        at=cleanup_now(at),
        config=config,
    )
    if retention.status not in {"eligible", "scheduled"}:
        raise CoordinatedPurgeError(
            "post_activation_dependency_remaining",
            "graph dependency remains after replacement activation",
        )
    operation.status = "cleanup_pending"
    operation.available_at = None
    operation.finished_at = None
    operation.last_error_code = None
    _clear_operation_claim(operation)
    await audit_log.record(
        db,
        operation.requested_by_user_id,
        "revision_purge.cleanup_pending",
        {
            "operation_id": str(operation.id),
            "retention_record_id": str(operation.retention_record_id),
            "library_id": str(operation.library_id),
            "replacement_publication_id": str(operation.replacement_publication_id),
            "retention_status": retention.status,
        },
    )
    return CoordinatedPurgeExecutionResult(
        operation.id, "cleanup_pending", retention.status
    )


async def coordinated_purge_candidate_ids(
    db,
    *,
    at: datetime,
    limit: int,
    config: Settings,
) -> tuple[uuid.UUID, ...]:
    from sqlalchemy import and_, or_

    due = and_(
        RevisionPurgeOperation.status.in_(("planned", "failed")),
        RevisionPurgeOperation.available_at <= at,
    )
    expired = and_(
        RevisionPurgeOperation.status == "processing",
        RevisionPurgeOperation.lease_expires_at <= at,
    )
    return tuple(
        (
            await db.execute(
                select(RevisionPurgeOperation.id)
                .where(
                    or_(due, expired),
                    RevisionPurgeOperation.attempt_count
                    < config.revision_coordinated_purge_max_attempts,
                )
                .order_by(
                    RevisionPurgeOperation.library_id,
                    RevisionPurgeOperation.available_at,
                    RevisionPurgeOperation.id,
                )
                .limit(limit)
            )
        ).scalars().all()
    )


async def reconcile_coordinated_purge_operations(
    db,
    *,
    at: datetime | None = None,
    batch_limit: int | None = None,
    config: Settings = settings,
) -> tuple[uuid.UUID, ...]:
    if not config.revision_coordinated_purge_enabled:
        return ()
    limit = batch_limit or config.revision_coordinated_purge_batch_size
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise CoordinatedPurgeError(
            "coordinated_purge_batch_invalid", "batch limit is invalid"
        )
    candidate_ids = tuple(
        (
            await db.execute(
                select(RevisionPurgeOperation.id)
                .where(RevisionPurgeOperation.status == "cleanup_pending")
                .order_by(RevisionPurgeOperation.library_id, RevisionPurgeOperation.id)
                .limit(limit)
            )
        ).scalars().all()
    )
    completed: list[uuid.UUID] = []
    current_time = cleanup_now(at)
    for operation_id in candidate_ids:
        scope = await _lock_operation_scope(db, operation_id)
        if scope is None or scope.operation.status != "cleanup_pending":
            continue
        if scope.retention_scope.record.status != "cleaned":
            continue
        scope.operation.status = "completed"
        scope.operation.finished_at = current_time
        scope.operation.last_error_code = None
        completed.append(scope.operation.id)
        await audit_log.record(
            db,
            scope.operation.requested_by_user_id,
            "revision_purge.completed",
            {
                "operation_id": str(scope.operation.id),
                "retention_record_id": str(scope.operation.retention_record_id),
                "library_id": str(scope.operation.library_id),
            },
        )
    return tuple(completed)
