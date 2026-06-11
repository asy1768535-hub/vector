"""admin 审计日志查询。"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.db import get_db
from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.admin import AuditLogRead

router = APIRouter(prefix="/admin/audit-log", tags=["admin"])


@router.get("", response_model=list[AuditLogRead])
async def list_audit(
    actor_user_id: Optional[uuid.UUID] = Query(default=None),
    action: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit).offset(offset)
    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())
