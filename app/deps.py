"""通用 FastAPI Depends：用户/超管/库权限校验。

权限校验链：
  - current_active_user：fastapi-users 解出当前 user（JWT cookie 或 API Key 任一）
  - current_superuser：要求 is_superuser
  - require_lib(action)：(user, library:<slug>, action) 经 Casbin 校验，超管直通
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user, current_superuser  # noqa: F401  (re-export)
from app.casbin.enforcer import has_permission
from app.db import get_db
from app.models.library import Library
from app.models.user import User

log = logging.getLogger(__name__)

Action = Literal["read", "insert", "delete", "admin"]


async def load_active_library(slug: str, db: AsyncSession) -> Library | None:
    result = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


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
        lib = await load_active_library(slug, db)
        if lib is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        if user.is_superuser:
            return lib
        if has_permission(str(user.id), slug, action):
            return lib
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")

    return _dep
