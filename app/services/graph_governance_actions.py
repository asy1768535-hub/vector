from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, or_, select

from app.models.canonical_entity import CanonicalEntity
from app.models.canonical_entity_evolution import (
    EVOLUTION_ASSIGNMENT_PENDING,
    EVOLUTION_ASSIGNMENT_RESOLVED,
    EVOLUTION_DECISION_PENDING,
    EVOLUTION_SOURCE_SUPERSEDED,
    CanonicalEntityEvolutionCommand,
    CanonicalEntityEvolutionDecision,
    CanonicalEntityEvolutionSource,
    CanonicalEntityProjectionAssignment,
)
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_resolution_decision import (
    ENTITY_RESOLUTION_STATUS_ACTIVE,
    EntityResolutionDecision,
)
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.user import User
from app.services import audit_log, graph_schema_validator
from app.services.canonical_entity_evolution import canonical_evolution_json_bytes
from app.services.graph_canonical import canonical_graph_value_hash_v1
from app.services.graph_governance_contracts import (
    CancelGraphGovernanceActionCommand,
    DecideGraphGovernanceActionCommand,
    GraphGovernanceEffect,
    GraphGovernanceError,
    MergeConflictResolutionInput,
    ReviewRelationCommand,
    StageAliasDisableCommand,
    StageEntityMergeCommand,
    StageEntityStatusCommand,
    StageGraphGovernanceActionCommand,
    StageRelationStatusCommand,
    SubmitAliasCommand,
    SubmitEntityCorrectionCommand,
    SubmitManualEntityCommand,
    SubmitManualRelationCommand,
    SubmitRelationCorrectionCommand,
    canonical_governance_absent_state_hash,
    canonical_governance_expected_state_hash,
    graph_governance_transition,
)
from app.services.graph_identity_locks import (
    ENTITY_PROJECTION_LOCK_SCOPE,
    GraphIdentityLockScope,
    lock_graph_identity_scopes,
)
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_access,
    resolve_loaded_library_management,
)


@dataclass(frozen=True, slots=True)
class GraphGovernanceActionResult:
    action: GraphGovernanceAction
    items: tuple[GraphGovernanceActionItem, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class GraphGovernanceActionPage:
    items: tuple[GraphGovernanceActionResult, ...]
    total: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def entity_governance_state(entity: Entity, **overrides: Any) -> dict[str, Any]:
    state = {
        "authority_level": entity.authority_level,
        "canonical_name": entity.canonical_name,
        "confidence": entity.confidence,
        "entity_type_id": str(entity.entity_type_id),
        "library_id": str(entity.library_id),
        "normalized_name": entity.normalized_name,
        "ontology_version_id": str(entity.ontology_version_id),
        "properties": dict(entity.properties or {}),
        "source_type": entity.source_type,
        "status": entity.status,
    }
    state.update(overrides)
    return state


def relation_governance_state(
    relation: KnowledgeRelation, **overrides: Any
) -> dict[str, Any]:
    state = {
        "authority_level": relation.authority_level,
        "confidence": relation.confidence,
        "library_id": str(relation.library_id),
        "ontology_version_id": str(relation.ontology_version_id),
        "properties": dict(relation.properties or {}),
        "relation_type_id": str(relation.relation_type_id),
        "review_status": relation.review_status,
        "source_entity_id": str(relation.source_entity_id),
        "source_type": relation.source_type,
        "status": relation.status,
        "target_entity_id": str(relation.target_entity_id),
    }
    state.update(overrides)
    return state


def alias_governance_state(alias: EntityAlias, **overrides: Any) -> dict[str, Any]:
    state = {
        "alias": alias.alias,
        "confidence": alias.confidence,
        "entity_id": str(alias.entity_id),
        "library_id": str(alias.library_id),
        "normalized_alias": alias.normalized_alias,
        "source_type": alias.source_type,
        "status": alias.status,
    }
    state.update(overrides)
    return state


def entity_governance_state_hash(entity: Entity) -> str:
    return canonical_governance_expected_state_hash(
        item_kind="entity", item_id=entity.id, state=entity_governance_state(entity)
    )


def relation_governance_state_hash(relation: KnowledgeRelation) -> str:
    return canonical_governance_expected_state_hash(
        item_kind="relation",
        item_id=relation.id,
        state=relation_governance_state(relation),
    )


def alias_governance_state_hash(alias: EntityAlias) -> str:
    return canonical_governance_expected_state_hash(
        item_kind="alias", item_id=alias.id, state=alias_governance_state(alias)
    )


async def _lock_library(db, library_id: uuid.UUID) -> Library:
    library = (
        await db.execute(
            select(Library)
            .where(Library.id == library_id, Library.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if library is None:
        raise GraphGovernanceError("graph_governance_not_found")
    return library


async def _active_user(db, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if user is None:
        raise GraphGovernanceError("graph_governance_forbidden")
    return user


async def _require_access(
    db,
    *,
    library: Library,
    actor_user_id: uuid.UUID,
    management: bool,
) -> User:
    user = await _active_user(db, actor_user_id)
    try:
        if management:
            await resolve_loaded_library_management(db, user=user, library=library)
        else:
            await resolve_loaded_library_access(
                db,
                user=user,
                library=library,
                action="insert",
            )
    except OrganizationAuthorizationError as exc:
        raise GraphGovernanceError("graph_governance_forbidden") from exc
    return user


async def _lock_entity(db, library: Library, entity_id: uuid.UUID) -> Entity:
    entity = (
        await db.execute(
            select(Entity)
            .where(
                Entity.id == entity_id,
                Entity.library_id == library.id,
                Entity.status != "deleted",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if entity is None:
        raise GraphGovernanceError("graph_governance_not_found")
    return entity


async def _lock_relation(
    db, library: Library, relation_id: uuid.UUID
) -> KnowledgeRelation:
    relation = (
        await db.execute(
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.id == relation_id,
                KnowledgeRelation.library_id == library.id,
                KnowledgeRelation.status != "deleted",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if relation is None:
        raise GraphGovernanceError("graph_governance_not_found")
    return relation


async def _lock_alias(db, library: Library, alias_id: uuid.UUID) -> EntityAlias:
    alias = (
        await db.execute(
            select(EntityAlias)
            .where(
                EntityAlias.id == alias_id,
                EntityAlias.library_id == library.id,
                EntityAlias.status != "deleted",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if alias is None:
        raise GraphGovernanceError("graph_governance_not_found")
    return alias


async def _action_items(db, action_id: uuid.UUID) -> tuple[GraphGovernanceActionItem, ...]:
    return tuple(
        (
            await db.execute(
                select(GraphGovernanceActionItem)
                .where(GraphGovernanceActionItem.action_id == action_id)
                .order_by(GraphGovernanceActionItem.ordinal)
            )
        )
        .scalars()
        .all()
    )


async def _idempotent_action(
    db,
    *,
    library_id: uuid.UUID,
    idempotency_key: str,
    command_hash: str,
) -> GraphGovernanceActionResult | None:
    action = (
        await db.execute(
            select(GraphGovernanceAction).where(
                GraphGovernanceAction.library_id == library_id,
                GraphGovernanceAction.idempotency_key == idempotency_key,
            )
        )
    ).scalars().first()
    if action is None:
        return None
    if action.command_hash != command_hash:
        raise GraphGovernanceError("graph_governance_idempotency_conflict")
    return GraphGovernanceActionResult(action, await _action_items(db, action.id), False)


def _effect_target(effect: GraphGovernanceEffect) -> dict[str, uuid.UUID | None]:
    return {
        "entity_id": effect.item_id if effect.item_kind == "entity" else None,
        "relation_id": effect.item_id if effect.item_kind == "relation" else None,
        "alias_id": effect.item_id if effect.item_kind == "alias" else None,
    }


async def _persist_action(
    db,
    *,
    command: StageGraphGovernanceActionCommand,
    effects: tuple[GraphGovernanceEffect, ...],
    now: datetime,
) -> GraphGovernanceActionResult:
    replay = await _idempotent_action(
        db,
        library_id=command.library_id,
        idempotency_key=command.idempotency_key,
        command_hash=command.command_hash,
    )
    if replay is not None:
        return replay
    if not effects or tuple(effect.ordinal for effect in effects) != tuple(range(len(effects))):
        raise GraphGovernanceError("graph_governance_request_invalid")
    approved = command.initial_status == "approved"
    action = GraphGovernanceAction(
        id=uuid.uuid4(),
        library_id=command.library_id,
        ontology_version_id=command.ontology_version_id,
        action_kind=command.action_kind,
        status=command.initial_status,
        target_entity_id=command.target_entity_id,
        target_relation_id=command.target_relation_id,
        target_alias_id=command.target_alias_id,
        survivor_entity_id=command.survivor_entity_id,
        loser_entity_id=command.loser_entity_id,
        payload=command.payload,
        expected_state_hash=command.expected_state_hash,
        command_hash=command.command_hash,
        idempotency_key=command.idempotency_key,
        reason_code=command.reason_code,
        requested_by_user_id=command.actor_user_id,
        decided_by_user_id=command.actor_user_id if approved else None,
        created_at=now,
        updated_at=now,
        decided_at=now if approved else None,
    )
    items = tuple(
        GraphGovernanceActionItem(
            id=uuid.uuid4(),
            action_id=action.id,
            library_id=command.library_id,
            ordinal=effect.ordinal,
            item_kind=effect.item_kind,
            effect_kind=effect.effect_kind,
            before_hash=effect.before_hash,
            after_hash=effect.after_hash,
            effect_payload=effect.after_state,
            status="planned",
            created_at=now,
            **_effect_target(effect),
        )
        for effect in effects
    )
    db.add(action)
    db.add_all(items)
    await db.flush()
    await audit_log.record(
        db,
        command.actor_user_id,
        "graph_governance.action_staged",
        {
            "action_id": str(action.id),
            "action_kind": action.action_kind,
            "command_hash": action.command_hash,
            "effect_count": len(items),
            "library_id": str(action.library_id),
            "ontology_version_id": str(action.ontology_version_id),
            "reason_code": action.reason_code,
            "status": action.status,
        },
    )
    return GraphGovernanceActionResult(action, items, True)


def _deterministic_target_id(
    library_id: uuid.UUID, action_kind: str, idempotency_key: str
) -> uuid.UUID:
    return uuid.uuid5(library_id, f"graph-governance:{action_kind}:{idempotency_key}")


async def submit_manual_entity(
    db,
    command: SubmitManualEntityCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=False
    )
    entity_id = _deterministic_target_id(library.id, "entity_create", command.idempotency_key)
    staged = StageGraphGovernanceActionCommand(
        library.id,
        command.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "entity_create",
        {
            "canonical_name": command.canonical_name,
            "entity_type_id": str(command.entity_type_id),
            "properties": command.properties or {},
        },
        canonical_governance_absent_state_hash(item_kind="entity", item_id=entity_id),
        "pending_review",
        target_entity_id=entity_id,
    )
    replay = await _idempotent_action(
        db,
        library_id=library.id,
        idempotency_key=command.idempotency_key,
        command_hash=staged.command_hash,
    )
    if replay is not None:
        return replay
    try:
        validation = await graph_schema_validator.validate_entity_write(
            db,
            library,
            ontology_version_id=command.ontology_version_id,
            entity_type_id=command.entity_type_id,
            canonical_name=command.canonical_name,
            properties=command.properties,
            requested_status="draft",
            source_type="manual",
        )
    except ValueError as exc:
        raise GraphGovernanceError("graph_governance_request_invalid") from exc
    entity = Entity(
        id=entity_id,
        library_id=library.id,
        ontology_version_id=validation.ontology_version.id,
        entity_type_id=validation.entity_type.id,
        canonical_name=validation.canonical_name,
        normalized_name=validation.normalized_name,
        properties=validation.properties,
        status="draft",
        source_type="manual",
        created_at=now,
        updated_at=now,
    )
    db.add(entity)
    await db.flush()
    effect = GraphGovernanceEffect(
        0,
        "entity",
        "activate",
        entity.id,
        entity_governance_state_hash(entity),
        entity_governance_state(entity, status="active"),
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def submit_manual_relation(
    db,
    command: SubmitManualRelationCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=False
    )
    relation_id = _deterministic_target_id(
        library.id, "relation_create", command.idempotency_key
    )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        command.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "relation_create",
        {
            "properties": command.properties or {},
            "relation_type_id": str(command.relation_type_id),
            "source_entity_id": str(command.source_entity_id),
            "target_entity_id": str(command.target_entity_id),
        },
        canonical_governance_absent_state_hash(
            item_kind="relation", item_id=relation_id
        ),
        "pending_review",
        target_relation_id=relation_id,
    )
    replay = await _idempotent_action(
        db,
        library_id=library.id,
        idempotency_key=command.idempotency_key,
        command_hash=staged.command_hash,
    )
    if replay is not None:
        return replay
    try:
        validation = await graph_schema_validator.validate_relation_write(
            db,
            library,
            relation_type_id=command.relation_type_id,
            source_entity_id=command.source_entity_id,
            target_entity_id=command.target_entity_id,
            properties=command.properties,
            requested_status="pending_review",
            source_type="manual",
            schema_boundary_clear=True,
        )
    except ValueError as exc:
        raise GraphGovernanceError("graph_governance_request_invalid") from exc
    if validation.source_entity.ontology_version_id != command.ontology_version_id:
        raise GraphGovernanceError("graph_governance_scope_mismatch")
    relation = KnowledgeRelation(
        id=relation_id,
        library_id=library.id,
        ontology_version_id=command.ontology_version_id,
        relation_type_id=validation.relation_type.id,
        source_entity_id=validation.source_entity.id,
        target_entity_id=validation.target_entity.id,
        properties=validation.properties,
        status="pending_review",
        review_status="pending_review",
        source_type="manual",
        created_at=now,
        updated_at=now,
    )
    db.add(relation)
    await db.flush()
    effect = GraphGovernanceEffect(
        0,
        "relation",
        "activate",
        relation.id,
        relation_governance_state_hash(relation),
        relation_governance_state(
            relation,
            status="active",
            review_status="approved",
        ),
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def submit_entity_correction(
    db,
    command: SubmitEntityCorrectionCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=False
    )
    entity = await _lock_entity(db, library, command.entity_id)
    before_hash = entity_governance_state_hash(entity)
    if before_hash != command.expected_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    canonical_name = command.canonical_name or entity.canonical_name
    properties = command.properties if command.replace_properties else entity.properties
    try:
        validation = await graph_schema_validator.validate_entity_write(
            db,
            library,
            ontology_version_id=entity.ontology_version_id,
            entity_type_id=entity.entity_type_id,
            canonical_name=canonical_name,
            properties=properties,
            requested_status=(entity.status if entity.status in {"draft", "pending_review", "active"} else "draft"),
            source_type=entity.source_type,
            confidence=entity.confidence,
            exclude_entity_id=entity.id,
        )
    except ValueError as exc:
        raise GraphGovernanceError("graph_governance_request_invalid") from exc
    after = entity_governance_state(
        entity,
        canonical_name=validation.canonical_name,
        normalized_name=validation.normalized_name,
        properties=validation.properties,
    )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        entity.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "entity_correct",
        {
            "canonical_name": validation.canonical_name,
            "properties": validation.properties,
        },
        before_hash,
        "pending_review",
        target_entity_id=entity.id,
    )
    effect = GraphGovernanceEffect(0, "entity", "update", entity.id, before_hash, after)
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def submit_relation_correction(
    db,
    command: SubmitRelationCorrectionCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=False
    )
    relation = await _lock_relation(db, library, command.relation_id)
    before_hash = relation_governance_state_hash(relation)
    if before_hash != command.expected_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    source_id = command.source_entity_id or relation.source_entity_id
    target_id = command.target_entity_id or relation.target_entity_id
    properties = command.properties if command.replace_properties else relation.properties
    try:
        validation = await graph_schema_validator.validate_relation_write(
            db,
            library,
            relation_type_id=relation.relation_type_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            properties=properties,
            requested_status="draft",
            source_type=relation.source_type,
            confidence=relation.confidence,
            schema_boundary_clear=True,
        )
    except ValueError as exc:
        raise GraphGovernanceError("graph_governance_request_invalid") from exc
    if validation.source_entity.ontology_version_id != relation.ontology_version_id:
        raise GraphGovernanceError("graph_governance_scope_mismatch")
    after = relation_governance_state(
        relation,
        source_entity_id=str(source_id),
        target_entity_id=str(target_id),
        properties=validation.properties,
    )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        relation.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "relation_correct",
        {
            "properties": validation.properties,
            "source_entity_id": str(source_id),
            "target_entity_id": str(target_id),
        },
        before_hash,
        "pending_review",
        target_relation_id=relation.id,
    )
    effect = GraphGovernanceEffect(
        0, "relation", "update", relation.id, before_hash, after
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def submit_alias(
    db,
    command: SubmitAliasCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=False
    )
    entity = await _lock_entity(db, library, command.entity_id)
    if entity_governance_state_hash(entity) != command.expected_entity_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    alias_id = _deterministic_target_id(library.id, "alias_add", command.idempotency_key)
    normalized = normalize_graph_name_v1(command.alias)
    staged = StageGraphGovernanceActionCommand(
        library.id,
        entity.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "alias_add",
        {"alias": command.alias, "entity_id": str(entity.id)},
        canonical_governance_absent_state_hash(item_kind="alias", item_id=alias_id),
        "pending_review",
        target_alias_id=alias_id,
    )
    replay = await _idempotent_action(
        db,
        library_id=library.id,
        idempotency_key=command.idempotency_key,
        command_hash=staged.command_hash,
    )
    if replay is not None:
        return replay
    existing = (
        await db.execute(
            select(EntityAlias).where(
                EntityAlias.library_id == library.id,
                EntityAlias.entity_id == entity.id,
                EntityAlias.normalized_alias == normalized,
                EntityAlias.status != "deleted",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise GraphGovernanceError("graph_governance_idempotency_conflict")
    alias = EntityAlias(
        id=alias_id,
        library_id=library.id,
        entity_id=entity.id,
        alias=command.alias,
        normalized_alias=normalized,
        source_type="manual",
        status="pending_review",
        created_at=now,
        updated_at=now,
    )
    db.add(alias)
    await db.flush()
    effect = GraphGovernanceEffect(
        0,
        "alias",
        "activate",
        alias.id,
        alias_governance_state_hash(alias),
        alias_governance_state(alias, status="active"),
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def _lock_action(
    db, library_id: uuid.UUID, action_id: uuid.UUID
) -> GraphGovernanceAction:
    action = (
        await db.execute(
            select(GraphGovernanceAction)
            .where(
                GraphGovernanceAction.id == action_id,
                GraphGovernanceAction.library_id == library_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if action is None:
        raise GraphGovernanceError("graph_governance_not_found")
    return action


async def _terminalize_unpublished_target(
    db,
    library: Library,
    action: GraphGovernanceAction,
    status: Literal["rejected", "cancelled"],
) -> None:
    safe_status = "rejected"
    if action.action_kind == "entity_create" and action.target_entity_id is not None:
        entity = await _lock_entity(db, library, action.target_entity_id)
        if entity.status in {"draft", "pending_review"}:
            entity.status = safe_status
    elif action.action_kind == "relation_create" and action.target_relation_id is not None:
        relation = await _lock_relation(db, library, action.target_relation_id)
        if relation.status in {"draft", "pending_review"}:
            relation.status = safe_status
            relation.review_status = "rejected"
    elif action.action_kind == "alias_add" and action.target_alias_id is not None:
        alias = await _lock_alias(db, library, action.target_alias_id)
        if alias.status == "pending_review":
            alias.status = safe_status


async def review_action(
    db,
    command: DecideGraphGovernanceActionCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    action = await _lock_action(db, library.id, command.action_id)
    if action.status != command.expected_status:
        raise GraphGovernanceError("graph_governance_state_changed")
    action.status = graph_governance_transition(action.status, command.decision)
    action.reason_code = command.reason_code
    action.decided_by_user_id = command.actor_user_id
    action.decided_at = now
    action.updated_at = now
    if action.status == "rejected":
        await _terminalize_unpublished_target(db, library, action, "rejected")
    await audit_log.record(
        db,
        command.actor_user_id,
        "graph_governance.action_decided",
        {
            "action_id": str(action.id),
            "action_kind": action.action_kind,
            "library_id": str(library.id),
            "reason_code": action.reason_code,
            "status": action.status,
        },
    )
    await db.flush()
    return GraphGovernanceActionResult(action, await _action_items(db, action.id), False)


async def cancel_governance_action(
    db,
    command: CancelGraphGovernanceActionCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    action = await _lock_action(db, library.id, command.action_id)
    user = await _active_user(db, command.actor_user_id)
    requester_cancel = (
        action.status == "pending_review"
        and action.requested_by_user_id == command.actor_user_id
    )
    if not requester_cancel:
        try:
            await resolve_loaded_library_management(db, user=user, library=library)
        except OrganizationAuthorizationError as exc:
            raise GraphGovernanceError("graph_governance_forbidden") from exc
    if action.status != command.expected_status:
        raise GraphGovernanceError("graph_governance_state_changed")
    if action.planned_publication_id is not None:
        raise GraphGovernanceError("graph_governance_action_in_use")
    action.status = graph_governance_transition(action.status, "cancel")
    action.reason_code = command.reason_code
    action.cancelled_by_user_id = command.actor_user_id
    action.cancelled_at = now
    action.updated_at = now
    await _terminalize_unpublished_target(db, library, action, "cancelled")
    await audit_log.record(
        db,
        command.actor_user_id,
        "graph_governance.action_cancelled",
        {
            "action_id": str(action.id),
            "action_kind": action.action_kind,
            "library_id": str(library.id),
            "reason_code": action.reason_code,
            "status": action.status,
        },
    )
    await db.flush()
    return GraphGovernanceActionResult(action, await _action_items(db, action.id), False)


async def review_relation(
    db,
    command: ReviewRelationCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    relation = await _lock_relation(db, library, command.relation_id)
    before_hash = relation_governance_state_hash(relation)
    if before_hash != command.expected_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    after = relation_governance_state(
        relation,
        review_status="approved" if command.decision == "approve" else "rejected",
        status="active" if command.decision == "approve" else "rejected",
    )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        relation.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "relation_review",
        {"decision": command.decision},
        before_hash,
        "approved",
        target_relation_id=relation.id,
        reason_code=command.reason_code,
    )
    effect = GraphGovernanceEffect(
        0, "relation", "update", relation.id, before_hash, after
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def stage_entity_status(
    db,
    command: StageEntityStatusCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    entity = await _lock_entity(db, library, command.entity_id)
    before_hash = entity_governance_state_hash(entity)
    if before_hash != command.expected_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    expected_status = "active" if command.operation == "disable" else "disabled"
    if entity.status != expected_status:
        raise GraphGovernanceError("graph_governance_state_changed")
    action_kind = f"entity_{command.operation}"
    effects: list[GraphGovernanceEffect] = [
        GraphGovernanceEffect(
            0,
            "entity",
            command.operation,
            entity.id,
            before_hash,
            entity_governance_state(
                entity,
                status="disabled" if command.operation == "disable" else "active",
            ),
        )
    ]
    if command.operation == "disable":
        relations = tuple(
            (
                await db.execute(
                    select(KnowledgeRelation)
                    .where(
                        KnowledgeRelation.library_id == library.id,
                        KnowledgeRelation.ontology_version_id == entity.ontology_version_id,
                        KnowledgeRelation.status == "active",
                        or_(
                            KnowledgeRelation.source_entity_id == entity.id,
                            KnowledgeRelation.target_entity_id == entity.id,
                        ),
                    )
                    .order_by(KnowledgeRelation.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for relation in relations:
            effects.append(
                GraphGovernanceEffect(
                    len(effects),
                    "relation",
                    "disable",
                    relation.id,
                    relation_governance_state_hash(relation),
                    relation_governance_state(relation, status="disabled"),
                )
            )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        entity.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        action_kind,
        {"related_relation_count": len(effects) - 1},
        before_hash,
        "approved",
        target_entity_id=entity.id,
        reason_code=command.reason_code,
    )
    return await _persist_action(db, command=staged, effects=tuple(effects), now=now)


async def stage_relation_status(
    db,
    command: StageRelationStatusCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    relation = await _lock_relation(db, library, command.relation_id)
    before_hash = relation_governance_state_hash(relation)
    if before_hash != command.expected_state_hash:
        raise GraphGovernanceError("graph_governance_state_changed")
    expected_status = "active" if command.operation == "disable" else "disabled"
    if relation.status != expected_status:
        raise GraphGovernanceError("graph_governance_state_changed")
    staged = StageGraphGovernanceActionCommand(
        library.id,
        relation.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        f"relation_{command.operation}",
        {},
        before_hash,
        "approved",
        target_relation_id=relation.id,
        reason_code=command.reason_code,
    )
    effect = GraphGovernanceEffect(
        0,
        "relation",
        command.operation,
        relation.id,
        before_hash,
        relation_governance_state(
            relation,
            status="disabled" if command.operation == "disable" else "active",
        ),
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


async def stage_alias_disable(
    db,
    command: StageAliasDisableCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    alias = await _lock_alias(db, library, command.alias_id)
    before_hash = alias_governance_state_hash(alias)
    if before_hash != command.expected_state_hash or alias.status != "active":
        raise GraphGovernanceError("graph_governance_state_changed")
    entity = await _lock_entity(db, library, alias.entity_id)
    staged = StageGraphGovernanceActionCommand(
        library.id,
        entity.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "alias_disable",
        {},
        before_hash,
        "approved",
        target_alias_id=alias.id,
        reason_code=command.reason_code,
    )
    effect = GraphGovernanceEffect(
        0,
        "alias",
        "disable",
        alias.id,
        before_hash,
        alias_governance_state(alias, status="disabled"),
    )
    return await _persist_action(db, command=staged, effects=(effect,), now=now)


def _resolution_map(
    values: tuple[MergeConflictResolutionInput, ...],
) -> dict[uuid.UUID, MergeConflictResolutionInput]:
    return {value.relation_id: value for value in values}


async def _has_live_canonical_evolution_intent(
    db,
    *,
    library_id: uuid.UUID,
    entity_ids: tuple[uuid.UUID, uuid.UUID],
) -> bool:
    """Read the P3 pending projection slot after the shared scope-40 lock."""

    if not hasattr(db, "sync_session"):
        return False
    decisions = tuple(
        (
            await db.execute(
                select(CanonicalEntityEvolutionDecision).where(
                    CanonicalEntityEvolutionDecision.library_id == library_id,
                    CanonicalEntityEvolutionDecision.lifecycle_status
                    == EVOLUTION_DECISION_PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    pending_ids = {row.id for row in decisions}
    if not pending_ids:
        return False
    assignments = tuple(
        (
            await db.execute(
                select(CanonicalEntityProjectionAssignment).where(
                    CanonicalEntityProjectionAssignment.library_id == library_id,
                    CanonicalEntityProjectionAssignment.entity_id.in_(entity_ids),
                    CanonicalEntityProjectionAssignment.evolution_decision_id.in_(pending_ids),
                    CanonicalEntityProjectionAssignment.assignment_state.in_(
                        (EVOLUTION_ASSIGNMENT_PENDING, EVOLUTION_ASSIGNMENT_RESOLVED)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return bool(assignments)


async def canonical_evolution_guard_fingerprint(
    db,
    *,
    library_id: uuid.UUID,
    entity_ids: tuple[uuid.UUID, uuid.UUID],
) -> tuple[str, bool]:
    """Sign the P3.1 state GraphGovernance must re-check after scope-40 locks."""

    if len(set(entity_ids)) != 2:
        raise GraphGovernanceError("graph_governance_state_changed")
    entities = tuple(
        (
            await db.execute(
                select(Entity)
                .where(Entity.library_id == library_id, Entity.id.in_(entity_ids))
                .order_by(Entity.id)
            )
        )
        .scalars()
        .all()
    )
    if len(entities) != 2:
        raise GraphGovernanceError("graph_governance_state_changed")
    active_decisions = tuple(
        (
            await db.execute(
                select(EntityResolutionDecision).where(
                    EntityResolutionDecision.library_id == library_id,
                    EntityResolutionDecision.entity_id.in_(entity_ids),
                    EntityResolutionDecision.lifecycle_status == ENTITY_RESOLUTION_STATUS_ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    canonical_ids = tuple(
        sorted({row.canonical_entity_id for row in entities if row.canonical_entity_id is not None}, key=str)
    )
    canonicals = {
        row.id: row
        for row in (
            (
                await db.execute(
                    select(CanonicalEntity).where(
                        CanonicalEntity.library_id == library_id,
                        CanonicalEntity.id.in_(canonical_ids),
                    )
                )
            )
            .scalars()
            .all()
            if canonical_ids
            else ()
        )
    }
    if len(canonicals) != len(canonical_ids):
        raise GraphGovernanceError("graph_governance_state_changed")
    sources = tuple(
        (
            await db.execute(
                select(CanonicalEntityEvolutionSource).where(
                    CanonicalEntityEvolutionSource.library_id == library_id,
                    CanonicalEntityEvolutionSource.source_canonical_entity_id.in_(canonical_ids),
                    CanonicalEntityEvolutionSource.resolution_state != EVOLUTION_SOURCE_SUPERSEDED,
                )
            )
        )
        .scalars()
        .all()
        if canonical_ids
        else ()
    )
    source_by_canonical: dict[uuid.UUID, CanonicalEntityEvolutionSource] = {}
    for source in sources:
        if source.source_canonical_entity_id in source_by_canonical:
            raise GraphGovernanceError("graph_governance_state_changed")
        source_by_canonical[source.source_canonical_entity_id] = source
    decisions = tuple(
        (
            await db.execute(
                select(CanonicalEntityEvolutionDecision).where(
                    CanonicalEntityEvolutionDecision.library_id == library_id
                )
            )
        )
        .scalars()
        .all()
    )
    commands = {
        row.id: row
        for row in (
            (
                await db.execute(
                    select(CanonicalEntityEvolutionCommand).where(
                        CanonicalEntityEvolutionCommand.library_id == library_id
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    child_ids = {row.supersedes_decision_id for row in decisions if row.supersedes_decision_id}
    heads = {
        row.command_id: row
        for row in decisions
        if row.id not in child_ids
    }
    if len(heads) != len({row.command_id for row in decisions}):
        raise GraphGovernanceError("graph_governance_state_changed")
    assignments = tuple(
        (
            await db.execute(
                select(CanonicalEntityProjectionAssignment).where(
                    CanonicalEntityProjectionAssignment.library_id == library_id,
                    CanonicalEntityProjectionAssignment.entity_id.in_(entity_ids),
                    CanonicalEntityProjectionAssignment.assignment_state.in_(
                        (EVOLUTION_ASSIGNMENT_PENDING, EVOLUTION_ASSIGNMENT_RESOLVED)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    intent_entities: dict[tuple[uuid.UUID, uuid.UUID], set[uuid.UUID]] = {}
    for assignment in assignments:
        head = heads.get(assignment.command_id)
        command = commands.get(assignment.command_id)
        if head is None or command is None:
            raise GraphGovernanceError("graph_governance_state_changed")
        if head.id == assignment.evolution_decision_id and head.lifecycle_status == EVOLUTION_DECISION_PENDING:
            intent_entities.setdefault((command.id, head.id), set()).add(assignment.entity_id)
    payload = {
        "canonical_states": [
            {
                "canonical_entity_id": str(canonical_id),
                "current_source_resolution_state": (
                    source_by_canonical[canonical_id].resolution_state
                    if canonical_id in source_by_canonical
                    else None
                ),
                "current_source_transition_id": (
                    str(source_by_canonical[canonical_id].id)
                    if canonical_id in source_by_canonical
                    else None
                ),
                "operational_status": canonicals[canonical_id].status,
            }
            for canonical_id in canonical_ids
        ],
        "contract_version": "canonical_entity_evolution/v1",
        "entity_states": [
            {
                "active_resolution_decisions": [
                    {
                        "canonical_entity_id": (
                            str(decision.canonical_entity_id)
                            if decision.canonical_entity_id is not None
                            else None
                        ),
                        "decision_fingerprint": decision.decision_fingerprint,
                        "decision_id": str(decision.id),
                        "decision_kind": decision.decision_kind,
                        "subject_fingerprint": decision.subject_fingerprint,
                    }
                    for decision in sorted(
                        (row for row in active_decisions if row.entity_id == entity.id),
                        key=lambda row: (row.subject_fingerprint, str(row.id)),
                    )
                ],
                "canonical_entity_id": (
                    str(entity.canonical_entity_id)
                    if entity.canonical_entity_id is not None
                    else None
                ),
                "entity_id": str(entity.id),
            }
            for entity in entities
        ],
        "library_id": str(library_id),
        "live_evolution_intents": [
            {
                "command_id": str(command_id),
                "current_decision_id": str(decision_id),
                "entity_ids": [str(entity_id) for entity_id in sorted(ids, key=str)],
                "operation_kind": commands[command_id].operation_kind,
            }
            for (command_id, decision_id), ids in sorted(
                intent_entities.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
            )
        ],
        "operation_kind": "graph_governance_entity_merge",
    }
    return hashlib.sha256(canonical_evolution_json_bytes(payload)).hexdigest(), bool(intent_entities)


async def stage_entity_merge(
    db,
    command: StageEntityMergeCommand,
    *,
    now: datetime | None = None,
) -> GraphGovernanceActionResult:
    now = now or _now()
    library = await _lock_library(db, command.library_id)
    await _require_access(
        db, library=library, actor_user_id=command.actor_user_id, management=True
    )
    await lock_graph_identity_scopes(
        db,
        library.id,
        (
            GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, command.survivor_entity_id),
            GraphIdentityLockScope(ENTITY_PROJECTION_LOCK_SCOPE, command.loser_entity_id),
        ),
    )
    rows = tuple(
        (
            await db.execute(
                select(Entity)
                .where(
                    Entity.library_id == library.id,
                    Entity.id.in_((command.survivor_entity_id, command.loser_entity_id)),
                    Entity.status != "deleted",
                )
                .order_by(Entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    survivor = by_id.get(command.survivor_entity_id)
    loser = by_id.get(command.loser_entity_id)
    if survivor is None or loser is None:
        raise GraphGovernanceError("graph_governance_not_found")
    if (
        survivor.ontology_version_id != command.ontology_version_id
        or loser.ontology_version_id != command.ontology_version_id
        or survivor.entity_type_id != loser.entity_type_id
    ):
        raise GraphGovernanceError("graph_governance_merge_incompatible")
    survivor_hash = entity_governance_state_hash(survivor)
    loser_hash = entity_governance_state_hash(loser)
    if (
        survivor_hash != command.expected_survivor_state_hash
        or loser_hash != command.expected_loser_state_hash
        or survivor.status != "active"
        or loser.status != "active"
    ):
        raise GraphGovernanceError("graph_governance_state_changed")
    guard_fingerprint, has_live_intent = await canonical_evolution_guard_fingerprint(
        db,
        library_id=library.id,
        entity_ids=(survivor.id, loser.id),
    )
    if (
        not hmac.compare_digest(
            guard_fingerprint, command.expected_canonical_evolution_guard_fingerprint
        )
        or has_live_intent
    ):
        raise GraphGovernanceError("graph_governance_state_changed")
    relations = tuple(
        (
            await db.execute(
                select(KnowledgeRelation)
                .where(
                    KnowledgeRelation.library_id == library.id,
                    KnowledgeRelation.ontology_version_id == command.ontology_version_id,
                    KnowledgeRelation.status == "active",
                    or_(
                        KnowledgeRelation.source_entity_id == loser.id,
                        KnowledgeRelation.target_entity_id == loser.id,
                    ),
                )
                .order_by(KnowledgeRelation.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    all_active_relations = tuple(
        (
            await db.execute(
                select(KnowledgeRelation)
                .where(
                    KnowledgeRelation.library_id == library.id,
                    KnowledgeRelation.ontology_version_id == command.ontology_version_id,
                    KnowledgeRelation.status == "active",
                )
                .order_by(KnowledgeRelation.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    resolutions = _resolution_map(command.resolutions)
    effects: list[GraphGovernanceEffect] = [
        GraphGovernanceEffect(
            0,
            "entity",
            "retain",
            survivor.id,
            survivor_hash,
            entity_governance_state(survivor),
        ),
        GraphGovernanceEffect(
            1,
            "entity",
            "disable",
            loser.id,
            loser_hash,
            entity_governance_state(loser, status="disabled"),
        ),
    ]
    conflict_count = 0
    for relation in relations:
        source_id = survivor.id if relation.source_entity_id == loser.id else relation.source_entity_id
        target_id = survivor.id if relation.target_entity_id == loser.id else relation.target_entity_id
        conflict = next(
            (
                candidate
                for candidate in all_active_relations
                if candidate.id != relation.id
                and candidate.relation_type_id == relation.relation_type_id
                and candidate.source_entity_id == source_id
                and candidate.target_entity_id == target_id
            ),
            None,
        )
        resolution = resolutions.get(relation.id)
        if conflict is not None:
            conflict_count += 1
            if (
                resolution is None
                or resolution.resolution not in {"disable", "retain"}
                or resolution.conflicting_relation_id != conflict.id
            ):
                raise GraphGovernanceError("graph_governance_merge_conflict")
            effects.append(
                GraphGovernanceEffect(
                    len(effects),
                    "relation",
                    "disable",
                    relation.id,
                    relation_governance_state_hash(relation),
                    relation_governance_state(relation, status="disabled"),
                )
            )
            continue
        if resolution is not None and resolution.resolution in {"disable", "retain"}:
            effects.append(
                GraphGovernanceEffect(
                    len(effects),
                    "relation",
                    "disable",
                    relation.id,
                    relation_governance_state_hash(relation),
                    relation_governance_state(relation, status="disabled"),
                )
            )
            continue
        effects.append(
            GraphGovernanceEffect(
                len(effects),
                "relation",
                "reassign",
                relation.id,
                relation_governance_state_hash(relation),
                relation_governance_state(
                    relation,
                    source_entity_id=str(source_id),
                    target_entity_id=str(target_id),
                ),
            )
        )
    unknown_resolutions = set(resolutions).difference(relation.id for relation in relations)
    if unknown_resolutions:
        raise GraphGovernanceError("graph_governance_merge_conflict")
    aliases = tuple(
        (
            await db.execute(
                select(EntityAlias)
                .where(
                    EntityAlias.library_id == library.id,
                    EntityAlias.entity_id.in_((survivor.id, loser.id)),
                    EntityAlias.status != "deleted",
                )
                .order_by(EntityAlias.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    survivor_aliases = {
        alias.normalized_alias
        for alias in aliases
        if alias.entity_id == survivor.id and alias.status == "active"
    }
    for alias in aliases:
        if alias.entity_id != loser.id or alias.status not in {"active", "pending_review"}:
            continue
        duplicate = alias.normalized_alias in survivor_aliases
        effects.append(
            GraphGovernanceEffect(
                len(effects),
                "alias",
                "disable" if duplicate else "reassign",
                alias.id,
                alias_governance_state_hash(alias),
                alias_governance_state(
                    alias,
                    **(
                        {"status": "disabled"}
                        if duplicate
                        else {"entity_id": str(survivor.id)}
                    ),
                ),
            )
        )
        survivor_aliases.add(alias.normalized_alias)
    expected_merge_hash = canonical_graph_value_hash_v1(
        {
            "loser_state_hash": loser_hash,
            "survivor_state_hash": survivor_hash,
        }
    )
    staged = StageGraphGovernanceActionCommand(
        library.id,
        command.ontology_version_id,
        command.actor_user_id,
        command.idempotency_key,
        "entity_merge",
        {
            "expected_canonical_evolution_guard_fingerprint": guard_fingerprint,
            "conflict_count": conflict_count,
            "effect_count": len(effects),
            "loser_state_hash": loser_hash,
            "survivor_state_hash": survivor_hash,
        },
        expected_merge_hash,
        "approved",
        survivor_entity_id=survivor.id,
        loser_entity_id=loser.id,
        reason_code=command.reason_code,
    )
    return await _persist_action(db, command=staged, effects=tuple(effects), now=now)


async def get_governance_action(
    db,
    *,
    library_id: uuid.UUID,
    action_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> GraphGovernanceActionResult:
    library = await _lock_library(db, library_id)
    await _require_access(
        db, library=library, actor_user_id=actor_user_id, management=True
    )
    action = await _lock_action(db, library.id, action_id)
    return GraphGovernanceActionResult(action, await _action_items(db, action.id), False)


async def list_governance_actions(
    db,
    *,
    library_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    statuses: tuple[str, ...] = (),
    limit: int = 50,
    offset: int = 0,
) -> GraphGovernanceActionPage:
    library = await _lock_library(db, library_id)
    await _require_access(
        db, library=library, actor_user_id=actor_user_id, management=True
    )
    allowed = {"pending_review", "approved", "rejected", "cancelled", "applied"}
    if (
        any(value not in allowed for value in statuses)
        or len(set(statuses)) != len(statuses)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
        or isinstance(offset, bool)
        or offset < 0
    ):
        raise GraphGovernanceError("graph_governance_request_invalid")
    filters = [GraphGovernanceAction.library_id == library.id]
    if statuses:
        filters.append(GraphGovernanceAction.status.in_(statuses))
    total = int(
        (
            await db.execute(
                select(func.count(GraphGovernanceAction.id)).where(*filters)
            )
        ).scalar_one()
    )
    actions = tuple(
        (
            await db.execute(
                select(GraphGovernanceAction)
                .where(*filters)
                .order_by(
                    GraphGovernanceAction.created_at.desc(),
                    GraphGovernanceAction.id,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if not actions:
        return GraphGovernanceActionPage((), total)
    item_rows = tuple(
        (
            await db.execute(
                select(GraphGovernanceActionItem)
                .where(
                    GraphGovernanceActionItem.action_id.in_(
                        tuple(action.id for action in actions)
                    )
                )
                .order_by(
                    GraphGovernanceActionItem.action_id,
                    GraphGovernanceActionItem.ordinal,
                )
            )
        )
        .scalars()
        .all()
    )
    by_action: dict[uuid.UUID, list[GraphGovernanceActionItem]] = {
        action.id: [] for action in actions
    }
    for item in item_rows:
        by_action[item.action_id].append(item)
    return GraphGovernanceActionPage(
        tuple(
            GraphGovernanceActionResult(action, tuple(by_action[action.id]), False)
            for action in actions
        ),
        total,
    )
