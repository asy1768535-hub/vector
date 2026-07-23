from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.user import User
from app.schemas.v05_graph_publication import (
    GraphPublicationActivateRequest,
    GraphPublicationCancelRequest,
    GraphPublicationCommandRead,
    GraphPublicationItemKind,
    GraphPublicationItemList,
    GraphPublicationItemRead,
    GraphPublicationItemStatus,
    GraphPublicationList,
    GraphPublicationPlanRequest,
    GraphPublicationRead,
    GraphPublicationRollbackRequest,
    GraphPublicationSourceMode,
    GraphPublicationStatus,
)
from app.services import audit_log, graph_publication_read
from app.services.graph_governance_contracts import GraphGovernanceError
from app.services.graph_governance_publication import (
    parse_graph_governance_plan,
    release_graph_governance_publication,
)
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
    plan_graph_publication_rollback,
)
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    GraphPublicationPlanResult,
    plan_graph_publication,
    plan_initial_publication,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
    credential_organization_scope,
)


router = APIRouter(
    prefix="/libraries/{slug}/v05/graph-publications",
    tags=["v0.5-graph-publication"],
)


def _require_publication_enabled() -> None:
    if not settings.graph_publication_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "publication_disabled")


def _command_key_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _raise_plan_error(exc: GraphPublicationPlanError) -> None:
    if exc.code in {"ontology_not_found", "active_ontology_not_found"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.code) from exc
    raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc


def _raise_activation_error(exc: GraphPublicationActivationError) -> None:
    if exc.code in {"publication_not_found", "rollback_target_not_found", "library_not_found"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.code) from exc
    if exc.code == "publication_disabled":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code) from exc
    raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc


def _publication_read(publication: GraphPublication) -> GraphPublicationRead:
    return GraphPublicationRead(
        id=publication.id,
        library_id=publication.library_id,
        ontology_version_id=publication.ontology_version_id,
        status=publication.status,
        healthy=publication.status == "active",
        publication_enabled=settings.graph_publication_enabled,
        source_mode=publication.source_mode,
        manifest_version=publication.manifest_version,
        policy_version=publication.policy_version,
        manifest_hash=publication.manifest_hash,
        include_drafts=publication.include_drafts,
        parent_publication_id=publication.parent_publication_id,
        rollback_target_publication_id=publication.rollback_target_publication_id,
        planned_by_user_id=publication.planned_by_user_id,
        activated_by_user_id=publication.activated_by_user_id,
        cancelled_by_user_id=publication.cancelled_by_user_id,
        superseded_by_publication_id=publication.superseded_by_publication_id,
        entity_count=publication.entity_count,
        relation_count=publication.relation_count,
        blocked_counts=dict(publication.blocked_counts or {}),
        error_code=publication.error_code,
        planned_at=publication.planned_at,
        activated_at=publication.activated_at,
        superseded_at=publication.superseded_at,
        cancelled_at=publication.cancelled_at,
        failed_at=publication.failed_at,
        last_reconciled_at=publication.last_reconciled_at,
        created_at=publication.created_at,
        updated_at=publication.updated_at,
    )


def _command_read(result: GraphPublicationPlanResult) -> GraphPublicationCommandRead:
    return GraphPublicationCommandRead(
        **_publication_read(result.publication).model_dump(),
        policy_snapshot_hash=result.policy_snapshot_hash,
        dry_run=result.dry_run,
        reused=result.reused,
    )


async def _scoped_publication(
    db: AsyncSession,
    library: Library,
    publication_id: uuid.UUID,
) -> GraphPublication:
    publication = await graph_publication_read.get_graph_publication(
        db,
        library,
        publication_id,
    )
    if publication is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publication_not_found")
    return publication


@router.post("/plan", response_model=GraphPublicationCommandRead, status_code=status.HTTP_201_CREATED)
async def plan_publication(
    body: GraphPublicationPlanRequest,
    library: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationCommandRead:
    _require_publication_enabled()
    try:
        planner = plan_initial_publication if body.source_mode == "initial_seed" else plan_graph_publication
        result = await planner(
            db,
            library,
            ontology_version_id=body.ontology_version_id,
            include_drafts=body.include_drafts,
            dry_run=body.dry_run,
            idempotency_key=body.idempotency_key,
            expected_parent_publication_id=body.expected_parent_publication_id,
            requested_by_user_id=user.id,
        )
        if body.dry_run:
            await db.rollback()
        else:
            await db.commit()
        return _command_read(result)
    except GraphPublicationPlanError as exc:
        await db.rollback()
        _raise_plan_error(exc)


@router.get("/", response_model=GraphPublicationList)
async def list_publications(
    publication_status: GraphPublicationStatus | None = Query(default=None, alias="status"),
    source_mode: GraphPublicationSourceMode | None = Query(default=None),
    ontology_version_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationList:
    result = await graph_publication_read.list_graph_publications(
        db,
        library,
        publication_status=publication_status,
        source_mode=source_mode,
        ontology_version_id=ontology_version_id,
        page=page,
        page_size=page_size,
    )
    return GraphPublicationList(
        items=[_publication_read(row) for row in result.rows],
        total=result.total,
        page=page,
        page_size=page_size,
        publication_enabled=settings.graph_publication_enabled,
    )


@router.get("/active", response_model=GraphPublicationRead)
async def get_active_publication(
    ontology_version_id: uuid.UUID | None = Query(default=None),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationRead:
    publication = await graph_publication_read.list_current_graph_publication(
        db,
        library,
        ontology_version_id,
    )
    if publication is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "current_publication_not_found")
    return _publication_read(publication)


@router.get("/{publication_id}", response_model=GraphPublicationRead)
async def get_publication(
    publication_id: uuid.UUID,
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationRead:
    return _publication_read(await _scoped_publication(db, library, publication_id))


@router.get("/{publication_id}/items", response_model=GraphPublicationItemList)
async def list_publication_items(
    publication_id: uuid.UUID,
    item_kind: GraphPublicationItemKind | None = Query(default=None),
    item_status: GraphPublicationItemStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationItemList:
    result = await graph_publication_read.list_graph_publication_items(
        db,
        library,
        publication_id,
        item_kind=item_kind,
        item_status=item_status,
        page=page,
        page_size=page_size,
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publication_not_found")
    return GraphPublicationItemList(
        items=[
            GraphPublicationItemRead(
                id=view.item.id,
                publication_id=view.item.publication_id,
                item_kind=view.item.item_kind,
                entity_id=view.item.entity_id,
                relation_id=view.item.relation_id,
                item_hash=view.item.item_hash,
                status=view.item.status,
                support_evidence_ids=list(view.item.support_evidence_ids or []),
                support_counts=dict(view.item.support_counts or {}),
                source_job_ids=list(view.source_job_ids),
                created_at=view.item.created_at,
                updated_at=view.item.updated_at,
            )
            for view in result.rows
        ],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post("/{publication_id}/activate", response_model=GraphPublicationRead)
async def activate_publication(
    publication_id: uuid.UUID,
    body: GraphPublicationActivateRequest,
    library: Library = Depends(require_lib("admin")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationRead:
    _require_publication_enabled()
    scoped = await _scoped_publication(db, library, publication_id)
    try:
        governance_plan = parse_graph_governance_plan(scoped)
    except GraphGovernanceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    if governance_plan is not None and credential_organization_scope(user) is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "cookie authentication required")
    actor_id = user.id
    await db.rollback()
    try:
        result = await activate_graph_publication(
            db,
            publication_id,
            activated_by_user_id=actor_id,
            expected_manifest_hash=body.expected_manifest_hash,
            command_idempotency_key=body.idempotency_key,
        )
        return _publication_read(result.publication)
    except GraphPublicationActivationError as exc:
        _raise_activation_error(exc)


@router.post("/{publication_id}/cancel", response_model=GraphPublicationRead)
async def cancel_publication(
    publication_id: uuid.UUID,
    body: GraphPublicationCancelRequest,
    library: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationRead:
    _require_publication_enabled()
    await db.execute(select(Library.id).where(Library.id == library.id).with_for_update())
    result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.id == publication_id,
            GraphPublication.library_id == library.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    publication = result.scalars().first()
    if publication is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publication_not_found")
    if publication.status != "planned":
        raise HTTPException(status.HTTP_409_CONFLICT, "publication_not_planned")
    try:
        governance_plan = parse_graph_governance_plan(publication)
    except GraphGovernanceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    if governance_plan is not None:
        if credential_organization_scope(user) is not None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "cookie authentication required"
            )
        try:
            await authorize_library_management(db, user=user, library=library)
        except OrganizationAuthorizationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    publication.status = "cancelled"
    publication.cancelled_by_user_id = user.id
    publication.cancelled_at = datetime.now(timezone.utc)
    audit_target = {
        "publication_id": str(publication.id),
        "library_id": str(library.id),
        "ontology_version_id": str(publication.ontology_version_id),
        "reason_code": body.reason_code,
    }
    command_hash = _command_key_hash(body.idempotency_key)
    if command_hash is not None:
        audit_target["command_idempotency_hash"] = command_hash
    await audit_log.record(
        db,
        user.id,
        "graph_publication.cancelled",
        audit_target,
    )
    if governance_plan is not None:
        try:
            await release_graph_governance_publication(db, publication)
        except GraphGovernanceError as exc:
            await db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    await db.commit()
    return _publication_read(publication)


@router.post(
    "/{publication_id}/rollback",
    response_model=GraphPublicationCommandRead,
    status_code=status.HTTP_201_CREATED,
)
async def rollback_publication(
    publication_id: uuid.UUID,
    body: GraphPublicationRollbackRequest,
    library: Library = Depends(require_lib("admin")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphPublicationCommandRead:
    _require_publication_enabled()
    scoped = await _scoped_publication(db, library, publication_id)
    try:
        governance_plan = parse_graph_governance_plan(scoped)
    except GraphGovernanceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    if governance_plan is not None and credential_organization_scope(user) is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "cookie authentication required")
    command_key = body.idempotency_key or f"rollback:{publication_id}:{uuid.uuid4()}"
    try:
        result = await plan_graph_publication_rollback(
            db,
            publication_id,
            idempotency_key=command_key,
            dry_run=body.dry_run,
            requested_by_user_id=user.id,
        )
        if body.dry_run:
            await db.rollback()
        else:
            await db.commit()
        return _command_read(result)
    except GraphPublicationActivationError as exc:
        await db.rollback()
        _raise_activation_error(exc)
