from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.schemas.chat_graph_context import (
    ChatGraphContextErrorResponse,
    ChatGraphContextResponse,
)
from app.services import chat_graph_context


router = APIRouter(
    prefix="/libraries/{slug}/chat",
    tags=["chat-graph-context"],
)


_ERROR_STATUS = {
    "citation_chunk_not_found": status.HTTP_404_NOT_FOUND,
    "publication_changed": status.HTTP_409_CONFLICT,
    "graph_retrieval_limit_exceeded": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "graph_publication_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "graph_publication_invariant_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error(status_code: int, detail: str) -> JSONResponse:
    body = ChatGraphContextErrorResponse(detail=detail)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def _rollback_quietly(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


@router.get(
    "/graph-context",
    response_model=ChatGraphContextResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ChatGraphContextErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ChatGraphContextErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ChatGraphContextErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ChatGraphContextErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ChatGraphContextErrorResponse},
        status.HTTP_504_GATEWAY_TIMEOUT: {"model": ChatGraphContextErrorResponse},
    },
)
async def get_chat_graph_context(
    chunk_id: uuid.UUID = Query(...),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> ChatGraphContextResponse | JSONResponse:
    if not settings.graph_retrieval_enabled:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "graph_retrieval_disabled",
        )
    try:
        async with asyncio.timeout(settings.graph_retrieval_timeout_seconds):
            return await chat_graph_context.load_chat_graph_context(
                db,
                library,
                chunk_id,
                config=settings,
            )
    except chat_graph_context.ChatGraphContextServiceError as exc:
        await _rollback_quietly(db)
        return _error(
            _ERROR_STATUS.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            exc.code if exc.code in _ERROR_STATUS else "graph_retrieval_internal_error",
        )
    except TimeoutError:
        await _rollback_quietly(db)
        return _error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "graph_retrieval_timeout",
        )
    except Exception:
        await _rollback_quietly(db)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "graph_retrieval_internal_error",
        )
