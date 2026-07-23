from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.sync_source import SyncSource
from app.models.user import User
from app.schemas.external_graph_sync import (
    ExternalGraphConflictRead,
    ExternalGraphConflictDecision,
    ExternalGraphMappingRead,
    ExternalGraphSyncBatchRequest,
    ExternalGraphSyncBatchResponse,
    GraphSyncPolicyRead,
    GraphSyncPolicyWrite,
)
from app.services.external_graph_sync import (
    ExternalGraphSyncError,
    decide_conflict,
    get_source_policy,
    list_conflicts,
    list_mappings,
    policy_read,
    put_source_policy,
    sync_batch,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_access,
    resolve_loaded_library_management,
)
from app.services.organization_rollouts import (
    OrganizationRolloutError,
    require_rollout_enabled,
)

router = APIRouter(
    prefix="/libraries/{slug}/external-graph-sync",
    tags=["external-graph-sync"],
)


@dataclass(frozen=True, slots=True)
class ExternalGraphSyncContext:
    user: User
    library: Library


async def _context(
    slug: str,
    user: User,
    db: AsyncSession,
) -> ExternalGraphSyncContext:
    if not settings.external_graph_sync_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    library = await deps_module.load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await require_rollout_enabled(
            db,
            organization_id=library.organization_id,
            capability="external_graph_sync",
        )
    except OrganizationRolloutError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from exc
    return ExternalGraphSyncContext(user, library)


async def require_sync_read(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ExternalGraphSyncContext:
    context = await _context(slug, user, db)
    try:
        await resolve_loaded_library_access(
            db, user=user, library=context.library, action="read"
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


async def require_sync_insert(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ExternalGraphSyncContext:
    context = await _context(slug, user, db)
    try:
        await resolve_loaded_library_access(
            db, user=user, library=context.library, action="insert"
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


async def require_sync_admin(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ExternalGraphSyncContext:
    context = await _context(slug, user, db)
    try:
        await resolve_loaded_library_management(
            db, user=user, library=context.library
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return context


def _http_error(exc: ExternalGraphSyncError) -> HTTPException:
    if exc.code.endswith("_not_found"):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.code)
    if exc.code in {
        "graph_sync_idempotency_conflict",
        "graph_sync_operation_in_progress",
        "graph_sync_source_disabled",
        "graph_sync_conflict_already_decided",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


@router.put(
    "/sources/{source_key}/policy",
    response_model=GraphSyncPolicyRead,
)
async def write_policy(
    source_key: str,
    body: GraphSyncPolicyWrite,
    context: ExternalGraphSyncContext = Depends(require_sync_admin),
    db: AsyncSession = Depends(get_db),
) -> GraphSyncPolicyRead:
    try:
        policy = await put_source_policy(db, context.library, source_key, body)
        await db.commit()
        source, policy = await get_source_policy(
            db, context.library, source_key
        )
        return policy_read(source, policy)
    except ExternalGraphSyncError as exc:
        await db.rollback()
        raise _http_error(exc) from None
    except (LookupError, ValueError):
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None


@router.get(
    "/sources/{source_key}/policy",
    response_model=GraphSyncPolicyRead,
)
async def read_policy(
    source_key: str,
    context: ExternalGraphSyncContext = Depends(require_sync_admin),
    db: AsyncSession = Depends(get_db),
) -> GraphSyncPolicyRead:
    try:
        source, policy = await get_source_policy(
            db, context.library, source_key
        )
        return policy_read(source, policy)
    except (ExternalGraphSyncError, LookupError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None


@router.post(
    "/sources/{source_key}/batches",
    response_model=ExternalGraphSyncBatchResponse,
)
async def run_batch(
    source_key: str,
    body: ExternalGraphSyncBatchRequest,
    context: ExternalGraphSyncContext = Depends(require_sync_insert),
    db: AsyncSession = Depends(get_db),
) -> ExternalGraphSyncBatchResponse:
    if any(item.action == "delete" for item in body.items):
        try:
            await resolve_loaded_library_access(
                db,
                user=context.user,
                library=context.library,
                action="delete",
            )
        except OrganizationAuthorizationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    try:
        response = await sync_batch(
            db,
            context.library,
            source_key,
            body,
            actor_id=context.user.id,
        )
        await db.commit()
        return response
    except ExternalGraphSyncError as exc:
        await db.rollback()
        raise _http_error(exc) from None
    except (LookupError, ValueError):
        await db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "graph_sync_request_invalid",
        ) from None


@router.get(
    "/sources/{source_key}/mappings",
    response_model=list[ExternalGraphMappingRead],
)
async def read_mappings(
    source_key: str,
    limit: int = Query(default=100, ge=1, le=100),
    context: ExternalGraphSyncContext = Depends(require_sync_read),
    db: AsyncSession = Depends(get_db),
) -> list[ExternalGraphMappingRead]:
    try:
        return await list_mappings(
            db, context.library, source_key, limit=limit
        )
    except (LookupError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None


@router.get(
    "/conflicts",
    response_model=list[ExternalGraphConflictRead],
)
async def read_conflicts(
    limit: int = Query(default=100, ge=1, le=100),
    context: ExternalGraphSyncContext = Depends(require_sync_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ExternalGraphConflictRead]:
    return await list_conflicts(db, context.library, limit=limit)


@router.patch(
    "/conflicts/{conflict_id}",
    response_model=ExternalGraphConflictRead,
)
async def write_conflict_decision(
    conflict_id: uuid.UUID,
    body: ExternalGraphConflictDecision,
    context: ExternalGraphSyncContext = Depends(require_sync_admin),
    db: AsyncSession = Depends(get_db),
) -> ExternalGraphConflictRead:
    try:
        row = await decide_conflict(db, context.library, conflict_id, body)
        source = await db.get(SyncSource, row.sync_source_id)
        await db.commit()
        return ExternalGraphConflictRead(
            id=row.id,
            source_key=source.source_key,
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
    except ExternalGraphSyncError as exc:
        await db.rollback()
        raise _http_error(exc) from None
