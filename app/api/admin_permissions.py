"""admin 权限矩阵：基于 Casbin。

PUT /admin/permissions —— 授权/扩权
DELETE /admin/permissions —— 撤权（actions=None 表示清空该 user-lib 全部策略）
GET /admin/permissions?user_id= —— 反查 (库, 动作)
GET /admin/permissions/library/{slug} —— 反查授权了该库的所有用户
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.casbin import service as casbin_service
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import (
    PermissionGrant,
    PermissionMatrixRow,
    PermissionRevoke,
)
from app.services import audit_log
from app.services.organization_permissions import (
    OrganizationPermissionError,
    grant_platform_library_permissions,
)

router = APIRouter(prefix="/admin/permissions", tags=["admin"])


async def _ensure_user_and_library(
    db: AsyncSession, user_id: uuid.UUID, library_slug: str
) -> tuple[User, Library]:
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    row = await db.execute(
        select(Library).where(Library.slug == library_slug, Library.deleted_at.is_(None))
    )
    lib = row.scalar_one_or_none()
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    return user, lib


@router.put("", status_code=status.HTTP_200_OK)
async def grant(
    body: PermissionGrant,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user, lib = await _ensure_user_and_library(db, body.user_id, body.library_slug)
    mutation = None
    try:
        if settings.organization_authorization_enabled:
            mutation = await grant_platform_library_permissions(
                db,
                actor_user_id=actor.id,
                target_user_id=user.id,
                library=lib,
                actions=body.actions,
            )
            added = [
                (str(user.id), f"library:{lib.slug}", action)
                for action in mutation.added
            ]
        else:
            added = casbin_service.grant(str(user.id), lib.slug, body.actions)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except OrganizationPermissionError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "organization_permission_scope_not_found"
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, exc.code) from exc
    try:
        if mutation is None:
            await audit_log.record(
                db, actor.id, "permission.grant",
                {
                    "user_id": str(user.id),
                    "library_slug": lib.slug,
                    "actions": body.actions,
                    "added": len(added),
                },
            )
        await db.commit()
    except Exception:
        if mutation is not None:
            mutation.compensate()
        await db.rollback()
        raise
    return {"added": [list(t) for t in added]}


@router.delete("", status_code=status.HTTP_200_OK)
async def revoke(
    body: PermissionRevoke,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    user, lib = await _ensure_user_and_library(db, body.user_id, body.library_slug)
    if lib.created_by == user.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "知识库创建者默认拥有全部权限，不能撤销",
        )
    removed = casbin_service.revoke(str(user.id), lib.slug, body.actions)
    await audit_log.record(
        db, actor.id, "permission.revoke",
        {"user_id": str(user.id), "library_slug": lib.slug, "actions": body.actions, "removed": removed},
    )
    await db.commit()
    return {"removed": removed}


@router.get("", response_model=list[PermissionMatrixRow])
async def list_user_perms(
    user_id: uuid.UUID = Query(...),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[PermissionMatrixRow]:
    perms = casbin_service.list_user_permissions(str(user_id))
    return [PermissionMatrixRow(library_slug=slug, actions=actions) for slug, actions in perms.items()]


@router.get("/library/{slug}")
async def list_library_grantees(
    slug: str,
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    return casbin_service.list_library_grantees(slug)
