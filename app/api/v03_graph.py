from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.schemas.v03_graph import (
    GraphEntityCreate,
    GraphEntityRead,
    GraphRelationCreate,
    GraphRelationRead,
    RelationEvidenceCreate,
    RelationEvidenceRead,
)
from app.services import graph_entities, graph_relations


router = APIRouter(prefix="/libraries/{slug}/v03", tags=["v0.3-graph"])


def _require_graph_v03_enabled() -> None:
    if not settings.graph_v03_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _raise_api_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    raise exc


@router.post("/entities", response_model=GraphEntityRead, status_code=status.HTTP_201_CREATED)
async def create_entity(
    body: GraphEntityCreate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> GraphEntityRead:
    _require_graph_v03_enabled()
    try:
        row = await graph_entities.create_entity(db, lib, body)
        await db.commit()
        return row
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/relations", response_model=GraphRelationRead, status_code=status.HTTP_201_CREATED)
async def create_relation(
    body: GraphRelationCreate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> GraphRelationRead:
    _require_graph_v03_enabled()
    try:
        row = await graph_relations.create_relation(db, lib, body)
        await db.commit()
        return row
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/relations/{relation_id}", response_model=GraphRelationRead)
async def get_relation(
    relation_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphRelationRead:
    _require_graph_v03_enabled()
    try:
        return await graph_relations.get_relation(db, lib, relation_id)
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/relations/{relation_id}/evidence",
    response_model=RelationEvidenceRead,
    status_code=status.HTTP_201_CREATED,
)
async def bind_relation_evidence(
    relation_id: uuid.UUID,
    body: RelationEvidenceCreate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> RelationEvidenceRead:
    _require_graph_v03_enabled()
    try:
        row = await graph_relations.bind_relation_evidence(db, lib, relation_id, body)
        await db.commit()
        return row
    except Exception as exc:
        _raise_api_error(exc)
