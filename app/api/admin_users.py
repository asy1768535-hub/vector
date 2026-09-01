"""admin 用户管理。仅 superuser 可访问。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi_users.exceptions import UserAlreadyExists
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.auth.user_manager import UserManager, get_user_manager
from app.db import get_db
from app.models.user import User
from app.schemas.admin import AdminResetPassword, AdminUserCreate, AdminUserRead, AdminUserUpdate
from app.schemas.users import UserCreate
from app.services import audit_log

router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: AdminUserCreate,
    actor: User = Depends(current_superuser),
    user_manager: UserManager = Depends(get_user_manager),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = UserCreate(
        email=body.email,
        password=body.password,
        is_active=True,
        is_superuser=body.is_superuser,
        is_verified=False,
        username=body.username,
        display_name=body.display_name,
    )
    try:
        user = await user_manager.create(payload, safe=False)
    except UserAlreadyExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from exc

    await audit_log.record(
        db,
        actor.id,
        "user.create",
        {"user_id": str(user.id), "email": user.email, "is_superuser": user.is_superuser},
    )
    await db.commit()
    return user


@router.get("", response_model=list[AdminUserRead])
async def list_users(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_deleted: bool = Query(default=False),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.get("/_stats/count")
async def user_count(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    active = (
        await db.execute(
            select(func.count(User.id)).where(User.is_active.is_(True), User.deleted_at.is_(None))
        )
    ).scalar_one()
    return {"total": int(total), "active": int(active)}


@router.get("/{user_id}", response_model=AdminUserRead)
async def get_user(
    user_id: uuid.UUID,
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


@router.patch("/{user_id}", response_model=AdminUserRead)
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    # 防自锁：当前超管不能通过 PATCH 取消自己的超管权限或停用自己，
    # 否则可能把系统锁死（无人能再管理）。降权/停用他人不受限。
    if user_id == actor.id:
        if body.is_superuser is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能取消自己的超级管理员权限")
        if body.is_active is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己的账号")
    changes: dict[str, object] = {}
    for field in ("username", "display_name", "is_active", "is_superuser"):
        value = getattr(body, field)
        if value is not None:
            setattr(user, field, value)
            changes[field] = value
    if changes:
        await audit_log.record(db, actor.id, "user.update", {"user_id": str(user.id), **changes})
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_password(
    user_id: uuid.UUID,
    body: AdminResetPassword,
    actor: User = Depends(current_superuser),
    user_manager: UserManager = Depends(get_user_manager),
    db: AsyncSession = Depends(get_db),
) -> None:
    """超管重置某用户密码。专用端点（与普通 PATCH 分离，password 不混入 AdminUserUpdate）。"""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    # 用 fastapi-users 的 password_helper（argon2）生成哈希，不手写算法；直接设到已由
    # db.get() 加载/锁定的 user 上，与审计同一事务一次提交（原子：审计失败则密码改动一并回滚）。
    user.hashed_password = user_manager.password_helper.hash(body.password)
    # 审计只记 user_id + 动作，绝不记录明文密码或哈希。
    await audit_log.record(db, actor.id, "user.password_reset", {"user_id": str(user.id)})
    await db.commit()
    return None


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_user(
    user_id: uuid.UUID,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> None:
    if user_id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot disable yourself")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)
    await audit_log.record(db, actor.id, "user.disable", {"user_id": str(user.id)})
    await db.commit()
    return None
