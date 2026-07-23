from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import Settings, settings
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.graph_publication import GraphPublication
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.user import User
from app.services import audit_log
from app.services.graph_governance_actions import (
    alias_governance_state_hash,
    entity_governance_state_hash,
    relation_governance_state_hash,
)
from app.services.graph_governance_contracts import (
    GRAPH_GOVERNANCE_CONTRACT_VERSION,
    GraphGovernanceActionBinding,
    GraphGovernanceEffect,
    GraphGovernanceError,
    PlanGraphGovernancePublicationCommand,
    StageGraphGovernanceActionCommand,
    canonical_governance_action_set_hash,
)
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    GraphPublicationPlanResult,
    GraphPublicationProjection,
    plan_graph_publication,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_management,
)


@dataclass(frozen=True, slots=True)
class LoadedGraphGovernanceProjection:
    projection: GraphPublicationProjection
    actions: tuple[GraphGovernanceAction, ...]
    items: tuple[GraphGovernanceActionItem, ...]
    entities: dict[uuid.UUID, Entity]
    relations: dict[uuid.UUID, KnowledgeRelation]
    aliases: dict[uuid.UUID, EntityAlias]


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


async def _require_management(db, library: Library, actor_user_id: uuid.UUID) -> None:
    user = (
        await db.execute(
            select(User).where(
                User.id == actor_user_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if user is None:
        raise GraphGovernanceError("graph_governance_forbidden")
    try:
        await resolve_loaded_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise GraphGovernanceError("graph_governance_forbidden") from exc


def graph_governance_plan_identity(
    projection: GraphPublicationProjection,
) -> dict[str, Any]:
    return {
        "action_ids": [str(value) for value in projection.action_ids],
        "action_set_hash": projection.action_set_hash,
        "contract_version": GRAPH_GOVERNANCE_CONTRACT_VERSION,
    }


def parse_graph_governance_plan(
    publication: GraphPublication,
) -> tuple[tuple[uuid.UUID, ...], str] | None:
    raw = dict(publication.plan_options or {}).get("graph_governance")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "action_ids",
        "action_set_hash",
        "contract_version",
    }:
        raise GraphGovernanceError("graph_governance_publication_changed")
    try:
        action_ids = tuple(uuid.UUID(value) for value in raw["action_ids"])
    except (TypeError, ValueError, AttributeError):
        raise GraphGovernanceError("graph_governance_publication_changed") from None
    action_hash = raw.get("action_set_hash")
    if (
        raw.get("contract_version") != GRAPH_GOVERNANCE_CONTRACT_VERSION
        or not action_ids
        or tuple(sorted(action_ids, key=str)) != action_ids
        or len(set(action_ids)) != len(action_ids)
        or not isinstance(action_hash, str)
        or len(action_hash) != 64
        or any(value not in "0123456789abcdef" for value in action_hash)
    ):
        raise GraphGovernanceError("graph_governance_publication_changed")
    return action_ids, action_hash


def _item_id(item: GraphGovernanceActionItem) -> uuid.UUID:
    values = (item.entity_id, item.relation_id, item.alias_id)
    selected = [value for value in values if value is not None]
    if len(selected) != 1:
        raise GraphGovernanceError("graph_governance_unavailable")
    return selected[0]


def _rebuild_action_command(action: GraphGovernanceAction) -> StageGraphGovernanceActionCommand:
    return StageGraphGovernanceActionCommand(
        library_id=action.library_id,
        ontology_version_id=action.ontology_version_id,
        actor_user_id=action.requested_by_user_id or uuid.UUID(int=0),
        idempotency_key=action.idempotency_key,
        action_kind=action.action_kind,
        payload=dict(action.payload or {}),
        expected_state_hash=action.expected_state_hash,
        initial_status="approved",
        target_entity_id=action.target_entity_id,
        target_relation_id=action.target_relation_id,
        target_alias_id=action.target_alias_id,
        survivor_entity_id=action.survivor_entity_id,
        loser_entity_id=action.loser_entity_id,
        reason_code=action.reason_code,
    )


async def _load_targets(
    db,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    items: tuple[GraphGovernanceActionItem, ...],
) -> tuple[
    dict[uuid.UUID, Entity],
    dict[uuid.UUID, KnowledgeRelation],
    dict[uuid.UUID, EntityAlias],
]:
    entity_ids = tuple(sorted({item.entity_id for item in items if item.entity_id}, key=str))
    relation_ids = tuple(
        sorted({item.relation_id for item in items if item.relation_id}, key=str)
    )
    alias_ids = tuple(sorted({item.alias_id for item in items if item.alias_id}, key=str))
    entities: tuple[Entity, ...] = ()
    relations: tuple[KnowledgeRelation, ...] = ()
    aliases: tuple[EntityAlias, ...] = ()
    if entity_ids:
        entities = tuple(
            (
                await db.execute(
                    select(Entity)
                    .where(
                        Entity.library_id == library_id,
                        Entity.ontology_version_id == ontology_version_id,
                        Entity.id.in_(entity_ids),
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
    if relation_ids:
        relations = tuple(
            (
                await db.execute(
                    select(KnowledgeRelation)
                    .where(
                        KnowledgeRelation.library_id == library_id,
                        KnowledgeRelation.ontology_version_id == ontology_version_id,
                        KnowledgeRelation.id.in_(relation_ids),
                        KnowledgeRelation.status != "deleted",
                    )
                    .order_by(KnowledgeRelation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    if alias_ids:
        aliases = tuple(
            (
                await db.execute(
                    select(EntityAlias)
                    .where(
                        EntityAlias.library_id == library_id,
                        EntityAlias.id.in_(alias_ids),
                        EntityAlias.status != "deleted",
                    )
                    .order_by(EntityAlias.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    if (
        len(entities) != len(entity_ids)
        or len(relations) != len(relation_ids)
        or len(aliases) != len(alias_ids)
    ):
        raise GraphGovernanceError("graph_governance_state_changed")
    return (
        {value.id: value for value in entities},
        {value.id: value for value in relations},
        {value.id: value for value in aliases},
    )


async def load_graph_governance_projection(
    db,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    action_ids: tuple[uuid.UUID, ...],
    publication_id: uuid.UUID | None = None,
    require_publication_binding: bool = False,
    allow_existing_publication_binding: bool = False,
) -> LoadedGraphGovernanceProjection:
    ordered_action_ids = tuple(sorted(action_ids, key=str))
    if not ordered_action_ids or len(set(ordered_action_ids)) != len(ordered_action_ids):
        raise GraphGovernanceError("graph_governance_request_invalid")
    actions = tuple(
        (
            await db.execute(
                select(GraphGovernanceAction)
                .where(
                    GraphGovernanceAction.library_id == library_id,
                    GraphGovernanceAction.ontology_version_id == ontology_version_id,
                    GraphGovernanceAction.id.in_(ordered_action_ids),
                )
                .order_by(GraphGovernanceAction.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if tuple(action.id for action in actions) != ordered_action_ids:
        raise GraphGovernanceError("graph_governance_not_found")
    for action in actions:
        if action.status != "approved":
            raise GraphGovernanceError("graph_governance_state_changed")
        if require_publication_binding:
            if publication_id is None or action.planned_publication_id != publication_id:
                raise GraphGovernanceError("graph_governance_publication_changed")
        elif (
            action.planned_publication_id is not None
            and not allow_existing_publication_binding
        ):
            raise GraphGovernanceError("graph_governance_action_in_use")
        if _rebuild_action_command(action).command_hash != action.command_hash:
            raise GraphGovernanceError("graph_governance_state_changed")
    items = tuple(
        (
            await db.execute(
                select(GraphGovernanceActionItem)
                .where(GraphGovernanceActionItem.action_id.in_(ordered_action_ids))
                .order_by(
                    GraphGovernanceActionItem.action_id,
                    GraphGovernanceActionItem.ordinal,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_action: dict[uuid.UUID, list[GraphGovernanceActionItem]] = {
        value: [] for value in ordered_action_ids
    }
    for item in items:
        by_action.get(item.action_id, []).append(item)
    bindings: list[GraphGovernanceActionBinding] = []
    effects: list[tuple[GraphGovernanceActionItem, GraphGovernanceEffect]] = []
    seen_targets: set[tuple[str, uuid.UUID]] = set()
    for action in actions:
        action_items = by_action[action.id]
        if not action_items or [item.ordinal for item in action_items] != list(
            range(len(action_items))
        ):
            raise GraphGovernanceError("graph_governance_state_changed")
        item_hashes: list[str] = []
        for item in action_items:
            item_id = _item_id(item)
            target = (item.item_kind, item_id)
            if target in seen_targets or item.status != "planned":
                raise GraphGovernanceError("graph_governance_state_changed")
            seen_targets.add(target)
            effect = GraphGovernanceEffect(
                item.ordinal,
                item.item_kind,
                item.effect_kind,
                item_id,
                item.before_hash,
                dict(item.effect_payload or {}),
            )
            if effect.after_hash != item.after_hash:
                raise GraphGovernanceError("graph_governance_state_changed")
            item_hashes.append(effect.item_hash)
            effects.append((item, effect))
        bindings.append(
            GraphGovernanceActionBinding(
                action.id,
                action.command_hash,
                tuple(item_hashes),
            )
        )
    entities, relations, aliases = await _load_targets(
        db,
        library_id=library_id,
        ontology_version_id=ontology_version_id,
        items=items,
    )
    entity_states: dict[uuid.UUID, dict[str, Any]] = {}
    relation_states: dict[uuid.UUID, dict[str, Any]] = {}
    for item, effect in effects:
        if item.item_kind == "entity":
            current_hash = entity_governance_state_hash(entities[effect.item_id])
            entity_states[effect.item_id] = effect.after_state
        elif item.item_kind == "relation":
            current_hash = relation_governance_state_hash(relations[effect.item_id])
            relation_states[effect.item_id] = effect.after_state
        else:
            current_hash = alias_governance_state_hash(aliases[effect.item_id])
        if current_hash != item.before_hash:
            raise GraphGovernanceError("graph_governance_state_changed")
    action_set_hash = canonical_governance_action_set_hash(tuple(bindings))
    projection = GraphPublicationProjection(
        entity_states=entity_states,
        relation_states=relation_states,
        action_ids=ordered_action_ids,
        action_set_hash=action_set_hash,
    )
    return LoadedGraphGovernanceProjection(
        projection,
        actions,
        items,
        entities,
        relations,
        aliases,
    )


async def plan_graph_governance_publication(
    db,
    command: PlanGraphGovernancePublicationCommand,
    *,
    dry_run: bool = False,
    config: Settings = settings,
) -> tuple[GraphPublicationPlanResult, str]:
    if not config.graph_governance_enabled:
        raise GraphGovernanceError("graph_governance_unavailable")
    library = await _lock_library(db, command.library_id)
    await _require_management(db, library, command.actor_user_id)
    loaded = await load_graph_governance_projection(
        db,
        library_id=library.id,
        ontology_version_id=command.ontology_version_id,
        action_ids=command.action_ids,
        allow_existing_publication_binding=not dry_run,
    )
    try:
        result = await plan_graph_publication(
            db,
            library,
            ontology_version_id=command.ontology_version_id,
            source_mode="manual_plan",
            include_drafts=False,
            dry_run=dry_run,
            idempotency_key=command.idempotency_key,
            expected_parent_publication_id=command.expected_parent_publication_id,
            requested_by_user_id=command.actor_user_id,
            projection=loaded.projection,
            config=config,
        )
    except GraphPublicationPlanError as exc:
        code = (
            "graph_governance_publication_changed"
            if exc.code in {"expected_parent_mismatch", "idempotency_conflict"}
            else "graph_governance_state_changed"
        )
        raise GraphGovernanceError(code) from exc
    if not dry_run:
        for action in loaded.actions:
            if action.planned_publication_id not in {None, result.publication.id}:
                raise GraphGovernanceError("graph_governance_action_in_use")
        if not result.reused:
            for action in loaded.actions:
                action.planned_publication_id = result.publication.id
                action.updated_at = _now()
            await audit_log.record(
                db,
                command.actor_user_id,
                "graph_governance.publication_planned",
                {
                    "action_count": len(loaded.actions),
                    "action_set_hash": loaded.projection.action_set_hash,
                    "library_id": str(library.id),
                    "ontology_version_id": str(command.ontology_version_id),
                    "publication_id": str(result.publication.id),
                },
            )
            await db.flush()
    return result, loaded.projection.action_set_hash or ""


async def apply_loaded_graph_governance_projection(
    db,
    *,
    publication: GraphPublication,
    loaded: LoadedGraphGovernanceProjection,
    actor_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    expected = parse_graph_governance_plan(publication)
    if expected is None or expected != (
        loaded.projection.action_ids,
        loaded.projection.action_set_hash,
    ):
        raise GraphGovernanceError("graph_governance_publication_changed")
    for item in loaded.items:
        state = dict(item.effect_payload or {})
        if item.item_kind == "entity" and item.entity_id is not None:
            row = loaded.entities[item.entity_id]
            for field in (
                "authority_level",
                "canonical_name",
                "confidence",
                "normalized_name",
                "properties",
                "status",
            ):
                if field in state:
                    setattr(row, field, state[field])
        elif item.item_kind == "relation" and item.relation_id is not None:
            row = loaded.relations[item.relation_id]
            for field in (
                "authority_level",
                "confidence",
                "properties",
                "review_status",
                "status",
            ):
                if field in state:
                    setattr(row, field, state[field])
            for field in ("source_entity_id", "target_entity_id"):
                if field in state:
                    try:
                        setattr(row, field, uuid.UUID(str(state[field])))
                    except (TypeError, ValueError, AttributeError):
                        raise GraphGovernanceError(
                            "graph_governance_state_changed"
                        ) from None
        elif item.item_kind == "alias" and item.alias_id is not None:
            row = loaded.aliases[item.alias_id]
            for field in (
                "alias",
                "confidence",
                "normalized_alias",
                "status",
            ):
                if field in state:
                    setattr(row, field, state[field])
            if "entity_id" in state:
                try:
                    row.entity_id = uuid.UUID(str(state["entity_id"]))
                except (TypeError, ValueError, AttributeError):
                    raise GraphGovernanceError("graph_governance_state_changed") from None
        else:
            raise GraphGovernanceError("graph_governance_state_changed")
        item.status = "applied"
        item.applied_at = now
    for action in loaded.actions:
        action.status = "applied"
        action.applied_publication_id = publication.id
        action.applied_at = now
        action.updated_at = now
    await audit_log.record(
        db,
        actor_user_id,
        "graph_governance.publication_applied",
        {
            "action_count": len(loaded.actions),
            "action_set_hash": loaded.projection.action_set_hash,
            "effect_count": len(loaded.items),
            "library_id": str(publication.library_id),
            "ontology_version_id": str(publication.ontology_version_id),
            "publication_id": str(publication.id),
        },
    )
    await db.flush()


async def release_graph_governance_publication(
    db,
    publication: GraphPublication,
) -> int:
    parsed = parse_graph_governance_plan(publication)
    if parsed is None:
        return 0
    action_ids, _action_set_hash = parsed
    actions = tuple(
        (
            await db.execute(
                select(GraphGovernanceAction)
                .where(
                    GraphGovernanceAction.id.in_(action_ids),
                    GraphGovernanceAction.library_id == publication.library_id,
                    GraphGovernanceAction.planned_publication_id == publication.id,
                    GraphGovernanceAction.status == "approved",
                )
                .order_by(GraphGovernanceAction.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for action in actions:
        action.planned_publication_id = None
    await db.flush()
    return len(actions)
