from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.external_graph_sync import (
    GraphExternalFactMapping,
    GraphExternalSyncConflict,
    GraphExternalSyncOperation,
    GraphSyncSourcePolicy,
)
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_type import RelationType
from app.models.sync_source import SyncSource
from app.schemas.external_graph_sync import (
    ExternalEntitySyncItem,
    ExternalGraphConflictRead,
    ExternalGraphConflictDecision,
    ExternalGraphMappingRead,
    ExternalGraphSyncBatchRequest,
    ExternalGraphSyncBatchResponse,
    ExternalGraphSyncItemResult,
    ExternalRelationSyncItem,
    GraphSyncPolicyRead,
    GraphSyncPolicyWrite,
)
from app.services.sync_sources import require_active_sync_source


class ExternalGraphSyncError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _source_and_policy(
    db: AsyncSession,
    library: Library,
    source_key: str,
    *,
    for_update: bool = False,
) -> tuple[SyncSource, GraphSyncSourcePolicy]:
    source = await require_active_sync_source(db, library, source_key)
    stmt = select(GraphSyncSourcePolicy).where(
        GraphSyncSourcePolicy.library_id == library.id,
        GraphSyncSourcePolicy.sync_source_id == source.id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    policy = (await db.execute(stmt)).scalars().one_or_none()
    if policy is None:
        raise ExternalGraphSyncError("graph_sync_policy_not_found")
    if policy.status != "active":
        raise ExternalGraphSyncError("graph_sync_source_disabled")
    return source, policy


async def put_source_policy(
    db: AsyncSession,
    library: Library,
    source_key: str,
    body: GraphSyncPolicyWrite,
) -> GraphSyncSourcePolicy:
    source = await require_active_sync_source(db, library, source_key)
    policy = (
        await db.execute(
            select(GraphSyncSourcePolicy)
            .where(GraphSyncSourcePolicy.sync_source_id == source.id)
            .with_for_update()
        )
    ).scalars().one_or_none()
    if policy is None:
        policy = GraphSyncSourcePolicy(
            library_id=library.id,
            sync_source_id=source.id,
            authority_rank=body.authority_rank,
            status=body.status,
            stale_after_seconds=body.stale_after_seconds,
        )
        db.add(policy)
    else:
        policy.authority_rank = body.authority_rank
        policy.status = body.status
        policy.stale_after_seconds = body.stale_after_seconds
    await db.flush()
    return policy


async def get_source_policy(
    db: AsyncSession,
    library: Library,
    source_key: str,
) -> tuple[SyncSource, GraphSyncSourcePolicy]:
    return await _source_and_policy(db, library, source_key)


def policy_read(source: SyncSource, policy: GraphSyncSourcePolicy) -> GraphSyncPolicyRead:
    return GraphSyncPolicyRead(
        id=policy.id,
        library_id=policy.library_id,
        sync_source_id=policy.sync_source_id,
        source_key=source.source_key,
        authority_rank=policy.authority_rank,
        status=policy.status,
        stale_after_seconds=policy.stale_after_seconds,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


async def _mapping(
    db: AsyncSession,
    library_id: uuid.UUID,
    source_id: uuid.UUID,
    item: ExternalEntitySyncItem | ExternalRelationSyncItem,
    *,
    for_update: bool = False,
) -> GraphExternalFactMapping | None:
    stmt = select(GraphExternalFactMapping).where(
        GraphExternalFactMapping.library_id == library_id,
        GraphExternalFactMapping.sync_source_id == source_id,
        GraphExternalFactMapping.fact_kind == item.fact_kind,
        GraphExternalFactMapping.external_type == item.external_type,
        GraphExternalFactMapping.external_id == item.external_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().one_or_none()


async def _published(
    db: AsyncSession,
    mapping: GraphExternalFactMapping,
) -> bool:
    target = (
        GraphPublicationItem.entity_id == mapping.entity_id
        if mapping.fact_kind == "entity"
        else GraphPublicationItem.relation_id == mapping.relation_id
    )
    row = await db.execute(
        select(GraphPublicationItem.id)
        .where(
            GraphPublicationItem.library_id == mapping.library_id,
            GraphPublicationItem.item_kind == mapping.fact_kind,
            GraphPublicationItem.status.in_(("planned", "active")),
            target,
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _competing_entity_rank(
    db: AsyncSession,
    mapping: GraphExternalFactMapping,
) -> int | None:
    return (
        await db.execute(
            select(GraphSyncSourcePolicy.authority_rank)
            .join(
                GraphExternalFactMapping,
                GraphExternalFactMapping.sync_source_id
                == GraphSyncSourcePolicy.sync_source_id,
            )
            .where(
                GraphExternalFactMapping.library_id == mapping.library_id,
                GraphExternalFactMapping.entity_id == mapping.entity_id,
                GraphExternalFactMapping.lifecycle == "active",
                GraphExternalFactMapping.sync_source_id != mapping.sync_source_id,
                GraphSyncSourcePolicy.status == "active",
            )
            .order_by(GraphSyncSourcePolicy.authority_rank)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _evidence_id(
    db: AsyncSession,
    library: Library,
    evidence_id: uuid.UUID | None,
) -> uuid.UUID | None:
    if evidence_id is None:
        return None
    found = (
        await db.execute(
            select(EvidenceUnit.id).where(
                EvidenceUnit.id == evidence_id,
                EvidenceUnit.library_id == library.id,
                EvidenceUnit.status == "active",
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise ExternalGraphSyncError("graph_sync_evidence_not_found")
    return found


async def _operation(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    body: ExternalGraphSyncBatchRequest,
    request_hash: str,
    actor_id: uuid.UUID | None,
) -> tuple[GraphExternalSyncOperation, bool]:
    identity = GraphExternalSyncOperation.idempotency_key == body.idempotency_key
    if body.source_event_id is not None:
        identity = or_(
            identity,
            and_(
                GraphExternalSyncOperation.source_event_id == body.source_event_id,
                GraphExternalSyncOperation.source_event_id.is_not(None),
            ),
        )
    existing = (
        await db.execute(
            select(GraphExternalSyncOperation)
            .where(
                GraphExternalSyncOperation.library_id == library.id,
                GraphExternalSyncOperation.sync_source_id == source.id,
                identity,
            )
            .with_for_update()
        )
    ).scalars().one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ExternalGraphSyncError("graph_sync_idempotency_conflict")
        if existing.status == "processing":
            raise ExternalGraphSyncError("graph_sync_operation_in_progress")
        return existing, True
    row = GraphExternalSyncOperation(
        library_id=library.id,
        sync_source_id=source.id,
        idempotency_key=body.idempotency_key,
        request_hash=request_hash,
        source_event_id=body.source_event_id,
        snapshot_id=body.snapshot_id,
        status="processing",
        item_count=len(body.items),
        requested_by_user_id=actor_id,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        winner = (
            await db.execute(
                select(GraphExternalSyncOperation)
                .where(
                    GraphExternalSyncOperation.library_id == library.id,
                    GraphExternalSyncOperation.sync_source_id == source.id,
                    identity,
                )
                .with_for_update()
            )
        ).scalars().one()
        if winner.request_hash != request_hash:
            raise ExternalGraphSyncError("graph_sync_idempotency_conflict") from None
        if winner.status == "processing":
            raise ExternalGraphSyncError("graph_sync_operation_in_progress") from None
        return winner, True
    return row, False


async def _conflict(
    db: AsyncSession,
    *,
    mapping: GraphExternalFactMapping,
    operation: GraphExternalSyncOperation,
    reason: str,
    incoming_hash: str,
    incoming_rank: int,
    current_rank: int | None,
    proposal: dict[str, Any],
) -> GraphExternalSyncConflict:
    existing = (
        await db.execute(
            select(GraphExternalSyncConflict).where(
                GraphExternalSyncConflict.mapping_id == mapping.id,
                GraphExternalSyncConflict.reason_code == reason,
                GraphExternalSyncConflict.incoming_hash == incoming_hash,
                GraphExternalSyncConflict.status == "open",
            )
        )
    ).scalars().one_or_none()
    if existing is not None:
        return existing
    row = GraphExternalSyncConflict(
        library_id=mapping.library_id,
        sync_source_id=mapping.sync_source_id,
        mapping_id=mapping.id,
        operation_id=operation.id,
        reason_code=reason,
        incoming_hash=incoming_hash,
        current_hash=mapping.payload_hash,
        incoming_authority_rank=incoming_rank,
        current_authority_rank=current_rank,
        proposal=proposal,
        status="open",
    )
    db.add(row)
    await db.flush()
    return row


def _result(
    item: ExternalEntitySyncItem | ExternalRelationSyncItem,
    operation: str,
    mapping: GraphExternalFactMapping | None = None,
    conflict: GraphExternalSyncConflict | None = None,
) -> ExternalGraphSyncItemResult:
    fact_id = None
    if mapping is not None:
        fact_id = mapping.entity_id if item.fact_kind == "entity" else mapping.relation_id
    return ExternalGraphSyncItemResult(
        fact_kind=item.fact_kind,
        external_type=item.external_type,
        external_id=item.external_id,
        operation=operation,
        mapping_id=mapping.id if mapping else None,
        fact_id=fact_id,
        conflict_id=conflict.id if conflict else None,
    )


async def _entity_upsert(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    policy: GraphSyncSourcePolicy,
    operation: GraphExternalSyncOperation,
    item: ExternalEntitySyncItem,
) -> ExternalGraphSyncItemResult:
    incoming_hash = canonical_hash(item)
    mapping = await _mapping(db, library.id, source.id, item, for_update=True)
    if mapping is not None and mapping.payload_hash == incoming_hash and mapping.lifecycle == "active":
        mapping.last_seen_at = datetime.now(timezone.utc)
        mapping.last_operation_id = operation.id
        mapping.snapshot_id = operation.snapshot_id
        return _result(item, "unchanged", mapping)

    if mapping is None:
        entity_type = (
            await db.execute(
                select(EntityType).where(
                    EntityType.library_id == library.id,
                    EntityType.ontology_version_id == item.ontology_version_id,
                    EntityType.key == item.entity_type_key,
                    EntityType.status == "active",
                )
            )
        ).scalars().one_or_none()
        if entity_type is None:
            raise ExternalGraphSyncError("graph_sync_entity_type_not_found")
        entity = (
            await db.execute(
                select(Entity)
                .where(
                    Entity.library_id == library.id,
                    Entity.ontology_version_id == item.ontology_version_id,
                    Entity.entity_type_id == entity_type.id,
                    Entity.normalized_name == item.normalized_name,
                )
                .with_for_update()
            )
        ).scalars().one_or_none()
        entity_was_new = entity is None
        if entity_was_new:
            entity = Entity(
                library_id=library.id,
                ontology_version_id=item.ontology_version_id,
                entity_type_id=entity_type.id,
                canonical_name=item.canonical_name,
                normalized_name=item.normalized_name,
                properties=item.properties,
                status="pending_review",
                source_type="imported",
                authority_level=f"external:{policy.authority_rank}",
            )
            db.add(entity)
            await db.flush()
        competing_rank = None
        if not entity_was_new and entity.source_type == "imported":
            competing_rank = (
                await db.execute(
                    select(GraphSyncSourcePolicy.authority_rank)
                    .join(
                        GraphExternalFactMapping,
                        GraphExternalFactMapping.sync_source_id
                        == GraphSyncSourcePolicy.sync_source_id,
                    )
                    .where(
                        GraphExternalFactMapping.library_id == library.id,
                        GraphExternalFactMapping.entity_id == entity.id,
                        GraphExternalFactMapping.lifecycle == "active",
                        GraphExternalFactMapping.sync_source_id != source.id,
                        GraphSyncSourcePolicy.status == "active",
                    )
                    .order_by(GraphSyncSourcePolicy.authority_rank)
                    .limit(1)
                )
            ).scalar_one_or_none()
        authority_reason = None
        if entity.source_type == "manual":
            authority_reason = "manual_authority"
        elif competing_rank is not None and competing_rank <= policy.authority_rank:
            authority_reason = (
                "equal_authority_divergence"
                if competing_rank == policy.authority_rank
                else "stronger_source_authority"
            )
        elif (
            not entity_was_new
            and (
                entity.status not in ("draft", "pending_review")
                or (
                    await db.execute(
                        select(GraphPublicationItem.id)
                        .where(
                            GraphPublicationItem.entity_id == entity.id,
                            GraphPublicationItem.status.in_(("planned", "active")),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                is not None
            )
        ):
            authority_reason = "published_fact_change"
        mapping = GraphExternalFactMapping(
            library_id=library.id,
            sync_source_id=source.id,
            fact_kind="entity",
            external_type=item.external_type,
            external_id=item.external_id,
            entity_id=entity.id,
            lifecycle="stale" if authority_reason else "active",
            payload_hash=incoming_hash,
            source_version=item.source_version,
            source_event_id=operation.source_event_id,
            snapshot_id=operation.snapshot_id,
            evidence_id=await _evidence_id(db, library, item.evidence_id),
            source_locator=item.source_locator,
            last_operation_id=operation.id,
        )
        db.add(mapping)
        await db.flush()
        if authority_reason:
            conflict = await _conflict(
                db,
                mapping=mapping,
                operation=operation,
                reason=authority_reason,
                incoming_hash=incoming_hash,
                incoming_rank=policy.authority_rank,
                current_rank=(
                    0
                    if entity.source_type == "manual"
                    else competing_rank
                ),
                proposal=item.model_dump(mode="json"),
            )
            return _result(item, "conflict", mapping, conflict)
        if not entity_was_new:
            entity.canonical_name = item.canonical_name
            entity.normalized_name = item.normalized_name
            entity.properties = item.properties
            entity.status = "pending_review"
        return _result(item, "created", mapping)

    entity = (
        await db.execute(select(Entity).where(Entity.id == mapping.entity_id).with_for_update())
    ).scalars().one()
    reason = None
    competing_rank = await _competing_entity_rank(db, mapping)
    if entity.source_type == "manual":
        reason = "manual_authority"
    elif competing_rank is not None and competing_rank <= policy.authority_rank:
        reason = (
            "equal_authority_divergence"
            if competing_rank == policy.authority_rank
            else "stronger_source_authority"
        )
    elif entity.status not in ("draft", "pending_review") or await _published(db, mapping):
        reason = "published_fact_change"
    if reason:
        conflict = await _conflict(
            db,
            mapping=mapping,
            operation=operation,
            reason=reason,
            incoming_hash=incoming_hash,
            incoming_rank=policy.authority_rank,
            current_rank=(
                0
                if entity.source_type == "manual"
                else competing_rank or policy.authority_rank
            ),
            proposal=item.model_dump(mode="json"),
        )
        return _result(item, "conflict", mapping, conflict)

    entity.canonical_name = item.canonical_name
    entity.normalized_name = item.normalized_name
    entity.properties = item.properties
    entity.status = "pending_review"
    mapping.lifecycle = "active"
    mapping.payload_hash = incoming_hash
    mapping.source_version = item.source_version
    mapping.source_event_id = operation.source_event_id
    mapping.snapshot_id = operation.snapshot_id
    mapping.evidence_id = await _evidence_id(db, library, item.evidence_id)
    mapping.source_locator = item.source_locator
    mapping.last_operation_id = operation.id
    mapping.last_seen_at = datetime.now(timezone.utc)
    mapping.tombstoned_at = None
    return _result(item, "updated", mapping)


async def _relation_upsert(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    policy: GraphSyncSourcePolicy,
    operation: GraphExternalSyncOperation,
    item: ExternalRelationSyncItem,
) -> ExternalGraphSyncItemResult:
    incoming_hash = canonical_hash(item)
    mapping = await _mapping(db, library.id, source.id, item, for_update=True)
    if mapping is not None and mapping.payload_hash == incoming_hash and mapping.lifecycle == "active":
        mapping.last_seen_at = datetime.now(timezone.utc)
        mapping.last_operation_id = operation.id
        mapping.snapshot_id = operation.snapshot_id
        return _result(item, "unchanged", mapping)
    if mapping is None:
        relation_type = (
            await db.execute(
                select(RelationType).where(
                    RelationType.library_id == library.id,
                    RelationType.ontology_version_id == item.ontology_version_id,
                    RelationType.key == item.relation_type_key,
                    RelationType.status == "active",
                )
            )
        ).scalars().one_or_none()
        if relation_type is None:
            raise ExternalGraphSyncError("graph_sync_relation_type_not_found")
        endpoint_ids = []
        for reference in (item.source_entity, item.target_entity):
            endpoint = (
                await db.execute(
                    select(GraphExternalFactMapping).where(
                        GraphExternalFactMapping.library_id == library.id,
                        GraphExternalFactMapping.sync_source_id == source.id,
                        GraphExternalFactMapping.fact_kind == "entity",
                        GraphExternalFactMapping.external_type == reference.external_type,
                        GraphExternalFactMapping.external_id == reference.external_id,
                        GraphExternalFactMapping.lifecycle == "active",
                    )
                )
            ).scalars().one_or_none()
            if endpoint is None:
                raise ExternalGraphSyncError("graph_sync_relation_endpoint_not_found")
            endpoint_ids.append(endpoint.entity_id)
        relation = KnowledgeRelation(
            library_id=library.id,
            ontology_version_id=item.ontology_version_id,
            relation_type_id=relation_type.id,
            source_entity_id=endpoint_ids[0],
            target_entity_id=endpoint_ids[1],
            properties=item.properties,
            status="pending_review",
            review_status="pending_review",
            source_type="imported",
            authority_level=f"external:{policy.authority_rank}",
            valid_from=item.valid_from,
            valid_to=item.valid_to,
        )
        db.add(relation)
        await db.flush()
        mapping = GraphExternalFactMapping(
            library_id=library.id,
            sync_source_id=source.id,
            fact_kind="relation",
            external_type=item.external_type,
            external_id=item.external_id,
            relation_id=relation.id,
            lifecycle="active",
            payload_hash=incoming_hash,
            source_version=item.source_version,
            source_event_id=operation.source_event_id,
            snapshot_id=operation.snapshot_id,
            evidence_id=await _evidence_id(db, library, item.evidence_id),
            source_locator=item.source_locator,
            last_operation_id=operation.id,
        )
        db.add(mapping)
        await db.flush()
        return _result(item, "created", mapping)
    relation = (
        await db.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.id == mapping.relation_id)
            .with_for_update()
        )
    ).scalars().one()
    if relation.status not in ("draft", "pending_review") or await _published(db, mapping):
        conflict = await _conflict(
            db,
            mapping=mapping,
            operation=operation,
            reason="published_fact_change",
            incoming_hash=incoming_hash,
            incoming_rank=policy.authority_rank,
            current_rank=policy.authority_rank,
            proposal=item.model_dump(mode="json"),
        )
        return _result(item, "conflict", mapping, conflict)
    relation.properties = item.properties
    relation.valid_from = item.valid_from
    relation.valid_to = item.valid_to
    relation.status = "pending_review"
    relation.review_status = "pending_review"
    mapping.lifecycle = "active"
    mapping.payload_hash = incoming_hash
    mapping.source_version = item.source_version
    mapping.source_event_id = operation.source_event_id
    mapping.snapshot_id = operation.snapshot_id
    mapping.evidence_id = await _evidence_id(db, library, item.evidence_id)
    mapping.source_locator = item.source_locator
    mapping.last_operation_id = operation.id
    mapping.last_seen_at = datetime.now(timezone.utc)
    mapping.tombstoned_at = None
    return _result(item, "updated", mapping)


async def _delete(
    db: AsyncSession,
    library: Library,
    source: SyncSource,
    policy: GraphSyncSourcePolicy,
    operation: GraphExternalSyncOperation,
    item: ExternalEntitySyncItem | ExternalRelationSyncItem,
) -> ExternalGraphSyncItemResult:
    mapping = await _mapping(db, library.id, source.id, item, for_update=True)
    if mapping is None:
        return _result(item, "not_found")
    incoming_hash = canonical_hash(item)
    fact = (
        (
            await db.execute(select(Entity).where(Entity.id == mapping.entity_id).with_for_update())
        ).scalars().one()
        if item.fact_kind == "entity"
        else (
            await db.execute(
                select(KnowledgeRelation)
                .where(KnowledgeRelation.id == mapping.relation_id)
                .with_for_update()
            )
        ).scalars().one()
    )
    reason = None
    if fact.status not in ("draft", "pending_review", "rejected") or await _published(db, mapping):
        reason = "published_fact_delete"
    elif item.fact_kind == "entity":
        dependency = (
            await db.execute(
                select(KnowledgeRelation.id)
                .where(
                    KnowledgeRelation.library_id == library.id,
                    KnowledgeRelation.status.not_in(("deleted", "rejected")),
                    or_(
                        KnowledgeRelation.source_entity_id == mapping.entity_id,
                        KnowledgeRelation.target_entity_id == mapping.entity_id,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if dependency is not None:
            reason = "relation_dependency"
    if reason:
        conflict = await _conflict(
            db,
            mapping=mapping,
            operation=operation,
            reason=reason,
            incoming_hash=incoming_hash,
            incoming_rank=policy.authority_rank,
            current_rank=policy.authority_rank,
            proposal={"action": "delete"},
        )
        return _result(item, "conflict", mapping, conflict)
    mapping.lifecycle = "tombstoned"
    mapping.last_operation_id = operation.id
    mapping.tombstoned_at = datetime.now(timezone.utc)
    fact.status = "deleted"
    return _result(item, "deleted", mapping)


async def sync_batch(
    db: AsyncSession,
    library: Library,
    source_key: str,
    body: ExternalGraphSyncBatchRequest,
    *,
    actor_id: uuid.UUID | None,
) -> ExternalGraphSyncBatchResponse:
    if len(body.items) > settings.external_graph_sync_max_batch_items:
        raise ExternalGraphSyncError("graph_sync_batch_too_large")
    if any(
        len(
            json.dumps(
                item.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        > settings.external_graph_sync_max_item_bytes
        for item in body.items
    ):
        raise ExternalGraphSyncError("graph_sync_item_too_large")
    source, policy = await _source_and_policy(db, library, source_key, for_update=True)
    request_hash = canonical_hash(
        body.model_dump(mode="json", exclude={"idempotency_key"})
    )
    operation, replayed = await _operation(
        db, library, source, body, request_hash, actor_id
    )
    if replayed:
        payload = dict(operation.result_payload)
        payload["replayed"] = True
        return ExternalGraphSyncBatchResponse.model_validate(payload)

    results: list[ExternalGraphSyncItemResult] = []
    ordered = sorted(body.items, key=lambda item: (item.fact_kind == "relation", item.external_type, item.external_id))
    for item in ordered:
        if item.action == "delete":
            result = await _delete(db, library, source, policy, operation, item)
        elif item.fact_kind == "entity":
            result = await _entity_upsert(db, library, source, policy, operation, item)
        else:
            result = await _relation_upsert(db, library, source, policy, operation, item)
        results.append(result)

    stale_count = 0
    if body.complete_snapshot:
        stale_rows = list(
            (
                await db.execute(
                    select(GraphExternalFactMapping)
                    .where(
                        GraphExternalFactMapping.library_id == library.id,
                        GraphExternalFactMapping.sync_source_id == source.id,
                        GraphExternalFactMapping.lifecycle == "active",
                        or_(
                            GraphExternalFactMapping.snapshot_id.is_(None),
                            GraphExternalFactMapping.snapshot_id != body.snapshot_id,
                        ),
                    )
                    .with_for_update()
                )
            ).scalars().all()
        )
        for mapping in stale_rows:
            mapping.lifecycle = "stale"
            mapping.last_operation_id = operation.id
            await _conflict(
                db,
                mapping=mapping,
                operation=operation,
                reason="source_snapshot_stale",
                incoming_hash=mapping.payload_hash,
                incoming_rank=policy.authority_rank,
                current_rank=policy.authority_rank,
                proposal={"snapshot_id": body.snapshot_id},
            )
        stale_count = len(stale_rows)

    counts = {
        name: sum(result.operation == name for result in results)
        for name in ("created", "updated", "unchanged", "deleted", "conflict")
    }
    operation.created_count = counts["created"]
    operation.updated_count = counts["updated"]
    operation.unchanged_count = counts["unchanged"]
    operation.deleted_count = counts["deleted"]
    operation.conflict_count = counts["conflict"] + stale_count
    operation.stale_count = stale_count
    operation.status = "conflicted" if operation.conflict_count else "applied"
    operation.finished_at = datetime.now(timezone.utc)
    response = ExternalGraphSyncBatchResponse(
        operation_id=operation.id,
        replayed=False,
        status=operation.status,
        created_count=operation.created_count,
        updated_count=operation.updated_count,
        unchanged_count=operation.unchanged_count,
        deleted_count=operation.deleted_count,
        conflict_count=operation.conflict_count,
        stale_count=operation.stale_count,
        items=results,
    )
    operation.result_payload = response.model_dump(mode="json")
    await db.flush()
    return response


async def list_mappings(
    db: AsyncSession,
    library: Library,
    source_key: str,
    *,
    limit: int,
) -> list[ExternalGraphMappingRead]:
    source = await require_active_sync_source(db, library, source_key)
    rows = list(
        (
            await db.execute(
                select(GraphExternalFactMapping)
                .where(
                    GraphExternalFactMapping.library_id == library.id,
                    GraphExternalFactMapping.sync_source_id == source.id,
                )
                .order_by(
                    GraphExternalFactMapping.fact_kind,
                    GraphExternalFactMapping.external_type,
                    GraphExternalFactMapping.external_id,
                )
                .limit(limit)
            )
        ).scalars().all()
    )
    return [
        ExternalGraphMappingRead(
            id=row.id,
            source_key=source.source_key,
            fact_kind=row.fact_kind,
            external_type=row.external_type,
            external_id=row.external_id,
            fact_id=row.entity_id if row.fact_kind == "entity" else row.relation_id,
            lifecycle=row.lifecycle,
            source_version=row.source_version,
            evidence_id=row.evidence_id,
            source_locator=row.source_locator,
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]


async def list_conflicts(
    db: AsyncSession,
    library: Library,
    *,
    limit: int,
) -> list[ExternalGraphConflictRead]:
    rows = (
        await db.execute(
            select(GraphExternalSyncConflict, SyncSource.source_key)
            .join(SyncSource, SyncSource.id == GraphExternalSyncConflict.sync_source_id)
            .where(GraphExternalSyncConflict.library_id == library.id)
            .order_by(GraphExternalSyncConflict.created_at.desc(), GraphExternalSyncConflict.id)
            .limit(limit)
        )
    ).all()
    return [
        ExternalGraphConflictRead(
            id=row.id,
            source_key=source_key,
            mapping_id=row.mapping_id,
            operation_id=row.operation_id,
            reason_code=row.reason_code,
            incoming_hash=row.incoming_hash,
            current_hash=row.current_hash,
            incoming_authority_rank=row.incoming_authority_rank,
            current_authority_rank=row.current_authority_rank,
            status=row.status,
            created_at=row.created_at,
        )
        for row, source_key in rows
    ]


async def decide_conflict(
    db: AsyncSession,
    library: Library,
    conflict_id: uuid.UUID,
    body: ExternalGraphConflictDecision,
) -> GraphExternalSyncConflict:
    row = (
        await db.execute(
            select(GraphExternalSyncConflict)
            .where(
                GraphExternalSyncConflict.id == conflict_id,
                GraphExternalSyncConflict.library_id == library.id,
            )
            .with_for_update()
        )
    ).scalars().one_or_none()
    if row is None:
        raise ExternalGraphSyncError("graph_sync_conflict_not_found")
    if row.status == body.decision:
        return row
    if row.status != "open":
        raise ExternalGraphSyncError("graph_sync_conflict_already_decided")
    row.status = body.decision
    row.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    return row
