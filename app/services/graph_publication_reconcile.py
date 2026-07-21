from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.entity import Entity
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, OntologyVersion
from app.services import audit_log
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    build_graph_publication_snapshot,
)
from app.services.graph_publication_read import list_current_graph_publication


log = logging.getLogger(__name__)
CURRENT_STATUSES = ("active", "degraded")
MAX_RECONCILIATION_BATCH = 100


@dataclass(frozen=True, slots=True)
class PublicationReconciliationResult:
    publication_id: uuid.UUID
    status: str
    checked_item_count: int
    newly_degraded_item_count: int
    degraded_item_count: int
    transitioned_to_degraded: bool
    reason_counts: dict[str, int]
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class PublicationReconciliationBatch:
    results: tuple[PublicationReconciliationResult, ...]
    next_cursor: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class CurrentPublicationHealth:
    publication_id: uuid.UUID | None
    current_publication_status: str | None
    degraded_item_count: int
    blocked_pending_review_count: int
    last_reconciled_at: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item_key(item: GraphPublicationItem) -> tuple[str, uuid.UUID] | None:
    if item.item_kind == "entity" and item.entity_id is not None:
        return item.item_kind, item.entity_id
    if item.item_kind == "relation" and item.relation_id is not None:
        return item.item_kind, item.relation_id
    return None


def _add_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1


def _frozen_policy_config(publication: GraphPublication, base: Settings) -> Settings:
    policy = dict(publication.policy_snapshot or {})
    try:
        require_entity_evidence = policy["require_entity_evidence"]
        entity_floor = float(policy["extracted_entity_min_confidence"])
        relation_floor = float(policy["extracted_relation_min_confidence"])
        max_items = int(policy["max_items_per_run"])
        support_types = policy["relation_support_types"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid publication policy snapshot") from exc
    if (
        not isinstance(require_entity_evidence, bool)
        or isinstance(policy.get("max_items_per_run"), bool)
        or max_items <= 0
        or not math.isfinite(entity_floor)
        or not math.isfinite(relation_floor)
        or not 0 <= entity_floor <= 1
        or not 0 <= relation_floor <= 1
        or support_types != ["supports"]
        or policy.get("manifest_version") != publication.manifest_version
        or policy.get("policy_version") != publication.policy_version
    ):
        raise ValueError("invalid publication policy snapshot")
    return base.model_copy(
        update={
            "graph_publication_require_entity_evidence": require_entity_evidence,
            "graph_publication_extracted_entity_min_confidence": entity_floor,
            "graph_publication_extracted_relation_min_confidence": relation_floor,
            "graph_publication_max_items_per_run": max_items,
            "graph_publication_manifest_version": publication.manifest_version,
            "graph_publication_policy_version": publication.policy_version,
        }
    )


async def _lock_scope(
    db: AsyncSession,
    publication_id: uuid.UUID,
) -> tuple[Library, GraphPublication] | None:
    initial = await db.get(GraphPublication, publication_id)
    if initial is None:
        return None
    library_result = await db.execute(
        select(Library)
        .where(Library.id == initial.library_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    library = library_result.scalars().first()
    if library is None:
        return None
    publication_result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.id == publication_id,
            GraphPublication.library_id == library.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    publication = publication_result.scalars().first()
    if publication is None:
        return None
    return library, publication


async def _locked_items(
    db: AsyncSession,
    publication: GraphPublication,
) -> list[GraphPublicationItem]:
    result = await db.execute(
        select(GraphPublicationItem)
        .where(
            GraphPublicationItem.publication_id == publication.id,
            GraphPublicationItem.library_id == publication.library_id,
            GraphPublicationItem.ontology_version_id == publication.ontology_version_id,
        )
        .order_by(GraphPublicationItem.item_kind, GraphPublicationItem.item_hash, GraphPublicationItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _formal_state(
    db: AsyncSession,
    publication: GraphPublication,
    items: list[GraphPublicationItem],
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, tuple[str, uuid.UUID, uuid.UUID]]]:
    entity_ids = {item.entity_id for item in items if item.entity_id is not None}
    relation_ids = {item.relation_id for item in items if item.relation_id is not None}
    relation_state: dict[uuid.UUID, tuple[str, uuid.UUID, uuid.UUID]] = {}
    if relation_ids:
        rows = (
            await db.execute(
                select(
                    KnowledgeRelation.id,
                    KnowledgeRelation.status,
                    KnowledgeRelation.source_entity_id,
                    KnowledgeRelation.target_entity_id,
                ).where(
                    KnowledgeRelation.library_id == publication.library_id,
                    KnowledgeRelation.ontology_version_id == publication.ontology_version_id,
                    KnowledgeRelation.id.in_(relation_ids),
                )
            )
        ).all()
        relation_state = {
            relation_id: (status, source_entity_id, target_entity_id)
            for relation_id, status, source_entity_id, target_entity_id in rows
        }
        for _, source_entity_id, target_entity_id in relation_state.values():
            entity_ids.add(source_entity_id)
            entity_ids.add(target_entity_id)
    entity_state: dict[uuid.UUID, str] = {}
    if entity_ids:
        rows = (
            await db.execute(
                select(Entity.id, Entity.status).where(
                    Entity.library_id == publication.library_id,
                    Entity.ontology_version_id == publication.ontology_version_id,
                    Entity.id.in_(entity_ids),
                )
            )
        ).all()
        entity_state = dict(rows)
    return entity_state, relation_state


async def reconcile_current_publication(
    db: AsyncSession,
    publication_id: uuid.UUID,
    *,
    config: Settings = settings,
) -> PublicationReconciliationResult:
    scope = await _lock_scope(db, publication_id)
    if scope is None:
        return PublicationReconciliationResult(
            publication_id=publication_id,
            status="missing",
            checked_item_count=0,
            newly_degraded_item_count=0,
            degraded_item_count=0,
            transitioned_to_degraded=False,
            reason_counts={},
            skipped=True,
        )
    library, publication = scope
    if publication.status not in CURRENT_STATUSES:
        return PublicationReconciliationResult(
            publication_id=publication.id,
            status=publication.status,
            checked_item_count=0,
            newly_degraded_item_count=0,
            degraded_item_count=0,
            transitioned_to_degraded=False,
            reason_counts={},
            skipped=True,
        )

    items = await _locked_items(db, publication)
    entity_state, relation_state = await _formal_state(db, publication, items)
    reasons: dict[str, int] = {}
    candidate_by_key: dict[tuple[str, uuid.UUID], GraphPublicationItem] = {}
    force_reason: str | None = None
    ontology = await db.get(OntologyVersion, publication.ontology_version_id)
    if (
        ontology is None
        or ontology.library_id != publication.library_id
        or ontology.status != ONTOLOGY_STATUS_ACTIVE
    ):
        force_reason = "ontology_not_active"
    else:
        try:
            frozen_config = _frozen_policy_config(publication, config)
            candidate = await build_graph_publication_snapshot(
                db,
                library,
                ontology,
                include_drafts=publication.include_drafts,
                enforce_item_limit=False,
                config=frozen_config,
            )
            candidate_by_key = {
                key: item for item in candidate.items if (key := _item_key(item)) is not None
            }
        except (GraphPublicationPlanError, ValueError):
            force_reason = "recheck_failed"

    newly_degraded = 0
    for item in items:
        reason = force_reason
        key = _item_key(item)
        if reason is None and key is None:
            reason = "item_invalid"
        elif reason is None and item.item_kind == "entity":
            if item.entity_id not in entity_state:
                reason = "formal_row_missing"
            elif entity_state[item.entity_id] != "active":
                reason = "formal_status_not_active"
        elif reason is None and item.item_kind == "relation":
            formal = relation_state.get(item.relation_id)
            if formal is None:
                reason = "formal_row_missing"
            elif formal[0] != "active":
                reason = "formal_status_not_active"
            elif entity_state.get(formal[1]) != "active" or entity_state.get(formal[2]) != "active":
                reason = "relation_endpoint_not_active"
        if reason is None:
            candidate_item = candidate_by_key.get(key)
            if candidate_item is None:
                reason = "eligibility_changed"
            elif candidate_item.item_hash != item.item_hash:
                reason = "item_hash_changed"
        if reason is not None:
            _add_reason(reasons, reason)
            if item.status != "degraded":
                item.status = "degraded"
                newly_degraded += 1

    degraded_count = sum(item.status == "degraded" for item in items)
    transitioned = publication.status == "active" and degraded_count > 0
    if transitioned:
        publication.status = "degraded"
    publication.last_reconciled_at = _now()
    diagnostics = dict(publication.blocked_diagnostics or {})
    diagnostics["reconciliation"] = {
        "degraded_item_count": degraded_count,
        "reason_counts": dict(sorted(reasons.items())),
    }
    publication.blocked_diagnostics = diagnostics
    if transitioned:
        await audit_log.record(
            db,
            None,
            "graph_publication.degraded",
            {
                "publication_id": str(publication.id),
                "library_id": str(publication.library_id),
                "ontology_version_id": str(publication.ontology_version_id),
                "degraded_item_count": degraded_count,
                "reason_counts": dict(sorted(reasons.items())),
            },
        )
        log.warning(
            "graph publication degraded publication_id=%s degraded_item_count=%s reason_counts=%s",
            publication.id,
            degraded_count,
            dict(sorted(reasons.items())),
        )
    await db.flush()
    return PublicationReconciliationResult(
        publication_id=publication.id,
        status=publication.status,
        checked_item_count=len(items),
        newly_degraded_item_count=newly_degraded,
        degraded_item_count=degraded_count,
        transitioned_to_degraded=transitioned,
        reason_counts=dict(sorted(reasons.items())),
    )


async def reconcile_library_current_publications(
    db: AsyncSession,
    library: Library,
    *,
    limit: int = MAX_RECONCILIATION_BATCH,
    config: Settings = settings,
) -> tuple[PublicationReconciliationResult, ...]:
    if limit < 1 or limit > MAX_RECONCILIATION_BATCH:
        raise ValueError("reconciliation limit must be between 1 and 100")
    publication_ids = (
        await db.execute(
            select(GraphPublication.id)
            .where(
                GraphPublication.library_id == library.id,
                GraphPublication.status.in_(CURRENT_STATUSES),
            )
            .order_by(GraphPublication.id)
            .limit(limit)
        )
    ).scalars().all()
    return tuple(
        [
            await reconcile_current_publication(db, publication_id, config=config)
            for publication_id in publication_ids
        ]
    )


async def reconcile_graph_publications_batch(
    db: AsyncSession,
    *,
    after_publication_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    limit: int = 50,
    config: Settings = settings,
) -> PublicationReconciliationBatch:
    if limit < 1 or limit > MAX_RECONCILIATION_BATCH:
        raise ValueError("reconciliation limit must be between 1 and 100")
    filters = [GraphPublication.status.in_(CURRENT_STATUSES)]
    if after_publication_id is not None:
        filters.append(GraphPublication.id > after_publication_id)
    if library_id is not None:
        filters.append(GraphPublication.library_id == library_id)
    publication_ids = list(
        (
            await db.execute(
                select(GraphPublication.id)
                .where(*filters)
                .order_by(GraphPublication.id)
                .limit(limit + 1)
            )
        ).scalars().all()
    )
    has_more = len(publication_ids) > limit
    to_process = publication_ids[:limit]
    results = tuple(
        [
            await reconcile_current_publication(db, publication_id, config=config)
            for publication_id in to_process
        ]
    )
    next_cursor = to_process[-1] if has_more and to_process else None
    return PublicationReconciliationBatch(results=results, next_cursor=next_cursor)


async def get_current_publication_health(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None = None,
) -> CurrentPublicationHealth:
    publication = await list_current_graph_publication(db, library, ontology_version_id)
    if publication is None:
        return CurrentPublicationHealth(
            publication_id=None,
            current_publication_status=None,
            degraded_item_count=0,
            blocked_pending_review_count=0,
            last_reconciled_at=None,
        )
    degraded_count = (
        await db.execute(
            select(func.count())
            .select_from(GraphPublicationItem)
            .where(
                GraphPublicationItem.publication_id == publication.id,
                GraphPublicationItem.status == "degraded",
            )
        )
    ).scalar_one()
    blocked_pending = sum(
        int(value)
        for key, value in dict(publication.blocked_counts or {}).items()
        if "pending_review" in key or key == "relation_review_blocked"
    )
    return CurrentPublicationHealth(
        publication_id=publication.id,
        current_publication_status=publication.status,
        degraded_item_count=degraded_count,
        blocked_pending_review_count=blocked_pending,
        last_reconciled_at=publication.last_reconciled_at,
    )
