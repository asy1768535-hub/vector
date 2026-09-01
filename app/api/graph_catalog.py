from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.graph_catalog import (
    GraphCatalogEntityDetailRead,
    GraphCatalogEntityPageRead,
    GraphCatalogSearchRequest,
    GraphCatalogRelationDetailRead,
    GraphCatalogRelationPageRead,
    GraphRelationCatalogSearchRequest,
)
from app.services.graph_catalog import (
    search_graph_catalog_entities,
    search_graph_catalog_relations,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogScopeError,
    GraphCatalogSelection,
    GraphEntityCatalogQuery,
    GraphRelationCatalogQuery,
)
from app.services.graph_catalog_details import (
    get_graph_catalog_entity_detail,
    get_graph_catalog_relation_detail,
)


router = APIRouter(
    prefix="/organizations/{organization_id}/graph-catalog",
    tags=["graph-catalog"],
)


def require_graph_catalog_enabled() -> None:
    if not settings.graph_catalog_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _selection(organization_id: uuid.UUID, body: GraphCatalogSearchRequest):
    return GraphCatalogSelection(
        organization_id=organization_id,
        library_slugs=(tuple(body.library_slugs) if body.library_slugs is not None else None),
        scope_id=body.scope_id,
    )


def _http_error(exc: GraphCatalogError) -> HTTPException:
    if exc.code == "graph_catalog_scope_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code == "graph_catalog_scope_incompatible":
        details = []
        if isinstance(exc, GraphCatalogScopeError):
            details = [
                {
                    "library_slug": item.library_slug,
                    "reason_codes": list(item.reason_codes),
                }
                for item in exc.incompatibilities
            ]
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": exc.code, "incompatibilities": details},
        )
    if exc.code == "graph_catalog_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "graph_catalog_unavailable":
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


@router.post("/entities:search", response_model=GraphCatalogEntityPageRead)
async def search_entities(
    organization_id: uuid.UUID,
    body: GraphCatalogSearchRequest,
    _: None = Depends(require_graph_catalog_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphCatalogEntityPageRead:
    try:
        return await search_graph_catalog_entities(
            db,
            user=user,
            query=GraphEntityCatalogQuery(
                selection=_selection(organization_id, body),
                query_text=body.query,
                ontology_version_ids=tuple(body.ontology_version_ids),
                type_keys=tuple(body.type_keys),
                statuses=tuple(body.statuses),
                source_types=tuple(body.source_types),
                publication_state=body.publication_state,
                limit=body.limit,
            ),
            cursor_value=body.cursor,
        )
    except GraphCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.post("/relations:search", response_model=GraphCatalogRelationPageRead)
async def search_relations(
    organization_id: uuid.UUID,
    body: GraphRelationCatalogSearchRequest,
    _: None = Depends(require_graph_catalog_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphCatalogRelationPageRead:
    try:
        return await search_graph_catalog_relations(
            db,
            user=user,
            query=GraphRelationCatalogQuery(
                selection=_selection(organization_id, body),
                query_text=body.query,
                ontology_version_ids=tuple(body.ontology_version_ids),
                type_keys=tuple(body.type_keys),
                statuses=tuple(body.statuses),
                review_statuses=tuple(body.review_statuses),
                source_types=tuple(body.source_types),
                publication_state=body.publication_state,
                limit=body.limit,
            ),
            cursor_value=body.cursor,
        )
    except GraphCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/libraries/{library_slug}/entities/{entity_id}",
    response_model=GraphCatalogEntityDetailRead,
)
async def entity_detail(
    organization_id: uuid.UUID,
    library_slug: str,
    entity_id: uuid.UUID,
    _: None = Depends(require_graph_catalog_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphCatalogEntityDetailRead:
    try:
        return await get_graph_catalog_entity_detail(
            db,
            user=user,
            organization_id=organization_id,
            library_slug=library_slug,
            entity_id=entity_id,
        )
    except GraphCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/libraries/{library_slug}/relations/{relation_id}",
    response_model=GraphCatalogRelationDetailRead,
)
async def relation_detail(
    organization_id: uuid.UUID,
    library_slug: str,
    relation_id: uuid.UUID,
    _: None = Depends(require_graph_catalog_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphCatalogRelationDetailRead:
    try:
        return await get_graph_catalog_relation_detail(
            db,
            user=user,
            organization_id=organization_id,
            library_slug=library_slug,
            relation_id=relation_id,
        )
    except GraphCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
