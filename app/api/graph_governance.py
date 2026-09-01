from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.graph_governance import (
    GraphGovernanceActionPageRead,
    GraphGovernanceActionRead,
    GraphGovernanceAliasRequest,
    GraphGovernanceCancelRequest,
    GraphGovernanceDecisionRequest,
    GraphGovernanceEntityCorrectionRequest,
    GraphGovernanceEntityMergeRequest,
    GraphGovernanceManualEntityRequest,
    GraphGovernanceManualRelationRequest,
    GraphGovernancePublicationPlanRead,
    GraphGovernancePublicationPlanRequest,
    GraphGovernanceRelationCorrectionRequest,
    GraphGovernanceRelationReviewRequest,
    GraphGovernanceStateRequest,
    GraphGovernanceWriteContextRead,
)
from app.services.graph_governance_actions import (
    GraphGovernanceActionResult,
    cancel_governance_action,
    get_governance_action,
    list_governance_actions,
    review_action,
    review_relation,
    stage_alias_disable,
    stage_entity_merge,
    stage_entity_status,
    stage_relation_status,
    submit_alias,
    submit_entity_correction,
    submit_manual_entity,
    submit_manual_relation,
    submit_relation_correction,
)
from app.services.graph_governance_contracts import (
    CancelGraphGovernanceActionCommand,
    DecideGraphGovernanceActionCommand,
    GraphGovernanceError,
    MergeConflictResolutionInput,
    PlanGraphGovernancePublicationCommand,
    ReviewRelationCommand,
    StageAliasDisableCommand,
    StageEntityMergeCommand,
    StageEntityStatusCommand,
    StageRelationStatusCommand,
    SubmitAliasCommand,
    SubmitEntityCorrectionCommand,
    SubmitManualEntityCommand,
    SubmitManualRelationCommand,
    SubmitRelationCorrectionCommand,
)
from app.services.graph_governance_context import load_graph_governance_write_context
from app.services.graph_governance_publication import (
    plan_graph_governance_publication,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_access,
    resolve_loaded_library_management,
)


router = APIRouter(
    prefix="/libraries/{slug}/graph-governance",
    tags=["graph-governance"],
)


@dataclass(frozen=True, slots=True)
class GraphGovernanceContext:
    user: User
    library: Library


def _require_runtime() -> None:
    if not settings.graph_governance_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def _load_context(slug: str, user: User, db: AsyncSession) -> GraphGovernanceContext:
    _require_runtime()
    library = await deps_module.load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return GraphGovernanceContext(user, library)


async def require_governance_insert(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceContext:
    context = await _load_context(slug, user, db)
    try:
        await resolve_loaded_library_access(
            db,
            user=user,
            library=context.library,
            action="insert",
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


async def require_governance_management(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceContext:
    context = await _load_context(slug, user, db)
    try:
        await resolve_loaded_library_management(
            db,
            user=user,
            library=context.library,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


async def require_governance_writer(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceContext:
    context = await _load_context(slug, user, db)
    try:
        await resolve_loaded_library_access(
            db,
            user=user,
            library=context.library,
            action="insert",
        )
        return context
    except OrganizationAuthorizationError:
        pass
    try:
        await resolve_loaded_library_management(
            db,
            user=user,
            library=context.library,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


async def require_governance_cookie(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceContext:
    return await _load_context(slug, user, db)


def _http_error(exc: GraphGovernanceError) -> HTTPException:
    if exc.code == "graph_governance_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code == "graph_governance_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "graph_governance_request_invalid":
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)
    if exc.code in {
        "graph_governance_state_changed",
        "graph_governance_idempotency_conflict",
        "graph_governance_scope_mismatch",
        "graph_governance_merge_incompatible",
        "graph_governance_merge_conflict",
        "graph_governance_action_in_use",
        "graph_governance_publication_changed",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "graph_governance_unavailable")


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "graph_governance_idempotency_conflict",
        ) from exc
    except Exception:
        await db.rollback()
        raise


def _read(result: GraphGovernanceActionResult) -> GraphGovernanceActionRead:
    action = result.action
    return GraphGovernanceActionRead(
        id=action.id,
        library_id=action.library_id,
        ontology_version_id=action.ontology_version_id,
        action_kind=action.action_kind,
        status=action.status,
        target_entity_id=action.target_entity_id,
        target_relation_id=action.target_relation_id,
        target_alias_id=action.target_alias_id,
        survivor_entity_id=action.survivor_entity_id,
        loser_entity_id=action.loser_entity_id,
        payload=dict(action.payload),
        expected_state_hash=action.expected_state_hash,
        command_hash=action.command_hash,
        reason_code=action.reason_code,
        planned_publication_id=action.planned_publication_id,
        applied_publication_id=action.applied_publication_id,
        requested_by_user_id=action.requested_by_user_id,
        decided_by_user_id=action.decided_by_user_id,
        cancelled_by_user_id=action.cancelled_by_user_id,
        created_at=action.created_at,
        updated_at=action.updated_at,
        decided_at=action.decided_at,
        cancelled_at=action.cancelled_at,
        applied_at=action.applied_at,
        items=list(result.items),
    )


async def _mutate(db: AsyncSession, operation) -> GraphGovernanceActionRead:
    try:
        result = await operation
        await _commit(db)
    except GraphGovernanceError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise
    return _read(result)


@router.get("/context", response_model=GraphGovernanceWriteContextRead)
async def governance_write_context(
    context: GraphGovernanceContext = Depends(require_governance_writer),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceWriteContextRead:
    try:
        return await load_graph_governance_write_context(db, library=context.library)
    except GraphGovernanceError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get("/actions", response_model=GraphGovernanceActionPageRead)
async def actions(
    statuses: list[str] = Query(default_factory=list, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionPageRead:
    try:
        page = await list_governance_actions(
            db,
            library_id=context.library.id,
            actor_user_id=context.user.id,
            statuses=tuple(statuses),
            limit=limit,
            offset=offset,
        )
    except GraphGovernanceError as exc:
        raise _http_error(exc) from exc
    return GraphGovernanceActionPageRead(
        items=[_read(value) for value in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.get("/actions/{action_id}", response_model=GraphGovernanceActionRead)
async def action_detail(
    action_id: uuid.UUID,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    try:
        result = await get_governance_action(
            db,
            library_id=context.library.id,
            action_id=action_id,
            actor_user_id=context.user.id,
        )
    except GraphGovernanceError as exc:
        raise _http_error(exc) from exc
    return _read(result)


@router.post(
    "/entities",
    response_model=GraphGovernanceActionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_entity_action(
    body: GraphGovernanceManualEntityRequest,
    context: GraphGovernanceContext = Depends(require_governance_insert),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        submit_manual_entity(
            db,
            SubmitManualEntityCommand(
                context.library.id,
                body.ontology_version_id,
                body.entity_type_id,
                context.user.id,
                body.idempotency_key,
                body.canonical_name,
                body.properties,
            ),
        ),
    )


@router.post(
    "/relations",
    response_model=GraphGovernanceActionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_relation_action(
    body: GraphGovernanceManualRelationRequest,
    context: GraphGovernanceContext = Depends(require_governance_insert),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        submit_manual_relation(
            db,
            SubmitManualRelationCommand(
                context.library.id,
                body.ontology_version_id,
                body.relation_type_id,
                body.source_entity_id,
                body.target_entity_id,
                context.user.id,
                body.idempotency_key,
                body.properties,
            ),
        ),
    )


@router.post("/entities/{entity_id}/corrections", response_model=GraphGovernanceActionRead)
async def correct_entity_action(
    entity_id: uuid.UUID,
    body: GraphGovernanceEntityCorrectionRequest,
    context: GraphGovernanceContext = Depends(require_governance_insert),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        submit_entity_correction(
            db,
            SubmitEntityCorrectionCommand(
                context.library.id,
                entity_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                canonical_name=body.canonical_name,
                properties=body.properties,
                replace_properties="properties" in body.model_fields_set,
            ),
        ),
    )


@router.post("/relations/{relation_id}/corrections", response_model=GraphGovernanceActionRead)
async def correct_relation_action(
    relation_id: uuid.UUID,
    body: GraphGovernanceRelationCorrectionRequest,
    context: GraphGovernanceContext = Depends(require_governance_insert),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        submit_relation_correction(
            db,
            SubmitRelationCorrectionCommand(
                context.library.id,
                relation_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                source_entity_id=body.source_entity_id,
                target_entity_id=body.target_entity_id,
                properties=body.properties,
                replace_properties="properties" in body.model_fields_set,
            ),
        ),
    )


@router.post("/entities/{entity_id}/aliases", response_model=GraphGovernanceActionRead)
async def create_alias_action(
    entity_id: uuid.UUID,
    body: GraphGovernanceAliasRequest,
    context: GraphGovernanceContext = Depends(require_governance_insert),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        submit_alias(
            db,
            SubmitAliasCommand(
                context.library.id,
                entity_id,
                context.user.id,
                body.idempotency_key,
                body.expected_entity_state_hash,
                body.alias,
            ),
        ),
    )


@router.post("/actions/{action_id}/decision", response_model=GraphGovernanceActionRead)
async def decide_action(
    action_id: uuid.UUID,
    body: GraphGovernanceDecisionRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        review_action(
            db,
            DecideGraphGovernanceActionCommand(
                context.library.id,
                action_id,
                context.user.id,
                body.expected_status,
                body.decision,
                body.reason_code,
            ),
        ),
    )


@router.post("/actions/{action_id}/cancel", response_model=GraphGovernanceActionRead)
async def cancel_action(
    action_id: uuid.UUID,
    body: GraphGovernanceCancelRequest,
    context: GraphGovernanceContext = Depends(require_governance_cookie),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        cancel_governance_action(
            db,
            CancelGraphGovernanceActionCommand(
                context.library.id,
                action_id,
                context.user.id,
                body.expected_status,
                body.reason_code,
            ),
        ),
    )


@router.post("/relations/{relation_id}/review", response_model=GraphGovernanceActionRead)
async def review_relation_action(
    relation_id: uuid.UUID,
    body: GraphGovernanceRelationReviewRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        review_relation(
            db,
            ReviewRelationCommand(
                context.library.id,
                relation_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                body.decision,
                body.reason_code,
            ),
        ),
    )


async def _entity_status_action(
    entity_id: uuid.UUID,
    operation: str,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext,
    db: AsyncSession,
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        stage_entity_status(
            db,
            StageEntityStatusCommand(
                context.library.id,
                entity_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                operation,
                body.reason_code,
            ),
        ),
    )


@router.post("/entities/{entity_id}/disable", response_model=GraphGovernanceActionRead)
async def disable_entity_action(
    entity_id: uuid.UUID,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _entity_status_action(entity_id, "disable", body, context, db)


@router.post("/entities/{entity_id}/restore", response_model=GraphGovernanceActionRead)
async def restore_entity_action(
    entity_id: uuid.UUID,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _entity_status_action(entity_id, "restore", body, context, db)


async def _relation_status_action(
    relation_id: uuid.UUID,
    operation: str,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext,
    db: AsyncSession,
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        stage_relation_status(
            db,
            StageRelationStatusCommand(
                context.library.id,
                relation_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                operation,
                body.reason_code,
            ),
        ),
    )


@router.post("/relations/{relation_id}/disable", response_model=GraphGovernanceActionRead)
async def disable_relation_action(
    relation_id: uuid.UUID,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _relation_status_action(relation_id, "disable", body, context, db)


@router.post("/relations/{relation_id}/restore", response_model=GraphGovernanceActionRead)
async def restore_relation_action(
    relation_id: uuid.UUID,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _relation_status_action(relation_id, "restore", body, context, db)


@router.post("/aliases/{alias_id}/disable", response_model=GraphGovernanceActionRead)
async def disable_alias_action(
    alias_id: uuid.UUID,
    body: GraphGovernanceStateRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        stage_alias_disable(
            db,
            StageAliasDisableCommand(
                context.library.id,
                alias_id,
                context.user.id,
                body.idempotency_key,
                body.expected_state_hash,
                body.reason_code,
            ),
        ),
    )


@router.post("/entities/merge", response_model=GraphGovernanceActionRead)
async def merge_entity_action(
    body: GraphGovernanceEntityMergeRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernanceActionRead:
    return await _mutate(
        db,
        stage_entity_merge(
            db,
            StageEntityMergeCommand(
                context.library.id,
                body.ontology_version_id,
                body.survivor_entity_id,
                body.loser_entity_id,
                context.user.id,
                body.idempotency_key,
                body.expected_survivor_state_hash,
                body.expected_loser_state_hash,
                tuple(
                    MergeConflictResolutionInput(
                        value.relation_id,
                        value.resolution,
                        value.conflicting_relation_id,
                    )
                    for value in body.resolutions
                ),
                body.reason_code,
            ),
        ),
    )


@router.post(
    "/publications/plan",
    response_model=GraphGovernancePublicationPlanRead,
    status_code=status.HTTP_201_CREATED,
)
async def plan_governance_publication(
    body: GraphGovernancePublicationPlanRequest,
    context: GraphGovernanceContext = Depends(require_governance_management),
    db: AsyncSession = Depends(get_db),
) -> GraphGovernancePublicationPlanRead:
    try:
        result, action_set_hash = await plan_graph_governance_publication(
            db,
            PlanGraphGovernancePublicationCommand(
                context.library.id,
                body.ontology_version_id,
                context.user.id,
                tuple(body.action_ids),
                body.expected_parent_publication_id,
                body.idempotency_key,
            ),
            dry_run=body.dry_run,
        )
        if body.dry_run:
            await db.rollback()
        else:
            await _commit(db)
    except GraphGovernanceError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise
    publication = result.publication
    return GraphGovernancePublicationPlanRead(
        publication_id=publication.id,
        status=publication.status,
        manifest_hash=result.manifest_hash,
        action_set_hash=action_set_hash,
        parent_publication_id=publication.parent_publication_id,
        entity_count=publication.entity_count,
        relation_count=publication.relation_count,
        blocked_counts=result.blocked_counts,
        dry_run=result.dry_run,
        reused=result.reused,
    )
