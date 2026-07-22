"""/me/permissions —— 当前用户在哪些库上有哪些动作。

前端登录后拉一次用于动态菜单/按钮权限；与后端 Casbin enforce 一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.casbin import service as casbin_service
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import PermissionMatrixRow
from app.services.organization_authorization import list_effective_permissions

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/permissions",
    response_model=list[PermissionMatrixRow],
    response_model_exclude_none=True,
)
async def my_permissions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[PermissionMatrixRow]:
    if not settings.organization_authorization_enabled:
        if user.is_superuser:
            return []
        perms = casbin_service.list_user_permissions(str(user.id))
        if not perms:
            return []
        rows = await db.execute(
            select(Library.slug, Library.name).where(
                Library.slug.in_(list(perms.keys())), Library.deleted_at.is_(None)
            )
        )
        name_by_slug = {slug: name for slug, name in rows.all()}
        return [
            PermissionMatrixRow(
                library_slug=slug,
                actions=actions,
                library_name=name_by_slug[slug],
            )
            for slug, actions in perms.items()
            if slug in name_by_slug
        ]
    projections = await list_effective_permissions(db, user=user)
    return [
        PermissionMatrixRow(
            library_slug=row.library_slug,
            actions=list(row.actions),
            library_name=row.library_name,
            organization_id=row.organization_id,
        )
        for row in projections
    ]
