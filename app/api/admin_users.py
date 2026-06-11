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
from app.schemas.admin import AdminUserCreate, AdminUserRead, AdminUserUpdate
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
        db, actor.id, "user.create",
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


@router.get("/_stats/count")
async def user_count(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    active = (await db.execute(
        select(func.count(User.id)).where(User.is_active.is_(True), User.deleted_at.is_(None))
    )).scalar_one()
    return {"total": int(total), "active": int(active)}
