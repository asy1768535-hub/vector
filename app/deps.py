"""通用 FastAPI Depends：用户/超管/库权限校验。

权限校验链：
  - current_active_user：fastapi-users 解出当前 user（JWT cookie 或 API Key 任一）
  - current_superuser：要求 is_superuser
  - require_lib(action)：(user, library:<slug>, action) 经 Casbin 校验，超管直通
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user, current_superuser  # noqa: F401  (re-export)
from app.casbin.enforcer import has_permission
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.services.organization_authorization import (
    Action,
    OrganizationAuthorizationError,
    authorize_library,
    load_active_library,
)

log = logging.getLogger(__name__)

def require_lib(action: Action):
    """生成一个 Depends：要求当前用户在 library:<slug> 上有 action 权限。

    - 库不存在 / 已软删 / 无权限 → 统一抛 403（不暴露存在性）
    - is_superuser 直通
    返回 Library ORM 对象，供下游 handler 使用。
    """

    async def _dep(
        slug: str,
        user: User = Depends(current_active_user),
        db: AsyncSession = Depends(get_db),
    ) -> Library:
        if not settings.organization_authorization_enabled:
            lib = await load_active_library(slug, db)
            if lib is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
            if user.is_superuser or has_permission(str(user.id), slug, action):
                return lib
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        try:
            return await authorize_library(
                db,
                user=user,
                library_slug=slug,
                action=action,
            )
        except OrganizationAuthorizationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc

    return _dep
