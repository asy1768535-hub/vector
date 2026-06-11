"""自助 API Key：列出 / 签发 / 撤销。

需要登录态（cookie 或 Bearer）。明文仅在签发响应中出现一次。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import generate_api_key
from app.auth.backend import current_active_user
from app.db import get_db
from app.models.api_key import ApiKey
from app.models.user import User
from app.schemas.api_keys import ApiKeyCreateRequest, ApiKeyCreated, ApiKeyRead

router = APIRouter(prefix="/me/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyRead])
async def list_keys(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKey]:
    rows = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return list(rows.scalars().all())


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreateRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreated:
    plain, prefix, hashed = generate_api_key()
    row = ApiKey(
        user_id=user.id,
        name=body.name,
        key_prefix=prefix,
        key_hash=hashed,
        expires_at=body.expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ApiKeyCreated(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        plaintext_key=plain,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    entry = row.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    if entry.revoked_at is not None:
        return None
    await db.execute(
        update(ApiKey).where(ApiKey.id == entry.id).values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return None
