"""admin 问答日志：查看 Chat 用户端的逐条问答留痕（只读）。

仅 superuser。按用户 / 知识库 / 状态 / 时间筛选；每行 = 一问一答 + 引用来源 + 错误原因。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.db import get_db
from app.models.user import User
from app.schemas.chat import ChatLogRow
from app.services import chat_history

router = APIRouter(prefix="/admin/chat-logs", tags=["admin"])


@router.get("", response_model=list[ChatLogRow])
async def list_chat_logs(
    user_id: uuid.UUID | None = Query(default=None),
    library_slug: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(success|failed)$"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[ChatLogRow]:
    return await chat_history.list_logs(
        db, user_id=user_id, library_slug=library_slug, status=status,
        start=start, end=end, limit=limit, offset=offset,
    )
