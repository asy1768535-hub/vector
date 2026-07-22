from __future__ import annotations

import copy
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.entity import GRAPH_FACT_STATUS_ACTIVE, Entity
from app.models.evidence_unit import EvidenceUnit
from app.models.document_revision_file import DocumentRevisionFile
from app.models.graph_publication import (
    GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
    GRAPH_PUBLICATION_SOURCE_ROLLBACK,
    GRAPH_PUBLICATION_STATUS_ACTIVE,
    GRAPH_PUBLICATION_STATUS_ACTIVATING,
    GRAPH_PUBLICATION_STATUS_DEGRADED,
    GRAPH_PUBLICATION_STATUS_FAILED,
    GRAPH_PUBLICATION_STATUS_PLANNED,
    GRAPH_PUBLICATION_STATUS_SUPERSEDED,
    GraphPublication,
)
from app.models.graph_publication_item import (
    GRAPH_PUBLICATION_ITEM_KIND_ENTITY,
    GRAPH_PUBLICATION_ITEM_KIND_RELATION,
    GRAPH_PUBLICATION_ITEM_STATUS_ACTIVE,
    GRAPH_PUBLICATION_ITEM_STATUS_PLANNED,
    GRAPH_PUBLICATION_ITEM_STATUS_SUPERSEDED,
    GraphPublicationItem,
)
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.revision_retention import RevisionRetentionRecord
from app.models.revision_purge_operation import RevisionPurgeOperation
from app.services import audit_log
from app.services.graph_governance_contracts import GraphGovernanceError
from app.services.graph_governance_publication import (
    LoadedGraphGovernanceProjection,
    apply_loaded_graph_governance_projection,
    load_graph_governance_projection,
    parse_graph_governance_plan,
    release_graph_governance_publication,
)
from app.services.graph_publication_planner import (
    GraphPublicationPlanResult,
    GraphPublicationSnapshot,
    GraphPublicationProjection,
    _manifest_hash,
    _sha256_json,
    build_graph_publication_snapshot,
    build_publication_policy_snapshot,
)


log = logging.getLogger(__name__)

CURRENT_PUBLICATION_STATUSES = (
    GRAPH_PUBLICATION_STATUS_ACTIVE,
    GRAPH_PUBLICATION_STATUS_DEGRADED,
)
ROLLBACK_TARGET_STATUSES = (
    GRAPH_PUBLICATION_STATUS_ACTIVE,
    GRAPH_PUBLICATION_STATUS_DEGRADED,
    GRAPH_PUBLICATION_STATUS_SUPERSEDED,
)


class GraphPublicationActivationError(RuntimeError):
    def __init__(self, code: str, message: str, *, persist_failure: bool = True) -> None:
        self.code = code
        self.persist_failure = persist_failure
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphPublicationActivationResult:
    publication: GraphPublication
    previous_publication_id: uuid.UUID | None
    idempotent: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _command_key_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _item_key(item: GraphPublicationItem) -> tuple[str, uuid.UUID]:
    if item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_ENTITY and item.entity_id is not None:
        return item.item_kind, item.entity_id
    if item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_RELATION and item.relation_id is not None:
        return item.item_kind, item.relation_id
    raise GraphPublicationActivationError("publication_item_invalid", "publication item is invalid")


async def _lock_library(db: AsyncSession, library_id: uuid.UUID) -> Library:
    result = await db.execute(
        select(Library)
        .where(Library.id == library_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    library = result.scalars().first()
    if library is None or library.deleted_at is not None:
        raise GraphPublicationActivationError("library_not_found", "library not found")
    return library


async def _lock_publication(db: AsyncSession, publication_id: uuid.UUID) -> GraphPublication:
    result = await db.execute(
        select(GraphPublication)
        .where(GraphPublication.id == publication_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    publication = result.scalars().first()
    if publication is None:
        raise GraphPublicationActivationError(
            "publication_not_found",
            "graph publication not found",
            persist_failure=False,
        )
    return publication


async def _lock_current_publication(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
) -> GraphPublication | None:
    result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.library_id == library_id,
            GraphPublication.ontology_version_id == ontology_version_id,
            GraphPublication.status.in_(CURRENT_PUBLICATION_STATUSES),
        )
        .order_by(GraphPublication.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def _lock_publication_items(
    db: AsyncSession,
    publication_id: uuid.UUID,
) -> list[GraphPublicationItem]:
    result = await db.execute(
        select(GraphPublicationItem)
        .where(GraphPublicationItem.publication_id == publication_id)
        .order_by(GraphPublicationItem.item_kind, GraphPublicationItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _lock_active_ontology(
    db: AsyncSession,
    publication: GraphPublication,
) -> OntologyVersion:
    result = await db.execute(
        select(OntologyVersion)
        .where(OntologyVersion.id == publication.ontology_version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    ontology = result.scalars().first()
    if (
        ontology is None
        or ontology.library_id != publication.library_id
        or ontology.status != ONTOLOGY_STATUS_ACTIVE
    ):
        raise GraphPublicationActivationError("ontology_not_active", "ontology version is not active")
    return ontology


def _validate_stored_snapshot(
    publication: GraphPublication,
    stored_items: list[GraphPublicationItem],
    candidate: GraphPublicationSnapshot,
    *,
    config: Settings,
) -> None:
    if publication.manifest_version != config.graph_publication_manifest_version:
        raise GraphPublicationActivationError("manifest_version_changed", "publication manifest version changed")
    if publication.policy_version != config.graph_publication_policy_version:
        raise GraphPublicationActivationError("publication_policy_changed", "publication policy changed")
    current_policy = build_publication_policy_snapshot(config)
    if publication.policy_snapshot != current_policy:
        raise GraphPublicationActivationError("publication_policy_changed", "publication policy changed")

    entity_count = sum(item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_ENTITY for item in stored_items)
    relation_count = sum(item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_RELATION for item in stored_items)
    if entity_count != publication.entity_count or relation_count != publication.relation_count:
        raise GraphPublicationActivationError("publication_count_mismatch", "publication item count mismatch")

    stored_by_key: dict[tuple[str, uuid.UUID], GraphPublicationItem] = {}
    for item in stored_items:
        if item.library_id != publication.library_id or item.ontology_version_id != publication.ontology_version_id:
            raise GraphPublicationActivationError("publication_scope_mismatch", "publication item scope mismatch")
        if item.status != GRAPH_PUBLICATION_ITEM_STATUS_PLANNED:
            raise GraphPublicationActivationError("publication_item_state_invalid", "publication item state is invalid")
        key = _item_key(item)
        if key in stored_by_key:
            raise GraphPublicationActivationError("publication_item_duplicate", "publication item is duplicated")
        stored_by_key[key] = item

    candidate_by_key = {_item_key(item): item for item in candidate.items}
    if publication.source_mode in {
        GRAPH_PUBLICATION_SOURCE_ROLLBACK,
        GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
    }:
        if not set(stored_by_key).issubset(candidate_by_key):
            code = (
                "rollback_item_ineligible"
                if publication.source_mode == GRAPH_PUBLICATION_SOURCE_ROLLBACK
                else "coordinated_purge_item_ineligible"
            )
            raise GraphPublicationActivationError(
                code, "publication snapshot is no longer eligible"
            )
    else:
        if set(stored_by_key) != set(candidate_by_key):
            raise GraphPublicationActivationError("publication_snapshot_changed", "publication snapshot changed")
        if dict(publication.blocked_counts or {}) != candidate.blocked_counts:
            raise GraphPublicationActivationError("publication_snapshot_changed", "publication snapshot changed")

    for key, stored in stored_by_key.items():
        current = candidate_by_key.get(key)
        if (
            current is None
            or stored.item_hash != current.item_hash
            or list(stored.support_evidence_ids or []) != list(current.support_evidence_ids or [])
            or dict(stored.support_counts or {}) != dict(current.support_counts or {})
            or dict(stored.fact_snapshot or {}) != dict(current.fact_snapshot or {})
        ):
            if publication.source_mode == GRAPH_PUBLICATION_SOURCE_ROLLBACK:
                code = "rollback_item_ineligible"
            elif publication.source_mode == GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE:
                code = "coordinated_purge_item_ineligible"
            else:
                code = "publication_item_changed"
            raise GraphPublicationActivationError(code, "publication item is no longer eligible")

    expected_manifest_hash = _manifest_hash(
        config=config,
        policy_snapshot_hash=_sha256_json(publication.policy_snapshot),
        library_id=publication.library_id,
        ontology_version_id=publication.ontology_version_id,
        source_mode=publication.source_mode,
        parent_publication_id=publication.parent_publication_id,
        include_drafts=publication.include_drafts,
        items=stored_items,
        blocked_counts=dict(publication.blocked_counts or {}),
        governance_action_set_hash=candidate.governance_action_set_hash,
    )
    if expected_manifest_hash != publication.manifest_hash:
        raise GraphPublicationActivationError("publication_manifest_mismatch", "publication manifest mismatch")


async def _reject_retiring_support_evidence(
    db: AsyncSession,
    publication: GraphPublication,
    items: list[GraphPublicationItem],
) -> None:
    support_ids: set[uuid.UUID] = set()
    try:
        for item in items:
            support_ids.update(
                uuid.UUID(str(value)) for value in (item.support_evidence_ids or [])
            )
    except (TypeError, ValueError, AttributeError):
        raise GraphPublicationActivationError(
            "publication_item_invalid", "publication support Evidence is invalid"
        ) from None
    if not support_ids:
        return
    retiring = (
        await db.execute(
            select(RevisionRetentionRecord.id)
            .select_from(EvidenceUnit)
            .join(
                RevisionRetentionRecord,
                RevisionRetentionRecord.document_revision_id
                == EvidenceUnit.document_revision_id,
            )
            .join(
                DocumentRevisionFile,
                DocumentRevisionFile.id == RevisionRetentionRecord.revision_file_id,
            )
            .where(
                EvidenceUnit.library_id == publication.library_id,
                EvidenceUnit.id.in_(support_ids),
                RevisionRetentionRecord.library_id == publication.library_id,
                or_(
                    RevisionRetentionRecord.status.in_(
                        ("queued", "processing", "cleaned")
                    ),
                    DocumentRevisionFile.lifecycle_status != "available",
                ),
            )
            .limit(1)
        )
    ).first()
    if retiring is not None:
        raise GraphPublicationActivationError(
            "publication_evidence_retiring",
            "publication support Evidence is retiring",
        )


async def _reject_coordinated_target_evidence(
    db: AsyncSession,
    publication: GraphPublication,
    items: list[GraphPublicationItem],
) -> None:
    if publication.source_mode != GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE:
        return
    operation = (
        await db.execute(
            select(RevisionPurgeOperation).where(
                RevisionPurgeOperation.replacement_publication_id == publication.id,
                RevisionPurgeOperation.library_id == publication.library_id,
            )
        )
    ).scalars().first()
    if operation is None or operation.status not in {"planned", "processing"}:
        raise GraphPublicationActivationError(
            "coordinated_purge_operation_invalid",
            "coordinated purge operation is unavailable",
        )
    options = dict(publication.plan_options or {})
    impact = dict(operation.impact_snapshot or {})
    if (
        options.get("retention_record_id") != str(operation.retention_record_id)
        or options.get("confirmation_hash") != operation.confirmation_hash
        or options.get("target_evidence_hash") != impact.get("target_evidence_hash")
        or publication.manifest_hash != operation.replacement_manifest_hash
        or publication.parent_publication_id != operation.source_publication_id
    ):
        raise GraphPublicationActivationError(
            "coordinated_purge_operation_changed",
            "coordinated purge operation identity changed",
        )
    support_ids: set[uuid.UUID] = set()
    try:
        for item in items:
            support_ids.update(
                uuid.UUID(str(value)) for value in (item.support_evidence_ids or [])
            )
    except (TypeError, ValueError, AttributeError):
        raise GraphPublicationActivationError(
            "publication_item_invalid", "publication support Evidence is invalid"
        ) from None
    if not support_ids:
        return
    target = (
        await db.execute(
            select(EvidenceUnit.id)
            .where(
                EvidenceUnit.library_id == publication.library_id,
                EvidenceUnit.document_revision_id == operation.document_revision_id,
                EvidenceUnit.id.in_(support_ids),
            )
            .limit(1)
        )
    ).first()
    if target is not None:
        raise GraphPublicationActivationError(
            "coordinated_purge_target_evidence",
            "coordinated replacement still cites target Evidence",
        )


async def _supersede_previous_items(
    db: AsyncSession,
    previous: GraphPublication | None,
) -> None:
    if previous is None:
        return
    previous_items = await _lock_publication_items(db, previous.id)
    for item in previous_items:
        item.status = GRAPH_PUBLICATION_ITEM_STATUS_SUPERSEDED


async def _switch_formal_graph(
    db: AsyncSession,
    publication: GraphPublication,
    items: list[GraphPublicationItem],
) -> None:
    entity_ids = [item.entity_id for item in items if item.entity_id is not None]
    relation_ids = [item.relation_id for item in items if item.relation_id is not None]

    if entity_ids:
        await db.execute(
            update(Entity)
            .where(
                Entity.library_id == publication.library_id,
                Entity.ontology_version_id == publication.ontology_version_id,
                Entity.id.in_(entity_ids),
            )
            .values(status=GRAPH_FACT_STATUS_ACTIVE)
        )
    if relation_ids:
        await db.execute(
            update(KnowledgeRelation)
            .where(
                KnowledgeRelation.library_id == publication.library_id,
                KnowledgeRelation.ontology_version_id == publication.ontology_version_id,
                KnowledgeRelation.id.in_(relation_ids),
            )
            .values(status=GRAPH_FACT_STATUS_ACTIVE)
        )

    omitted_relation_ids = select(KnowledgeRelation.id).where(
        KnowledgeRelation.library_id == publication.library_id,
        KnowledgeRelation.ontology_version_id == publication.ontology_version_id,
        KnowledgeRelation.status == GRAPH_FACT_STATUS_ACTIVE,
    )
    if relation_ids:
        omitted_relation_ids = omitted_relation_ids.where(KnowledgeRelation.id.not_in(relation_ids))
    await db.execute(
        update(RelationEvidence)
        .where(
            RelationEvidence.library_id == publication.library_id,
            RelationEvidence.status == GRAPH_PUBLICATION_ITEM_STATUS_ACTIVE,
            RelationEvidence.relation_id.in_(omitted_relation_ids),
        )
        .values(status="stale")
    )
    stale_relations = (
        update(KnowledgeRelation)
        .where(
            KnowledgeRelation.library_id == publication.library_id,
            KnowledgeRelation.ontology_version_id == publication.ontology_version_id,
            KnowledgeRelation.status == GRAPH_FACT_STATUS_ACTIVE,
        )
        .values(status="stale")
    )
    if relation_ids:
        stale_relations = stale_relations.where(KnowledgeRelation.id.not_in(relation_ids))
    await db.execute(stale_relations)


async def _build_rollback_status_projection(
    db: AsyncSession,
    *,
    publication: GraphPublication,
    items: list[GraphPublicationItem],
) -> GraphPublicationProjection:
    entity_ids = tuple(
        sorted({item.entity_id for item in items if item.entity_id is not None}, key=str)
    )
    relation_ids = tuple(
        sorted(
            {item.relation_id for item in items if item.relation_id is not None},
            key=str,
        )
    )
    entities: tuple[Entity, ...] = ()
    relations: tuple[KnowledgeRelation, ...] = ()
    initial_entity_values = []
    for entity_id in entity_ids:
        value = await db.get(Entity, entity_id)
        if value is not None:
            initial_entity_values.append(value)
    initial_entities = tuple(initial_entity_values)
    initial_relation_values = []
    for relation_id in relation_ids:
        value = await db.get(KnowledgeRelation, relation_id)
        if value is not None:
            initial_relation_values.append(value)
    initial_relations = tuple(initial_relation_values)
    restorable = {"active", "disabled", "stale"}
    if (
        len(initial_entities) != len(entity_ids)
        or len(initial_relations) != len(relation_ids)
        or any(
            value.library_id != publication.library_id
            or value.ontology_version_id != publication.ontology_version_id
            or value.status not in restorable
            for value in (*initial_entities, *initial_relations)
        )
    ):
        raise GraphPublicationActivationError(
            "rollback_item_ineligible",
            "rollback fact state is not eligible",
            persist_failure=False,
        )
    if entity_ids:
        entities = tuple(
            (
                await db.execute(
                    select(Entity)
                    .where(
                        Entity.library_id == publication.library_id,
                        Entity.ontology_version_id == publication.ontology_version_id,
                        Entity.id.in_(entity_ids),
                    )
                    .order_by(Entity.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    if relation_ids:
        relations = tuple(
            (
                await db.execute(
                    select(KnowledgeRelation)
                    .where(
                        KnowledgeRelation.library_id == publication.library_id,
                        KnowledgeRelation.ontology_version_id
                        == publication.ontology_version_id,
                        KnowledgeRelation.id.in_(relation_ids),
                    )
                    .order_by(KnowledgeRelation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    if len(entities) != len(entity_ids) or len(relations) != len(relation_ids):
        raise GraphPublicationActivationError(
            "rollback_item_ineligible",
            "rollback fact is unavailable",
            persist_failure=False,
        )
    if any(value.status not in restorable for value in (*entities, *relations)):
        raise GraphPublicationActivationError(
            "rollback_item_ineligible",
            "rollback fact state is not eligible",
            persist_failure=False,
        )
    return GraphPublicationProjection(
        entity_states={
            value.id: {"status": "active"}
            for value in entities
            if value.status != "active"
        },
        relation_states={
            value.id: {"status": "active"}
            for value in relations
            if value.status != "active"
        },
    )


def _snapshot_covers_items(
    snapshot: GraphPublicationSnapshot,
    items: list[GraphPublicationItem],
) -> bool:
    candidate_keys = {_item_key(item) for item in snapshot.items}
    return all(_item_key(item) in candidate_keys for item in items)


async def _activate_locked(
    db: AsyncSession,
    publication_id: uuid.UUID,
    *,
    activated_by_user_id: uuid.UUID | None,
    expected_manifest_hash: str | None,
    command_idempotency_key: str | None,
    config: Settings,
) -> GraphPublicationActivationResult:
    initial = await db.get(GraphPublication, publication_id)
    if initial is None:
        raise GraphPublicationActivationError(
            "publication_not_found",
            "graph publication not found",
            persist_failure=False,
        )
    library = await _lock_library(db, initial.library_id)
    publication = await _lock_publication(db, publication_id)
    if publication.library_id != library.id:
        raise GraphPublicationActivationError("publication_scope_mismatch", "publication scope mismatch")
    if expected_manifest_hash is not None and publication.manifest_hash != expected_manifest_hash:
        raise GraphPublicationActivationError(
            "expected_manifest_mismatch",
            "publication manifest does not match the expected hash",
            persist_failure=False,
        )

    if publication.status == GRAPH_PUBLICATION_STATUS_ACTIVE:
        return GraphPublicationActivationResult(
            publication=publication,
            previous_publication_id=publication.parent_publication_id,
            idempotent=True,
        )
    if publication.status not in {GRAPH_PUBLICATION_STATUS_PLANNED, GRAPH_PUBLICATION_STATUS_ACTIVATING}:
        raise GraphPublicationActivationError(
            "publication_state_invalid",
            "publication cannot be activated from its current state",
            persist_failure=False,
        )
    if not config.graph_publication_enabled:
        raise GraphPublicationActivationError("publication_disabled", "graph publication is disabled")

    ontology = await _lock_active_ontology(db, publication)
    previous = await _lock_current_publication(
        db,
        library_id=publication.library_id,
        ontology_version_id=publication.ontology_version_id,
    )
    previous_id = previous.id if previous is not None else None
    if publication.parent_publication_id != previous_id:
        raise GraphPublicationActivationError("publication_parent_changed", "current publication changed")

    items = await _lock_publication_items(db, publication.id)
    governance: LoadedGraphGovernanceProjection | None = None
    projection: GraphPublicationProjection | None = None
    try:
        governance_plan = parse_graph_governance_plan(publication)
        if governance_plan is not None:
            if not config.graph_governance_enabled:
                raise GraphPublicationActivationError(
                    "graph_governance_unavailable",
                    "graph governance is disabled",
                )
            action_ids, expected_action_set_hash = governance_plan
            governance = await load_graph_governance_projection(
                db,
                library_id=publication.library_id,
                ontology_version_id=publication.ontology_version_id,
                action_ids=action_ids,
                publication_id=publication.id,
                require_publication_binding=True,
            )
            if governance.projection.action_set_hash != expected_action_set_hash:
                raise GraphPublicationActivationError(
                    "graph_governance_publication_changed",
                    "graph governance Publication changed",
                )
            projection = governance.projection
    except GraphGovernanceError as exc:
        raise GraphPublicationActivationError(exc.code, str(exc)) from exc
    candidate = await build_graph_publication_snapshot(
        db,
        library,
        ontology,
        include_drafts=publication.include_drafts,
        projection=projection,
        config=config,
    )
    if (
        governance is None
        and publication.source_mode == GRAPH_PUBLICATION_SOURCE_ROLLBACK
        and not _snapshot_covers_items(candidate, items)
    ):
        projection = await _build_rollback_status_projection(
            db,
            publication=publication,
            items=items,
        )
        candidate = await build_graph_publication_snapshot(
            db,
            library,
            ontology,
            include_drafts=publication.include_drafts,
            projection=projection,
            config=config,
        )
    _validate_stored_snapshot(publication, items, candidate, config=config)
    await _reject_coordinated_target_evidence(db, publication, items)
    await _reject_retiring_support_evidence(db, publication, items)

    publication.status = GRAPH_PUBLICATION_STATUS_ACTIVATING
    await db.flush()
    if governance is not None:
        try:
            await apply_loaded_graph_governance_projection(
                db,
                publication=publication,
                loaded=governance,
                actor_user_id=activated_by_user_id,
            )
        except GraphGovernanceError as exc:
            raise GraphPublicationActivationError(exc.code, str(exc)) from exc
    await _switch_formal_graph(db, publication, items)
    await _supersede_previous_items(db, previous)

    now = _now()
    if previous is not None:
        previous.status = GRAPH_PUBLICATION_STATUS_SUPERSEDED
        previous.superseded_by_publication_id = publication.id
        previous.superseded_at = now
        await audit_log.record(
            db,
            activated_by_user_id,
            "graph_publication.superseded",
            {
                "publication_id": str(previous.id),
                "superseded_by_publication_id": str(publication.id),
                "library_id": str(publication.library_id),
                "ontology_version_id": str(publication.ontology_version_id),
            },
        )
        # The partial current-publication index is immediate. Flush the old
        # current row first instead of relying on ORM UPDATE ordering.
        await db.flush()
    for item in items:
        item.status = GRAPH_PUBLICATION_ITEM_STATUS_ACTIVE
    publication.status = GRAPH_PUBLICATION_STATUS_ACTIVE
    publication.activated_by_user_id = activated_by_user_id
    publication.activated_at = now
    publication.error_code = None
    publication.error_message = None
    active_audit_target = {
        "publication_id": str(publication.id),
        "previous_publication_id": str(previous_id) if previous_id is not None else None,
        "library_id": str(publication.library_id),
        "ontology_version_id": str(publication.ontology_version_id),
        "entity_count": publication.entity_count,
        "relation_count": publication.relation_count,
    }
    command_hash = _command_key_hash(command_idempotency_key)
    if command_hash is not None:
        active_audit_target["command_idempotency_hash"] = command_hash
    await audit_log.record(
        db,
        activated_by_user_id,
        "graph_publication.active",
        active_audit_target,
    )
    await db.flush()
    return GraphPublicationActivationResult(
        publication=publication,
        previous_publication_id=previous_id,
    )


async def _persist_activation_failure(
    db: AsyncSession,
    publication_id: uuid.UUID,
    error: GraphPublicationActivationError,
) -> None:
    if not error.persist_failure:
        return
    initial = await db.get(GraphPublication, publication_id)
    if initial is None:
        return
    await _lock_library(db, initial.library_id)
    publication = await _lock_publication(db, publication_id)
    if publication.status not in {GRAPH_PUBLICATION_STATUS_PLANNED, GRAPH_PUBLICATION_STATUS_ACTIVATING}:
        return
    publication.status = GRAPH_PUBLICATION_STATUS_FAILED
    publication.error_code = error.code[:64]
    publication.error_message = str(error)[:255]
    publication.failed_at = _now()
    try:
        await release_graph_governance_publication(db, publication)
    except GraphGovernanceError as exc:
        raise GraphPublicationActivationError(exc.code, str(exc)) from exc
    await audit_log.record(
        db,
        None,
        "graph_publication.failed",
        {
            "publication_id": str(publication.id),
            "library_id": str(publication.library_id),
            "ontology_version_id": str(publication.ontology_version_id),
            "error_code": publication.error_code,
        },
    )
    await db.flush()


async def activate_graph_publication(
    db: AsyncSession,
    publication_id: uuid.UUID,
    *,
    activated_by_user_id: uuid.UUID | None = None,
    expected_manifest_hash: str | None = None,
    command_idempotency_key: str | None = None,
    config: Settings = settings,
) -> GraphPublicationActivationResult:
    if db.in_transaction():
        raise GraphPublicationActivationError(
            "activation_transaction_active",
            "activation requires an idle database session",
            persist_failure=False,
        )
    try:
        result = await _activate_locked(
            db,
            publication_id,
            activated_by_user_id=activated_by_user_id,
            expected_manifest_hash=expected_manifest_hash,
            command_idempotency_key=command_idempotency_key,
            config=config,
        )
        await db.commit()
        log.info(
            "graph publication activation completed publication_id=%s previous_publication_id=%s idempotent=%s",
            result.publication.id,
            result.previous_publication_id,
            result.idempotent,
        )
        return result
    except GraphPublicationActivationError as exc:
        await db.rollback()
        try:
            await _persist_activation_failure(db, publication_id, exc)
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception(
                "graph publication failure record could not be persisted publication_id=%s error_code=%s",
                publication_id,
                exc.code,
            )
        raise
    except Exception as exc:
        await db.rollback()
        sanitized = GraphPublicationActivationError("activation_failed", "graph publication activation failed")
        try:
            await _persist_activation_failure(db, publication_id, sanitized)
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception(
                "graph publication failure record could not be persisted publication_id=%s",
                publication_id,
            )
        raise sanitized from exc


async def plan_graph_publication_rollback(
    db: AsyncSession,
    target_publication_id: uuid.UUID,
    *,
    idempotency_key: str,
    dry_run: bool = False,
    requested_by_user_id: uuid.UUID | None = None,
    config: Settings = settings,
) -> GraphPublicationPlanResult:
    if not idempotency_key:
        raise GraphPublicationActivationError("idempotency_key_required", "idempotency key is required")
    target_initial = await db.get(GraphPublication, target_publication_id)
    if target_initial is None:
        raise GraphPublicationActivationError(
            "rollback_target_not_found",
            "rollback target not found",
            persist_failure=False,
        )
    library = await _lock_library(db, target_initial.library_id)
    target = await _lock_publication(db, target_publication_id)
    if target.status not in ROLLBACK_TARGET_STATUSES:
        raise GraphPublicationActivationError("rollback_target_invalid", "rollback target is not eligible")
    ontology = await _lock_active_ontology(db, target)
    current = await _lock_current_publication(
        db,
        library_id=target.library_id,
        ontology_version_id=target.ontology_version_id,
    )
    if current is None or current.id == target.id:
        raise GraphPublicationActivationError("rollback_target_is_current", "rollback target is already current")

    if not dry_run:
        replay_result = await db.execute(
            select(GraphPublication).where(
                GraphPublication.library_id == target.library_id,
                GraphPublication.ontology_version_id == target.ontology_version_id,
                GraphPublication.idempotency_key == idempotency_key,
                GraphPublication.status.in_(
                    (GRAPH_PUBLICATION_STATUS_PLANNED, GRAPH_PUBLICATION_STATUS_ACTIVATING)
                ),
            )
        )
        replay = replay_result.scalars().first()
        if replay is not None:
            if (
                replay.source_mode != GRAPH_PUBLICATION_SOURCE_ROLLBACK
                or replay.rollback_target_publication_id != target.id
            ):
                raise GraphPublicationActivationError("idempotency_conflict", "idempotency key conflicts")
            return GraphPublicationPlanResult(
                publication=replay,
                items=(),
                manifest_hash=replay.manifest_hash,
                policy_snapshot_hash=_sha256_json(replay.policy_snapshot),
                blocked_counts=dict(replay.blocked_counts or {}),
                reused=True,
            )

    target_items = await _lock_publication_items(db, target.id)
    candidate = await build_graph_publication_snapshot(
        db,
        library,
        ontology,
        include_drafts=target.include_drafts,
        config=config,
    )
    if not _snapshot_covers_items(candidate, target_items):
        rollback_projection = await _build_rollback_status_projection(
            db,
            publication=target,
            items=target_items,
        )
        candidate = await build_graph_publication_snapshot(
            db,
            library,
            ontology,
            include_drafts=target.include_drafts,
            projection=rollback_projection,
            config=config,
        )
    candidate_by_key = {_item_key(item): item for item in candidate.items}
    for target_item in target_items:
        current_item = candidate_by_key.get(_item_key(target_item))
        if current_item is None or current_item.item_hash != target_item.item_hash:
            raise GraphPublicationActivationError(
                "rollback_item_ineligible",
                "rollback snapshot is no longer eligible",
                persist_failure=False,
            )

    copied_items = [
        GraphPublicationItem(
            library_id=target.library_id,
            ontology_version_id=target.ontology_version_id,
            item_kind=item.item_kind,
            entity_id=item.entity_id,
            relation_id=item.relation_id,
            item_hash=item.item_hash,
            status=GRAPH_PUBLICATION_ITEM_STATUS_PLANNED,
            support_evidence_ids=copy.deepcopy(item.support_evidence_ids or []),
            support_counts=copy.deepcopy(item.support_counts or {}),
            fact_snapshot=copy.deepcopy(item.fact_snapshot or {}),
        )
        for item in target_items
    ]
    policy_snapshot = build_publication_policy_snapshot(config)
    policy_snapshot_hash = _sha256_json(policy_snapshot)
    blocked_counts: dict[str, int] = {}
    manifest_hash = _manifest_hash(
        config=config,
        policy_snapshot_hash=policy_snapshot_hash,
        library_id=target.library_id,
        ontology_version_id=target.ontology_version_id,
        source_mode=GRAPH_PUBLICATION_SOURCE_ROLLBACK,
        parent_publication_id=current.id,
        include_drafts=target.include_drafts,
        items=copied_items,
        blocked_counts=blocked_counts,
    )
    if not dry_run:
        reusable_result = await db.execute(
            select(GraphPublication).where(
                GraphPublication.library_id == target.library_id,
                GraphPublication.ontology_version_id == target.ontology_version_id,
                GraphPublication.manifest_hash == manifest_hash,
                GraphPublication.status.in_(
                    (
                        GRAPH_PUBLICATION_STATUS_PLANNED,
                        GRAPH_PUBLICATION_STATUS_ACTIVATING,
                        GRAPH_PUBLICATION_STATUS_ACTIVE,
                        GRAPH_PUBLICATION_STATUS_DEGRADED,
                    )
                ),
            )
        )
        reusable = reusable_result.scalars().first()
        if reusable is not None:
            return GraphPublicationPlanResult(
                publication=reusable,
                items=(),
                manifest_hash=reusable.manifest_hash,
                policy_snapshot_hash=_sha256_json(reusable.policy_snapshot),
                blocked_counts=dict(reusable.blocked_counts or {}),
                reused=True,
            )

    publication = GraphPublication(
        library_id=target.library_id,
        ontology_version_id=target.ontology_version_id,
        status=GRAPH_PUBLICATION_STATUS_PLANNED,
        source_mode=GRAPH_PUBLICATION_SOURCE_ROLLBACK,
        manifest_version=config.graph_publication_manifest_version,
        policy_version=config.graph_publication_policy_version,
        policy_snapshot=policy_snapshot,
        manifest_hash=manifest_hash,
        idempotency_key=idempotency_key,
        include_drafts=target.include_drafts,
        plan_options={"rollback_target_publication_id": str(target.id)},
        parent_publication_id=current.id,
        rollback_target_publication_id=target.id,
        planned_by_user_id=requested_by_user_id,
        entity_count=sum(item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_ENTITY for item in copied_items),
        relation_count=sum(item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_RELATION for item in copied_items),
        blocked_counts=blocked_counts,
        blocked_diagnostics={},
        item_hashes_summary={
            "entity_hashes": sorted(
                item.item_hash for item in copied_items if item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_ENTITY
            ),
            "relation_hashes": sorted(
                item.item_hash for item in copied_items if item.item_kind == GRAPH_PUBLICATION_ITEM_KIND_RELATION
            ),
        },
    )
    if publication.id is None:
        publication.id = uuid.uuid4()
    for item in copied_items:
        item.publication_id = publication.id
    if not dry_run:
        db.add(publication)
        await db.flush()
        for item in copied_items:
            db.add(item)
        await db.flush()
    return GraphPublicationPlanResult(
        publication=publication,
        items=tuple(copied_items),
        manifest_hash=manifest_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        blocked_counts=blocked_counts,
        dry_run=dry_run,
    )
