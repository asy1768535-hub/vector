"""自定义 fastapi-users Strategy：用 sys_api_keys 表的 Bearer Key 验证用户。

read_token: 从 Authorization Header 拿到原始 key →
  - 前 8 位作为 prefix 在 sys_api_keys 查活跃记录候选
  - 对候选记录用 bcrypt 校验 key_hash
  - 命中则更新 last_used_at，返回 User
write_token / destroy_token: 不通过登录接口发 API Key（由 /me/api-keys 单独接口签发），这里 raise。
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi_users.authentication.strategy import Strategy
from fastapi_users.exceptions import InvalidPasswordException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.api_key import ApiKey
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.services.organization_authorization import bind_credential_organization
from app.services.stable_predicate_evolution_actor import (
    bind_credential_api_key_audit_identity,
)

log = logging.getLogger(__name__)

API_KEY_BYTES = 32  # 256 位熵


def hash_api_key(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_api_key(plain: str, hashed: str) -> bool:
    """常量时间校验。任何异常（如非 bcrypt 格式）一律返回 False。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def generate_api_key() -> tuple[str, str, str]:
    """生成一把 key。返回 (plain, prefix, hash)。plain 仅本次出现。"""
    plain = "vk_" + secrets.token_urlsafe(API_KEY_BYTES)
    prefix = plain[:12]  # "vk_" + 9 char
    hashed = hash_api_key(plain)
    return plain, prefix, hashed


class APIKeyStrategy(Strategy[User, uuid.UUID]):
    """fastapi-users Strategy 实现：read_token 校验 Bearer Key。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def read_token(
        self, token: Optional[str], user_manager
    ) -> Optional[User]:
        if not token:
            return None
        prefix = token[:12]
        result = await self._session.execute(
            select(ApiKey).where(
                ApiKey.key_prefix == prefix,
                ApiKey.revoked_at.is_(None),
            )
        )
        candidates = result.scalars().all()
        now = datetime.now(timezone.utc)
        for candidate in candidates:
            if candidate.expires_at is not None and candidate.expires_at < now:
                continue
            if not verify_api_key(token, candidate.key_hash):
                continue
            if settings.organization_authorization_enabled:
                membership = (
                    await self._session.execute(
                        select(OrganizationMembership.id)
                        .join(
                            Organization,
                            Organization.id
                            == OrganizationMembership.organization_id,
                        )
                        .where(
                            OrganizationMembership.organization_id
                            == candidate.organization_id,
                            OrganizationMembership.user_id == candidate.user_id,
                            OrganizationMembership.status == "active",
                            Organization.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if membership is None:
                    continue
            # 命中：更新 last_used_at（fire and forget，错误不阻塞）
            await self._session.execute(
                update(ApiKey).where(ApiKey.id == candidate.id).values(last_used_at=now)
            )
            await self._session.commit()
            try:
                user_uuid = candidate.user_id
                user = await user_manager.get(user_uuid)
                bind_credential_api_key_audit_identity(
                    user,
                    candidate.organization_id,
                    candidate.id,
                )
                if settings.organization_authorization_enabled:
                    bind_credential_organization(
                        user,
                        candidate.organization_id,
                        candidate.id,
                    )
                return user
            except Exception:  # noqa: BLE001
                log.warning("api key matched but user lookup failed")
                return None
        return None

    async def write_token(self, user: User) -> str:
        raise InvalidPasswordException(reason="API keys are issued via /me/api-keys, not via login")

    async def destroy_token(self, token: str, user: User) -> None:
        # 显式撤销走 DELETE /me/api-keys/{id}，这里 no-op
        return None
