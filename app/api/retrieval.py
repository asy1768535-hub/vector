"""POST /retrieval —— Dify 外部知识库接口。

权限：要求当前用户在 body.knowledge_id 这个库上有 read 权限（superuser 直通）。
库不存在或无权 → 403（不暴露存在性）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.casbin.enforcer import has_permission
from app.db import get_db
from app.deps import load_active_library
from app.models.user import User
from app.schemas.dify import DifyRetrievalRequest, DifyRetrievalResponse
from app.services.retrieval import run_retrieval

log = logging.getLogger(__name__)
router = APIRouter(tags=["retrieval"])


@router.post("/retrieval", response_model=DifyRetrievalResponse)
async def retrieval(
    request: DifyRetrievalRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DifyRetrievalResponse:
    lib = await load_active_library(request.knowledge_id, db)
    if lib is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not user.is_superuser and not has_permission(str(user.id), lib.slug, "read"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        return await run_retrieval(
            collection=lib.qdrant_collection,
            embedding_model=lib.embedding_model,
            embedding_base_url=lib.embedding_base_url,
            request=request,
            source_config=lib.source_config,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("retrieval failed: knowledge_id=%s", request.knowledge_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "retrieval failed") from exc
