"""/me/permissions —— 当前用户在哪些库上有哪些动作。

前端登录后拉一次用于动态菜单/按钮权限；与后端 Casbin enforce 一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.backend import current_active_user
from app.casbin import service as casbin_service
from app.models.user import User
from app.schemas.admin import PermissionMatrixRow

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/permissions", response_model=list[PermissionMatrixRow])
async def my_permissions(user: User = Depends(current_active_user)) -> list[PermissionMatrixRow]:
    if user.is_superuser:
        # superuser 不通过 Casbin 也都通过；前端给个特殊标记即可
        return []
    perms = casbin_service.list_user_permissions(str(user.id))
    return [PermissionMatrixRow(library_slug=s, actions=a) for s, a in perms.items()]
